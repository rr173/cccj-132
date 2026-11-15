"""Resolution engine: effective declarations, version derivation and evidence.

The engine works strictly inside one :class:`ResolutionRequest`.  Repository
lookup, parent selection, property values and SNAPSHOT mappings all come from
the request's observations; the newest observation in the database is never a
fallback.

Report buckets are kept separate on purpose:

* ``unsupported`` -- constructs this subset refuses to pretend to interpret
* ``missing``     -- material the selected observations do not contain
* ``problems``    -- contradictions between observed facts
* per-dependency ``unresolved`` -- a direct dependency whose version cannot be
  derived; this is not silently turned into an empty dependency.

Status precedence: invalid (contradiction) > blocked (root/ancestor POM or its
snapshot mapping unavailable) > partial (unresolved versions, missing
non-chain material, unsupported constructs that affect resolution) > resolved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import Problem
from .interpolate import Interpolator
from .pom import Coordinate, DepKey, DependencyDecl, ManagementDecl
from .pomproject import ChainResult, ProjectLoader, ProjectNode
from .resolution import ENGINE_VERSION, FileSelector, ParseCache, ResolutionRequest
from .sources import SourceRef
from .store import FileFact, Observation, Store

#: Result statuses in increasing severity.  "unresolved" is a per-dependency
#: status that maps onto project-level "partial": something is known to be
#: underdetermined, but nothing contradicts itself and the chain is complete.
STATUS_ORDER = {
    "resolved": 0,
    "unresolved": 1,
    "partial": 1,
    "blocked": 2,
    "invalid": 3,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _worst(statuses: Iterable[str]) -> str:
    worst = "resolved"
    for status in statuses:
        if STATUS_ORDER[status] > STATUS_ORDER[worst]:
            worst = status
    return worst


def _dedupe_problems(problems: Iterable[Problem]) -> List[Problem]:
    out: List[Problem] = []
    seen = set()
    for problem in problems:
        try:
            details_key = json.dumps(problem.details, sort_keys=True, default=str)
        except TypeError:  # pragma: no cover - defensive
            details_key = repr(problem.details)
        key = (problem.code, problem.locator, details_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(problem)
    return out


@dataclass
class ManagementEntry:
    key: DepKey
    key_raw: Dict[str, Optional[str]]
    version_raw: Optional[str]
    version: Optional[str]
    declaring_project: str
    declaration: SourceRef
    version_source: SourceRef
    property_evidence: List[Dict[str, object]] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    overrides: List[Dict[str, object]] = field(default_factory=list)
    scope: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "key": self.key.as_dict(),
            "declared_raw": {
                "groupId": self.key_raw.get("groupId"),
                "artifactId": self.key_raw.get("artifactId"),
                "type": self.key_raw.get("type"),
                "classifier": self.key_raw.get("classifier"),
                "version": self.version_raw,
            },
            "version": self.version,
            "declared_in": self.declaration.to_dict(),
            "version_value_source": self.version_source.to_dict(),
            "declaring_project": self.declaring_project,
            "properties": self.property_evidence,
        }
        if self.scope is not None:
            out["scope"] = self.scope
        if self.overrides:
            out["overrides"] = self.overrides
        if self.problems:
            out["problems"] = [p.to_dict() for p in self.problems]
        return out


@dataclass
class DependencyResult:
    key: Optional[DepKey]
    key_raw: Dict[str, Optional[str]]
    declaration: SourceRef
    version_raw: Optional[str]
    version: Optional[str]
    version_source_kind: str          # direct | managed | unresolved
    version_evidence: Dict[str, object] = field(default_factory=dict)
    management_entry: Optional[ManagementEntry] = None
    scope: Optional[str] = None
    status: str = "resolved"
    files: List[Dict[str, object]] = field(default_factory=list)
    artifact_gaps: List[Dict[str, object]] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    missing: List[Problem] = field(default_factory=list)
    unsupported: List[Problem] = field(default_factory=list)

    def coordinate(self) -> Optional[Coordinate]:
        if self.key is None or self.version is None:
            return None
        return Coordinate(self.key.group_id, self.key.artifact_id, self.version)

    def to_dict(self) -> Dict[str, object]:
        coord = self.coordinate()
        out: Dict[str, object] = {
            "key": self.key.as_dict() if self.key else {
                **self.key_raw, "key": None,
            },
            "declaration": self.declaration.to_dict(),
            "version": {
                "raw": self.version_raw,
                "resolved": self.version,
                "source": self.version_source_kind,
                "evidence": self.version_evidence,
            },
            "status": self.status,
        }
        if coord is not None:
            out["coordinate"] = coord.as_dict()
            out["full_coordinate"] = {
                "groupId": coord.group_id,
                "artifactId": coord.artifact_id,
                "version": coord.version,
                "type": self.key.type if self.key else None,
                "classifier": self.key.classifier if self.key else None,
            }
        if self.scope is not None:
            out["scope"] = self.scope
        if self.files:
            out["files"] = self.files
            out["artifact_status"] = "partial" if self.artifact_gaps else "complete"
        if self.artifact_gaps:
            out["artifact_gaps"] = self.artifact_gaps
        if self.problems:
            out["problems"] = [p.to_dict() for p in self.problems]
        if self.missing:
            out["missing"] = [p.to_dict() for p in self.missing]
        if self.unsupported:
            out["unsupported"] = [p.to_dict() for p in self.unsupported]
        return out


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class Resolver:
    """Resolves one requested context with one shared parse cache."""

    def __init__(self, store: Store, request: ResolutionRequest, cache: ParseCache):
        self.store = store
        self.request = request
        self.cache = cache
        self.selector = FileSelector(store, request.chain, cache)
        self.problems: List[Problem] = []
        self.missing: List[Problem] = []
        self.unsupported: List[Problem] = []
        self._observations: Dict[Tuple[str, str], Observation] = {}

    # ------------------------------------------------------------------ entry
    def run(self) -> Dict[str, object]:
        self._validate_chain()
        if self.problems:
            return self._assemble(None, None, {}, [], "invalid")
        loader = ProjectLoader(self.selector, self.cache, self.request.chain)
        chain_result = loader.load_subject(
            coordinate=self.request.root.coordinate,
            explicit_file=self.request.root.file,
        )
        self.problems.extend(chain_result.problems)
        self.missing.extend(chain_result.missing)
        self.unsupported.extend(chain_result.unsupported)

        if not chain_result.complete or chain_result.subject is None:
            status = "invalid" if self.problems else "blocked"
            return self._assemble(chain_result, chain_result.subject, {}, [], status)

        subject = chain_result.subject
        subject_env = self._project_interpolator(chain_result, subject)
        management, excluded, mgmt_problems = self._build_management(chain_result, subject_env)
        self.problems.extend(mgmt_problems)
        self.unsupported.extend(excluded)

        # Even with contradictions in the management table, direct dependencies
        # are still evaluated so each one can show what it could and could not
        # derive.  The overall status stays "invalid".
        dependencies = self._resolve_dependencies(chain_result, subject, subject_env, management)
        status = self._project_status(dependencies)
        return self._assemble(chain_result, subject, management, dependencies, status,
                              excluded_management=excluded, subject_env=subject_env)

    # ---------------------------------------------------------------- chain
    def _validate_chain(self) -> None:
        if not self.request.chain:
            self.problems.append(Problem(
                "empty-chain", "the request has an empty repository search chain", "request.chain"
            ))
            return
        seen = set()
        for index, selection in enumerate(self.request.chain):
            key = (selection.repo_id, selection.observation_id)
            if key in seen:
                self.problems.append(Problem(
                    "duplicate-chain-entry",
                    "the same observation appears twice in the search chain; order would be ambiguous",
                    f"request.chain[{index}]",
                    {"repo_id": selection.repo_id, "observation_id": selection.observation_id},
                ))
                continue
            seen.add(key)
            observation = self.store.get_observation(selection.repo_id, selection.observation_id)
            if observation is None:
                self.problems.append(Problem(
                    "observation-not-registered",
                    "the request selects an observation that was never registered",
                    f"request.chain[{index}]",
                    {"repo_id": selection.repo_id, "observation_id": selection.observation_id},
                ))
            else:
                self._observations[key] = observation

    def _project_interpolator(self, chain: ChainResult, node: ProjectNode) -> Interpolator:
        coords: Dict[str, str] = {}
        if node.coordinate is not None:
            coords = {
                "groupId": node.coordinate.group_id,
                "artifactId": node.coordinate.artifact_id,
                "version": node.coordinate.version,
            }
        return Interpolator(
            coords,
            node.effective_raw_properties,
            project_id=node.fact.locator,
            context_locator=node.fact.locator,
        )

    # ----------------------------------------------------------- management
    def _build_management(
        self, chain: ChainResult, env: Interpolator
    ) -> Tuple[Dict[DepKey, ManagementEntry], List[Problem], List[Problem]]:
        table: Dict[DepKey, ManagementEntry] = {}
        excluded: List[Problem] = []
        problems: List[Problem] = []
        for node in chain.nodes:
            node_keys: Dict[DepKey, ManagementEntry] = {}
            for decl in node.parsed.management:
                if decl.scope_raw and decl.scope_raw.strip() == "import":
                    excluded.append(Problem(
                        "unsupported-bom-import",
                        "scope=import (BOM import) is not supported; this management entry is "
                        "excluded from the effective table and its versions are not consulted",
                        self._loc(node, decl.line),
                        {"declaration": node.fact.to_ref(), "raw": self._raw_key(decl)},
                    ))
                    continue
                entry, entry_problems, entry_unsupported = self._management_entry(node, decl, env)
                excluded.extend(entry_unsupported)
                problems.extend(entry_problems)
                if entry is None:
                    continue
                same_node = node_keys.get(entry.key)
                if same_node is not None:
                    problems.append(Problem(
                        "duplicate-management-entry",
                        f"dependencyManagement key {entry.key.render()} is declared more than once "
                        f"in the same POM; refusing to pick one",
                        entry.declaration.locator,
                        {
                            "key": entry.key.render(),
                            "key_parts": entry.key.as_dict(),
                            "first": same_node.declaration.to_dict(),
                            "second": entry.declaration.to_dict(),
                            "declaring_project": entry.declaring_project,
                        },
                    ))
                    continue
                node_keys[entry.key] = entry
                previous = table.get(entry.key)
                if previous is not None and previous.declaring_project != entry.declaring_project:
                    entry.overrides = list(previous.overrides) + [{
                        "key": previous.key.as_dict(),
                        "version": previous.version,
                        "declared_in": previous.declaration.to_dict(),
                        "declaring_project": previous.declaring_project,
                    }]
                table[entry.key] = entry
        return table, excluded, problems

    def _management_entry(
        self, node: ProjectNode, decl: ManagementDecl, env: Interpolator
    ) -> Tuple[Optional[ManagementEntry], List[Problem], List[Problem]]:
        locator = self._loc(node, decl.line)
        problems: List[Problem] = []
        unsupported: List[Problem] = []
        key_raw = {
            "groupId": decl.group_id_raw,
            "artifactId": decl.artifact_id_raw,
            "type": decl.type_raw,
            "classifier": decl.classifier_raw,
            "version": decl.version_raw,
        }
        values: Dict[str, Optional[str]] = {}
        referenced: List[str] = []
        for field_name in ("groupId", "artifactId", "type", "classifier", "version"):
            raw = key_raw[field_name]
            if raw is None:
                values[field_name] = None
                continue
            expanded = env.expand(raw, locator=locator, what=f"dependencyManagement/{field_name}")
            referenced.extend(expanded.referenced)
            if not expanded.ok:
                for problem in expanded.problems:
                    problems.append(Problem(
                        problem.code,
                        problem.message,
                        locator,
                        {**problem.details, "management_entry": key_raw, "field": field_name},
                    ))
                values[field_name] = None
            else:
                values[field_name] = expanded.value

        type_value = (values["type"] or "jar").strip() or "jar"
        classifier_value = (values["classifier"] or "").strip()
        if values["groupId"] is None or values["artifactId"] is None:
            return None, problems, unsupported
        key = DepKey(values["groupId"], values["artifactId"], type_value, classifier_value)
        version = values["version"]
        if decl.version_raw is None:
            # A management entry without a version cannot supply one.
            problems.append(Problem(
                "management-version-missing",
                "dependencyManagement entry has no version; it cannot supply a version",
                locator,
                {"key": key.as_dict(), "declaration": node.fact.to_ref()},
            ))
        evidence = env.property_evidence(referenced)
        entry = ManagementEntry(
            key=key,
            key_raw=key_raw,
            version_raw=decl.version_raw,
            version=version,
            declaring_project=node.coordinate.as_string() if node.coordinate else node.fact.locator,
            declaration=self._ref(node, decl.line),
            version_source=self._ref(node, decl.version_line or decl.line),
            property_evidence=evidence,
            problems=problems,
            scope=decl.scope_raw,
        )
        return entry, problems, unsupported

    # ---------------------------------------------------------- dependencies
    def _resolve_dependencies(
        self,
        chain: ChainResult,
        subject: ProjectNode,
        env: Interpolator,
        management: Dict[DepKey, ManagementEntry],
    ) -> List[DependencyResult]:
        results: List[DependencyResult] = []
        seen_keys: Dict[DepKey, DependencyResult] = {}
        for decl in subject.parsed.dependencies:
            result = self._dependency_result(subject, decl, env, management)
            if result.key is not None:
                if result.key in seen_keys:
                    self.problems.append(Problem(
                        "duplicate-direct-dependency",
                        f"direct dependency {result.key.render()} is declared more than once; "
                        f"only the first declaration is used and the result is invalid",
                        result.declaration.locator,
                        {
                            "key": result.key.render(),
                            "key_parts": result.key.as_dict(),
                            "first": seen_keys[result.key].declaration.to_dict(),
                            "second": result.declaration.to_dict(),
                        },
                    ))
                    continue
                seen_keys[result.key] = result
            results.append(result)
            self.problems.extend(result.problems)
            self.missing.extend(result.missing)
            self.unsupported.extend(result.unsupported)
        return results

    def _dependency_result(
        self,
        subject: ProjectNode,
        decl: DependencyDecl,
        env: Interpolator,
        management: Dict[DepKey, ManagementEntry],
    ) -> DependencyResult:
        locator = self._loc(subject, decl.line)
        problems: List[Problem] = []
        missing: List[Problem] = []
        unsupported: List[Problem] = []
        key_raw = {
            "groupId": decl.group_id_raw,
            "artifactId": decl.artifact_id_raw,
            "type": decl.type_raw,
            "classifier": decl.classifier_raw,
            "version": decl.version_raw,
        }
        values: Dict[str, Optional[str]] = {}
        referenced: List[str] = []
        for field_name in ("groupId", "artifactId", "type", "classifier", "version"):
            raw = key_raw[field_name]
            if raw is None:
                values[field_name] = None
                continue
            expanded = env.expand(raw, locator=locator, what=f"dependency/{field_name}")
            referenced.extend(expanded.referenced)
            if not expanded.ok:
                for problem in expanded.problems:
                    problems.append(Problem(
                        problem.code,
                        problem.message,
                        locator,
                        {**problem.details, "dependency": key_raw, "field": field_name},
                    ))
                values[field_name] = None
            else:
                values[field_name] = expanded.value

        if values["groupId"] is None or values["artifactId"] is None:
            return DependencyResult(
                key=None,
                key_raw=key_raw,
                declaration=self._ref(subject, decl.line),
                version_raw=decl.version_raw,
                version=None,
                version_source_kind="unresolved",
                status="invalid",
                problems=problems,
            )
        key = DepKey(values["groupId"], values["artifactId"],
                     (values["type"] or "jar").strip() or "jar",
                     (values["classifier"] or "").strip())
        if decl.scope_raw and decl.scope_raw.strip() == "import":
            unsupported.append(Problem(
                "unsupported-bom-import",
                "a direct dependency with scope=import is not supported; it is reported as a "
                "dependency but no BOM import is performed",
                locator,
                {"declaration": subject.fact.to_ref(), "key": key.as_dict()},
            ))
        result = DependencyResult(
            key=key,
            key_raw=key_raw,
            declaration=self._ref(subject, decl.line),
            version_raw=decl.version_raw,
            version=None,
            version_source_kind="unresolved",
            scope=decl.scope_raw,
        )
        result.problems.extend(problems)
        result.unsupported.extend(unsupported)

        if decl.version_raw is not None:
            exact = env.expand(decl.version_raw, locator=locator, what="dependency/version")
            if not exact.ok:
                # Problems were already recorded above for version.
                result.status = "invalid"
                result.version_evidence = {
                    "kind": "direct-declared",
                    "raw": decl.version_raw,
                    "declared_in": self._ref(subject, decl.version_line or decl.line).to_dict(),
                    "properties": env.property_evidence(exact.referenced),
                    "problems": [p.to_dict() for p in exact.problems],
                }
                return result
            result.version = exact.value
            result.version_source_kind = "direct"
            result.version_evidence = {
                "kind": "direct-declared",
                "raw": decl.version_raw,
                "resolved": exact.value,
                "declared_in": self._ref(subject, decl.version_line or decl.line).to_dict(),
                "properties": env.property_evidence(exact.referenced),
                "rule": "explicit version on a direct dependency wins over dependencyManagement",
            }
            result.status = "resolved"
        else:
            managed = management.get(key)
            if managed is None or managed.version is None:
                result.version_source_kind = "unresolved"
                result.status = "unresolved"
                result.version_evidence = {
                    "kind": "unresolved",
                    "reason": "no explicit version and no matching dependencyManagement entry",
                    "lookup_key": key.as_dict(),
                    "declared_in": self._ref(subject, decl.line).to_dict(),
                }
                result.missing.append(Problem(
                    "dependency-version-unresolved",
                    f"direct dependency {key.render()} has no version and no usable management "
                    f"entry; this subset never picks a repository's highest version",
                    locator,
                    {
                        "key": key.as_dict(),
                        "declaration": subject.fact.to_ref(),
                        "management_key": key.render(),
                    },
                ))
            else:
                result.version = managed.version
                result.version_source_kind = "managed"
                result.management_entry = managed
                result.version_evidence = {
                    "kind": "managed",
                    "lookup_key": key.as_dict(),
                    "managed_from": managed.declaration.to_dict(),
                    "version_value_source": managed.version_source.to_dict(),
                    "raw_version": managed.version_raw,
                    "resolved_version": managed.version,
                    "declaring_project": managed.declaring_project,
                    "properties": managed.property_evidence,
                    "overrides": managed.overrides,
                    "rule": "dependencyManagement supplies the version because the direct "
                            "dependency has none",
                }
                result.status = "resolved"

        if result.version is not None:
            self._collect_files(result, subject)
        return result

    # -------------------------------------------------------------- artifacts
    def _collect_files(self, result: DependencyResult, subject: ProjectNode) -> None:
        """Select this dependency's POM and artifact files independently."""
        coordinate = result.coordinate()
        assert coordinate is not None
        wanted: List[Tuple[str, str, str]] = [("pom", "", "pom")]
        artifact_extension = result.key.type if result.key else "jar"
        classifier = result.key.classifier if result.key else ""
        if (artifact_extension, classifier) != ("pom", ""):
            wanted.append((artifact_extension, classifier, "artifact"))

        for extension, cls, role in wanted:
            if extension == "pom" and cls == "":
                selection, problems, missing = self.selector.select_pom(coordinate, role="dependency-pom")
            else:
                selection, problems, missing = self.selector.select_artifact(
                    coordinate, extension, cls, role="dependency-artifact"
                )
            entry: Dict[str, object] = {
                "role": "dependency-pom" if (extension == "pom" and cls == "") else "dependency-artifact",
                "extension": extension,
                "classifier": cls,
                "expected_path": selection.path,
            }
            if selection.found is not None:
                entry.update({
                    "observed": True,
                    "repo_id": selection.found.repo_id,
                    "observation_id": selection.found.observation_id,
                    "path": selection.found.path,
                    "sha256": selection.found.sha256,
                    "size": selection.found.size,
                })
                sidecar = self._verify_sidecar(selection.found)
                if sidecar is not None:
                    entry["digest_check"] = sidecar
                    if sidecar["status"] == "mismatch":
                        problem = Problem(
                            "artifact-digest-mismatch",
                            f"{selection.found.path} does not match the checksum sidecar "
                            f"{sidecar['sidecar_path']}",
                            f"{selection.found.repo_id}/{selection.found.observation_id}/"
                            f"{sidecar['sidecar_path']}",
                            {
                                "file": selection.found.to_ref(),
                                **sidecar,
                            },
                        )
                        result.problems.append(problem)
                        result.status = "invalid"
            else:
                entry["observed"] = False
                if selection.problem is not None:
                    entry["problem"] = selection.problem.to_dict()
                # A dependency's own POM/artifact may legitimately be absent:
                # this subset does not resolve transitively and a bundle may
                # hold only what is needed to explain versions.  Absence is
                # reported per file but does not downgrade the subject.
                # A SNAPSHOT metadata claim is different: the observation
                # contains metadata that names a file, so failing to find that
                # file is missing material.
                if selection.problem is not None:
                    if selection.problem.code in (
                        "snapshot-metadata-missing",
                        "snapshot-mapping-missing",
                        "snapshot-target-missing",
                    ):
                        result.missing.append(selection.problem)
                        if result.status == "resolved":
                            result.status = "partial"
                    else:
                        # Observed placeholder material is simply absent; make
                        # the gap explicit without pretending the version is
                        # unknown.
                        result.artifact_gaps.append({
                            "role": entry["role"],
                            "extension": extension,
                            "classifier": cls,
                            "path": selection.path,
                            "code": selection.problem.code,
                            "message": selection.problem.message,
                        })
            if selection.snapshot is not None:
                entry["snapshot"] = selection.snapshot
            if selection.trail:
                entry["search_trail"] = selection.trail
            result.files.append(entry)
            result.problems.extend(problems)
            if problems:
                result.status = "invalid"
            for problem in missing:
                if problem.code in ("snapshot-metadata-missing", "snapshot-mapping-missing",
                                    "snapshot-target-missing"):
                    if problem not in result.missing:
                        result.missing.append(problem)
                    if result.status == "resolved":
                        result.status = "partial"

    def _verify_sidecar(self, fact: FileFact) -> Optional[Dict[str, object]]:
        """Compare a file with ``<path>.sha1`` / ``.sha256`` in the same observation.

        A sidecar is an observed fact; when it disagrees with the observed bytes
        that is a contradiction the caller turns into an ``invalid`` result.
        """
        for extension, algo in (("sha256", "sha256"), ("sha1", "sha1")):
            sidecar_path = f"{fact.path}.{extension}"
            sidecar = self.store.read_file(fact.repo_id, fact.observation_id, sidecar_path)
            if sidecar is None:
                continue
            declared = sidecar.content.decode("utf-8", "replace").strip().split()[0] if sidecar.content.strip() else ""
            actual = hashlib.new(algo, fact.content).hexdigest()
            matches = declared.lower() == actual.lower()
            return {
                "status": "match" if matches else "mismatch",
                "sidecar_path": sidecar_path,
                "sidecar_sha256": sidecar.sha256,
                "algorithm": algo,
                "declared": declared.lower(),
                "actual": actual,
            }
        return None

    # ------------------------------------------------------------------ util
    def _ref(self, node: ProjectNode, line: Optional[int]) -> SourceRef:
        return node.ref.with_line(line)

    @staticmethod
    def _loc(node: ProjectNode, line: Optional[int]) -> str:
        return node.fact.locator_at(line)

    @staticmethod
    def _raw_key(decl: ManagementDecl) -> Dict[str, Optional[str]]:
        return {
            "groupId": decl.group_id_raw,
            "artifactId": decl.artifact_id_raw,
            "type": decl.type_raw,
            "classifier": decl.classifier_raw,
            "version": decl.version_raw,
        }

    def _project_status(self, dependencies: Sequence[DependencyResult]) -> str:
        statuses = ["resolved"]
        if self.problems:
            statuses.append("invalid")
        if self.missing:
            statuses.append("partial")
        if any(p.details.get("affects_resolution", True) for p in self.unsupported):
            statuses.append("partial")
        for dep in dependencies:
            if dep.status in ("unresolved", "invalid", "partial"):
                statuses.append(dep.status)
        return _worst(statuses)

    # ------------------------------------------------------------- assembly
    def _assemble(
        self,
        chain: Optional[ChainResult],
        subject: Optional[ProjectNode],
        management: Dict[DepKey, ManagementEntry],
        dependencies: List[DependencyResult],
        status: str,
        *,
        excluded_management: Optional[List[Problem]] = None,
        subject_env: Optional[Interpolator] = None,
    ) -> Dict[str, object]:
        chain_problems = list(chain.problems) if chain else []
        chain_missing = list(chain.missing) if chain else []
        unsupported = list(self.unsupported)
        problems = _dedupe_problems([*chain_problems, *self.problems])
        missing = _dedupe_problems([*chain_missing, *self.missing])
        excluded_management = excluded_management or []

        management_entries = [
            entry.to_dict() for entry in management.values()
        ]
        management_entries.sort(key=lambda e: (e["key"]["groupId"], e["key"]["artifactId"],
                                               e["key"]["type"], e["key"]["classifier"]))

        properties = self._property_report(chain, subject_env)

        payload = {
            "context": self._context_dict(),
            "status": status,
            "subject": self._subject_dict(subject, chain),
            "parent_chain": chain.chain_summary() if chain else [],
            "parent_edges": [edge.to_dict() for edge in (chain.parent_edges if chain else [])],
            "properties": properties,
            "management": {
                "entries": management_entries,
                "excluded": [p.to_dict() for p in excluded_management],
            },
            "dependencies": [dep.to_dict() for dep in dependencies],
            "artifact_gaps": [
                {"dependency": dep.key.render() if dep.key else None, **gap}
                for dep in dependencies
                for gap in dep.artifact_gaps
            ],
            "unsupported": [p.to_dict() for p in _dedupe_problems(unsupported)],
            "missing": [p.to_dict() for p in missing],
            "problems": [p.to_dict() for p in problems],
            "cache": self.cache.stats.to_dict(),
            "engine": ENGINE_VERSION,
        }
        if status in ("blocked", "invalid") and not dependencies and subject is not None:
            # Deliver the known raw declarations without pretending they are
            # effective dependencies: versions are not derived in this state.
            payload["declared_dependencies"] = [
                {
                    "raw": self._raw_key(decl),
                    "declared_in": self._loc(subject, decl.line),
                    "status": "not-evaluated",
                    "reason": "subject chain is not fully available; no version is derived",
                }
                for decl in subject.parsed.dependencies
            ]
            payload["declared_management"] = [
                {
                    "raw": self._raw_key(decl),
                    "declared_in": self._loc(subject, decl.line),
                    "status": "not-evaluated",
                    "reason": "subject chain is not fully available; the management table "
                              "is not applied",
                }
                for decl in subject.parsed.management
            ]
        return payload

    def _context_dict(self) -> Dict[str, object]:
        root = self.request.root.to_dict()
        payload = {
            "chain": self.request.chain_dicts(),
            "root": root,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observations = []
        for selection in self.request.chain:
            observation = self._observations.get((selection.repo_id, selection.observation_id))
            observations.append({
                "repo_id": selection.repo_id,
                "observation_id": selection.observation_id,
                "observed_at": observation.observed_at if observation else None,
            })
        return {
            "chain": observations,
            "root": root,
            "context_sha256": digest,
            "engine": ENGINE_VERSION,
            "note": "results belong to this explicit context; registering newer observations "
                    "does not change them",
        }

    @staticmethod
    def _subject_dict(subject: Optional[ProjectNode], chain: Optional[ChainResult]) -> Optional[Dict[str, object]]:
        if subject is None:
            if chain is not None and chain.nodes:
                node = chain.nodes[-1]
                out: Dict[str, object] = {
                    "coordinate": node.coordinate.as_dict() if node.coordinate else None,
                    "file": node.fact.to_ref(),
                    "partial": True,
                }
                if node.requested_coordinate is not None and node.coordinate is None:
                    out["requested_coordinate"] = node.requested_coordinate.as_dict()
                    out["coordinate_verified"] = False
                return out
            return None
        out: Dict[str, object] = {
            "coordinate": subject.coordinate.as_dict() if subject.coordinate else None,
            "coordinate_verified": subject.coordinate is not None and not subject.parsed.problems,
            "file": subject.fact.to_ref(),
            "groupId_from_parent": subject.group_id_from_parent,
            "version_from_parent": subject.version_from_parent,
        }
        if subject.coordinate is None and subject.requested_coordinate is not None:
            # Blocked chain: the coordinate we were asked for is context, not a
            # verified declaration of this file.
            out["requested_coordinate"] = subject.requested_coordinate.as_dict()
            out["coordinate_verified"] = False
        return out

    def _property_report(
        self, chain: Optional[ChainResult], env: Optional[Interpolator]
    ) -> Dict[str, object]:
        if chain is None:
            return {"effective": [], "lineage": {}}
        subject = chain.subject or (chain.nodes[-1] if chain.nodes else None)
        if subject is None:
            return {"effective": [], "lineage": {}}
        effective = []
        names = set(subject.effective_raw_properties)
        if env is not None:
            names.update(env._cache)
        for name in sorted(names):
            decl = subject.effective_raw_properties.get(name)
            expanded = None
            if env is not None:
                expanded = env.expanded_property(name) or env.evaluate_property(name)
            item: Dict[str, object] = {
                "name": name,
                "raw": decl.raw if decl else None,
                "defined_in": decl.source.to_dict() if decl and decl.source else None,
                "defined_in_project": decl.origin_project if decl else None,
            }
            if expanded is not None:
                item["value"] = expanded.value
                item["resolved_through"] = expanded.chain
                if expanded.problems:
                    item["problems"] = [p.to_dict() for p in expanded.problems]
            effective.append(item)
        lineage = {
            name: entries
            for name, entries in chain.property_lineage.items()
            if len(entries) > 1
        }
        return {"effective": effective, "lineage": lineage}
