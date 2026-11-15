"""Resolution of one explicit observation context.

A *context* is a request, not a database state:

* an ordered repository search chain, each entry ``(repo_id, observation_id)``
* a subject POM (by coordinate or by an explicit file in an observation)

Nothing in this module performs a lookup outside that chain.  In particular the
database's newest observation is never consulted implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import Problem
from .metadata import SnapshotMetadata, check_snapshot_identity, parse_metadata
from .pom import Coordinate, ParsedPom, parse_pom
from .store import FileFact, Store

ENGINE_VERSION = "obsdeps-engine/1"


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoSelection:
    repo_id: str
    observation_id: str

    def render(self) -> str:
        return f"{self.repo_id}@{self.observation_id}"

    def to_dict(self) -> Dict[str, str]:
        return {"repo_id": self.repo_id, "observation_id": self.observation_id}


@dataclass(frozen=True)
class RootSpec:
    """Either a logical coordinate or a concrete POM file in one observation."""

    coordinate: Optional[Coordinate] = None
    file: Optional[Tuple[str, str, str]] = None   # (repo_id, observation_id, path)

    def to_dict(self) -> Dict[str, object]:
        if self.coordinate is not None:
            return {"coordinate": self.coordinate.as_dict()}
        assert self.file is not None
        return {"file": {"repo_id": self.file[0], "observation_id": self.file[1], "path": self.file[2]}}


@dataclass(frozen=True)
class ResolutionRequest:
    chain: Tuple[RepoSelection, ...]
    root: RootSpec

    def chain_dicts(self) -> List[Dict[str, str]]:
        return [selection.to_dict() for selection in self.chain]


# ---------------------------------------------------------------------------
# Shared parse cache.  Keys always include repo, observation, path and digest,
# so two contexts can share an identical ancestor file but never a different
# file that happens to live at the same path.
# ---------------------------------------------------------------------------

@dataclass
class ParseCacheStats:
    pom_parses: int = 0
    pom_hits: int = 0
    metadata_parses: int = 0
    metadata_hits: int = 0
    parsed_keys: List[str] = field(default_factory=list)
    hit_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "pom_parses": self.pom_parses,
            "pom_cache_hits": self.pom_hits,
            "metadata_parses": self.metadata_parses,
            "metadata_cache_hits": self.metadata_hits,
            "parsed_files": sorted(set(self.parsed_keys)),
            "cache_hit_files": sorted(set(self.hit_keys)),
        }


class ParseCache:
    """Process-local cache of parsed POM/metadata documents."""

    def __init__(self) -> None:
        self._poms: Dict[Tuple[str, str, str, str], ParsedPom] = {}
        self._metadata: Dict[Tuple[str, str, str, str], SnapshotMetadata] = {}
        self.stats = ParseCacheStats()

    @staticmethod
    def _key(fact: FileFact) -> Tuple[str, str, str, str]:
        return (fact.repo_id, fact.observation_id, fact.path, fact.sha256)

    def pom(self, fact: FileFact) -> ParsedPom:
        key = self._key(fact)
        label = self._label(fact)
        cached = self._poms.get(key)
        if cached is not None:
            self.stats.pom_hits += 1
            self.stats.hit_keys.append(label)
            return cached
        parsed = parse_pom(fact.content, fact.locator, fact.path)
        self._poms[key] = parsed
        self.stats.pom_parses += 1
        self.stats.parsed_keys.append(label)
        return parsed

    def metadata(self, fact: FileFact) -> SnapshotMetadata:
        key = self._key(fact)
        label = self._label(fact)
        cached = self._metadata.get(key)
        if cached is not None:
            self.stats.metadata_hits += 1
            self.stats.hit_keys.append(label)
            return cached
        parsed = parse_metadata(fact.content, fact.locator)
        self._metadata[key] = parsed
        self.stats.metadata_parses += 1
        self.stats.parsed_keys.append(label)
        return parsed

    @staticmethod
    def _label(fact: FileFact) -> str:
        return f"{fact.repo_id}@{fact.observation_id}/{fact.path}#sha256:{fact.sha256[:12]}"


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------

@dataclass
class SelectedFile:
    role: str
    path: str
    found: Optional[FileFact] = None
    repo_id: Optional[str] = None
    observation_id: Optional[str] = None
    trail: List[Dict[str, object]] = field(default_factory=list)
    snapshot: Optional[Dict[str, object]] = None
    problem: Optional[Problem] = None

    @property
    def present(self) -> bool:
        return self.found is not None

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "role": self.role,
            "path": self.path,
            "present": self.present,
        }
        if self.found is not None:
            out.update({
                "repo_id": self.found.repo_id,
                "observation_id": self.found.observation_id,
                "sha256": self.found.sha256,
                "size": self.found.size,
            })
        if self.trail:
            out["search_trail"] = self.trail
        if self.snapshot is not None:
            out["snapshot"] = self.snapshot
        if self.problem is not None:
            out["problem"] = self.problem.to_dict()
        return out


class FileSelector:
    def __init__(self, store: Store, chain: Sequence[RepoSelection], parse_cache: ParseCache):
        self.store = store
        self.chain = tuple(chain)
        self.parse_cache = parse_cache

    # ------------------------------------------------------------ primitives
    def locate_exact(self, path: str) -> Tuple[Optional[FileFact], List[Dict[str, object]]]:
        trail: List[Dict[str, object]] = []
        for selection in self.chain:
            fact = self.store.read_file(selection.repo_id, selection.observation_id, path)
            trail.append({
                "repo_id": selection.repo_id,
                "observation_id": selection.observation_id,
                "present": fact is not None,
            })
            if fact is not None:
                return fact, trail
        return None, trail

    def locate_exact_in(self, repo_id: str, observation_id: str, path: str) -> Optional[FileFact]:
        return self.store.read_file(repo_id, observation_id, path)

    # -------------------------------------------------------------- metadata
    def locate_metadata(
        self, coordinate: Coordinate
    ) -> Tuple[Optional[FileFact], Optional[SnapshotMetadata], List[Dict[str, object]], List[Problem]]:
        path = coordinate.metadata_path()
        fact, trail = self.locate_exact(path)
        if fact is None:
            return None, None, trail, []
        meta = self.parse_cache.metadata(fact)
        problems = list(meta.problems)
        problems.extend(check_snapshot_identity(
            meta,
            group_id=coordinate.group_id,
            artifact_id=coordinate.artifact_id,
            version=coordinate.version,
            file_locator=fact.locator,
        ))
        return fact, meta, trail, problems

    # ------------------------------------------------------------------- POM
    def select_pom(
        self, coordinate: Coordinate, *, role: str = "pom"
    ) -> Tuple[Optional[SelectedFile], List[Problem], List[Problem]]:
        """Select a coordinate's POM, using metadata for SNAPSHOT versions.

        Returns ``(selection, problems, missing)``; ``problems`` are
        contradictions, ``missing`` are absent-material facts.
        """
        problems: List[Problem] = []
        missing: List[Problem] = []
        if coordinate.version.endswith("-SNAPSHOT"):
            meta_fact, meta, trail, meta_problems = self.locate_metadata(coordinate)
            selection = SelectedFile(role=role, path=coordinate.pom_path(), trail=trail)
            if meta_problems:
                problems.extend(meta_problems)
                selection.problem = meta_problems[0]
                return selection, problems, missing
            if meta_fact is None or meta is None:
                problem = Problem(
                    "snapshot-metadata-missing",
                    "SNAPSHOT version needs maven-metadata.xml, none of the selected observations has it",
                    f"chain({', '.join(s.render() for s in self.chain)})/{coordinate.metadata_path()}",
                    {"coordinate": coordinate.as_dict(), "path": coordinate.metadata_path(),
                     "search_trail": trail},
                )
                missing.append(problem)
                selection.problem = problem
                return selection, problems, missing
            mapping, mapping_problems = meta.mapping_for("pom", "")
            if mapping_problems:
                problems.extend(mapping_problems)
                selection.problem = mapping_problems[0]
                return selection, problems, missing
            if mapping is None:
                problem = Problem(
                    "snapshot-mapping-missing",
                    "maven-metadata.xml has no snapshotVersions mapping for extension='pom', classifier=''",
                    meta_fact.locator,
                    {
                        "coordinate": coordinate.as_dict(),
                        "extension": "pom",
                        "classifier": "",
                        "available": meta.describe_available(),
                    },
                )
                missing.append(problem)
                selection.problem = problem
                selection.snapshot = {
                    "metadata": meta_fact.to_ref(),
                    "available": meta.describe_available(),
                }
                return selection, problems, missing
            target_path = coordinate.artifact_path("pom", "", mapping.value)
            fact = self.locate_exact_in(meta_fact.repo_id, meta_fact.observation_id, target_path)
            selection.path = target_path
            selection.snapshot = {
                "metadata": meta_fact.to_ref(),
                "extension": "pom",
                "classifier": "",
                "value": mapping.value,
                "mapping_line": mapping.line,
                "timestamp": meta.timestamp,
                "build_number": meta.build_number,
                "timestamp_used_for_selection": False,
            }
            if fact is None:
                problem = Problem(
                    "snapshot-target-missing",
                    "file named by snapshotVersions mapping is not present in the observation "
                    "that supplied the metadata",
                    f"{meta_fact.repo_id}/{meta_fact.observation_id}/{target_path}",
                    {
                        "coordinate": coordinate.as_dict(),
                        "mapping": {"extension": "pom", "classifier": "", "value": mapping.value},
                        "metadata": meta_fact.to_ref(),
                    },
                )
                missing.append(problem)
                selection.problem = problem
                return selection, problems, missing
            selection.found = fact
            selection.repo_id = fact.repo_id
            selection.observation_id = fact.observation_id
            return selection, problems, missing

        fact, trail = self.locate_exact(coordinate.pom_path())
        selection = SelectedFile(role=role, path=coordinate.pom_path(), trail=trail, found=fact)
        if fact is None:
            problem = Problem(
                "pom-missing",
                "no selected observation contains the POM for this coordinate",
                f"chain({', '.join(s.render() for s in self.chain)})/{coordinate.pom_path()}",
                {"coordinate": coordinate.as_dict(), "search_trail": trail},
            )
            missing.append(problem)
            selection.problem = problem
        else:
            selection.repo_id = fact.repo_id
            selection.observation_id = fact.observation_id
        return selection, problems, missing

    # -------------------------------------------------------------- artifacts
    def select_artifact(
        self,
        coordinate: Coordinate,
        extension: str,
        classifier: str,
        *,
        role: str,
    ) -> Tuple[SelectedFile, List[Problem], List[Problem]]:
        problems: List[Problem] = []
        missing: List[Problem] = []
        classifier = classifier or ""
        if coordinate.version.endswith("-SNAPSHOT"):
            meta_fact, meta, trail, meta_problems = self.locate_metadata(coordinate)
            selection = SelectedFile(role=role, path=coordinate.artifact_path(extension, classifier), trail=trail)
            if meta_problems:
                problems.extend(meta_problems)
                selection.problem = meta_problems[0]
                return selection, problems, missing
            if meta_fact is None or meta is None:
                problem = Problem(
                    "snapshot-metadata-missing",
                    "SNAPSHOT artifact needs maven-metadata.xml, none of the selected observations has it",
                    f"chain({', '.join(s.render() for s in self.chain)})/{coordinate.metadata_path()}",
                    {"coordinate": coordinate.as_dict(), "extension": extension,
                     "classifier": classifier, "search_trail": trail},
                )
                missing.append(problem)
                selection.problem = problem
                return selection, problems, missing
            mapping, mapping_problems = meta.mapping_for(extension, classifier)
            if mapping_problems:
                problems.extend(mapping_problems)
                selection.problem = mapping_problems[0]
                return selection, problems, missing
            if mapping is None:
                problem = Problem(
                    "snapshot-mapping-missing",
                    f"maven-metadata.xml has no snapshotVersions mapping for extension={extension!r}, "
                    f"classifier={classifier!r}; this subset does not derive it from timestamp/buildNumber",
                    meta_fact.locator,
                    {
                        "coordinate": coordinate.as_dict(),
                        "extension": extension,
                        "classifier": classifier,
                        "available": meta.describe_available(),
                    },
                )
                missing.append(problem)
                selection.problem = problem
                selection.snapshot = {
                    "metadata": meta_fact.to_ref(),
                    "available": meta.describe_available(),
                }
                return selection, problems, missing
            target_path = coordinate.artifact_path(extension, classifier, mapping.value)
            fact = self.locate_exact_in(meta_fact.repo_id, meta_fact.observation_id, target_path)
            selection.path = target_path
            selection.snapshot = {
                "metadata": meta_fact.to_ref(),
                "extension": extension,
                "classifier": classifier,
                "value": mapping.value,
                "mapping_line": mapping.line,
                "timestamp": meta.timestamp,
                "build_number": meta.build_number,
                "timestamp_used_for_selection": False,
            }
            if fact is None:
                problem = Problem(
                    "snapshot-target-missing",
                    "file named by snapshotVersions mapping is not present in the observation "
                    "that supplied the metadata",
                    f"{meta_fact.repo_id}/{meta_fact.observation_id}/{target_path}",
                    {
                        "coordinate": coordinate.as_dict(),
                        "mapping": {"extension": extension, "classifier": classifier, "value": mapping.value},
                        "metadata": meta_fact.to_ref(),
                    },
                )
                missing.append(problem)
                selection.problem = problem
                return selection, problems, missing
            selection.found = fact
            selection.repo_id = fact.repo_id
            selection.observation_id = fact.observation_id
            return selection, problems, missing

        path = coordinate.artifact_path(extension, classifier)
        fact, trail = self.locate_exact(path)
        selection = SelectedFile(role=role, path=path, trail=trail, found=fact)
        if fact is None:
            problem = Problem(
                "artifact-missing",
                f"no selected observation contains {path}",
                f"chain({', '.join(s.render() for s in self.chain)})/{path}",
                {
                    "coordinate": coordinate.as_dict(),
                    "extension": extension,
                    "classifier": classifier,
                    "search_trail": trail,
                },
            )
            missing.append(problem)
            selection.problem = problem
        else:
            selection.repo_id = fact.repo_id
            selection.observation_id = fact.observation_id
        return selection, problems, missing
