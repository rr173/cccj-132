"""Public Python API.

Example::

    from obsdeps import Api

    api = Api("data/obsdeps.sqlite3")
    api.register_manifest("bundle/obs-r1-2024-01-01.json")
    result = api.resolve(
        chain=[{"repo_id": "R1", "observation_id": "obs-r1-2024-01-01"}],
        coordinate={"groupId": "com.acme", "artifactId": "child", "version": "1.0"},
    )
    print(result["status"], [d["version"]["resolved"] for d in result["dependencies"]])

Every ``resolve`` returns a plain JSON-serialisable dict.  Input problems are
reported in the result where they belong; only malformed *calls* raise
:class:`UsageError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .compare import compare_results
from .engine import Resolver
from .errors import Problem, Problematic, UsageError
from .manifest import Manifest, ManifestError, ensure_registrable, load_manifest_file, load_manifest_text
from .pom import Coordinate
from .resolution import ENGINE_VERSION, RepoSelection, ResolutionRequest, RootSpec, ParseCache
from .store import Store

__all__ = ["Api", "ManifestError", "Problem", "Problematic", "UsageError", "ENGINE_VERSION"]


def _as_dict(value: Any, what: str) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()  # type: ignore[no-any-return]
    raise UsageError(f"{what} must be a mapping or an object with as_dict()")


def _coordinate_from(value: Any, what: str = "coordinate") -> Coordinate:
    data = _as_dict(value, what)
    try:
        group = data["groupId"]
        artifact = data["artifactId"]
        version = data["version"]
    except KeyError as exc:
        raise UsageError(f"{what} needs groupId, artifactId and version ({exc})")
    for label, item in (("groupId", group), ("artifactId", artifact), ("version", version)):
        if not isinstance(item, str) or not item.strip():
            raise UsageError(f"{what}.{label} must be a non-empty string")
    return Coordinate(group.strip(), artifact.strip(), version.strip())


def _chain_from(value: Any) -> Tuple[RepoSelection, ...]:
    if value is None:
        raise UsageError("a resolution request needs an explicit 'chain'")
    if not isinstance(value, (list, tuple)) or not value:
        raise UsageError("'chain' must be a non-empty list of {repo_id, observation_id}")
    selections: List[RepoSelection] = []
    for index, item in enumerate(value):
        data = _as_dict(item, f"chain[{index}]")
        repo_id = data.get("repo_id")
        observation_id = data.get("observation_id")
        if not isinstance(repo_id, str) or not repo_id.strip():
            raise UsageError(f"chain[{index}].repo_id must be a non-empty string")
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise UsageError(f"chain[{index}].observation_id must be a non-empty string")
        selections.append(RepoSelection(repo_id.strip(), observation_id.strip()))
    return tuple(selections)


def _root_from(coordinate: Any, observation: Any, path: Any) -> RootSpec:
    if coordinate is not None:
        return RootSpec(coordinate=_coordinate_from(coordinate, "root coordinate"))
    if path is None:
        raise UsageError("a request needs either 'coordinate' or 'pom'/'observation'+'path'")
    if not isinstance(path, str) or not path.strip():
        raise UsageError("'pom' must be a non-empty repository path")
    if observation is None:
        raise UsageError("identifying a root POM file needs 'observation' {repo_id, observation_id}")
    data = _as_dict(observation, "observation")
    repo_id = data.get("repo_id")
    observation_id = data.get("observation_id")
    if not isinstance(repo_id, str) or not isinstance(observation_id, str):
        raise UsageError("observation needs repo_id and observation_id")
    return RootSpec(file=(repo_id, observation_id, path.strip()))


class Api:
    """Facade over :class:`Store`, the resolver and the comparison."""

    def __init__(self, database: str = ":memory:", *, cache: Optional[ParseCache] = None):
        self.store = Store(database)
        self.cache = cache if cache is not None else ParseCache()

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Api":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- register
    def register(
        self,
        manifest: Union[Manifest, str, Dict[str, Any]],
        *,
        registered_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register one observation from a manifest object, file path or JSON text."""
        loaded = self._load_manifest(manifest)
        ensure_registrable(loaded)
        return self.store.register(loaded, registered_at=registered_at)

    def register_manifest(self, path: str) -> Dict[str, Any]:
        return self.register(load_manifest_file(path))

    def register_manifest_text(self, text: str, origin: str = "<manifest>") -> Dict[str, Any]:
        return self.register(load_manifest_text(text, origin))

    @staticmethod
    def _load_manifest(manifest: Union[Manifest, str, Dict[str, Any]]) -> Manifest:
        if isinstance(manifest, Manifest):
            return manifest
        if isinstance(manifest, dict):
            return load_manifest_text(json.dumps(manifest), "<manifest object>")
        if isinstance(manifest, str):
            stripped = manifest.lstrip()
            if stripped.startswith("{"):
                return load_manifest_text(manifest, "<manifest text>")
            return load_manifest_file(manifest)
        raise UsageError("register() needs a Manifest, a manifest path or JSON text")

    # -------------------------------------------------------------- listing
    def observations(self) -> List[Dict[str, Any]]:
        return [
            {
                "repo_id": observation.repo_id,
                "observation_id": observation.observation_id,
                "observed_at": observation.observed_at,
                "manifest_sha256": observation.manifest_sha256,
                "registered_at": observation.registered_at,
                "files": self.store.count_files(observation.repo_id, observation.observation_id),
            }
            for observation in self.store.list_observations()
        ]

    def files(self, repo_id: str, observation_id: str) -> List[Dict[str, Any]]:
        summaries = self.store.file_summaries(repo_id, observation_id)
        return [summaries[path].to_dict() for path in sorted(summaries)]

    # -------------------------------------------------------------- resolve
    def resolve(
        self,
        *,
        chain: Sequence[Dict[str, str]],
        coordinate: Optional[Dict[str, str]] = None,
        observation: Optional[Dict[str, str]] = None,
        pom: Optional[str] = None,
        raise_on_problem: bool = False,
    ) -> Dict[str, Any]:
        request = self._request(chain=chain, coordinate=coordinate,
                                observation=observation, pom=pom)
        result = Resolver(self.store, request, self.cache).run()
        if raise_on_problem and result["status"] in ("invalid", "blocked"):
            raise Problematic(Problem(
                "resolution-failed",
                f"resolution status is {result['status']}",
                f"context:{result['context'].get('context_sha256')}",
                {"status": result["status"], "problems": result["problems"][:5],
                 "missing": result["missing"][:5]},
            ))
        return result

    # ---------------------------------------------------------------- explain
    def explain(
        self,
        *,
        chain: Sequence[Dict[str, str]],
        dependency: Optional[Dict[str, str]] = None,
        coordinate: Optional[Dict[str, str]] = None,
        observation: Optional[Dict[str, str]] = None,
        pom: Optional[str] = None,
        raise_on_problem: bool = False,
    ) -> Dict[str, Any]:
        """Resolution plus a focused, ordered narrative for one dependency.

        The full resolution is returned under ``resolution``; ``explanation``
        shows the step chain from observed file to final version and the files
        selected for the artifact.
        """
        result = self.resolve(chain=chain, coordinate=coordinate, observation=observation,
                              pom=pom, raise_on_problem=raise_on_problem)
        selected = None
        if dependency is not None:
            wanted = self._dep_key_from(dependency)
            for entry in result["dependencies"]:
                entry_key = entry.get("key", {}).get("key")
                if entry_key == wanted:
                    selected = entry
                    break
            if selected is None:
                raise Problematic(Problem(
                    "dependency-not-in-result",
                    f"{wanted} is not one of the subject's direct dependencies",
                    f"context:{result['context'].get('context_sha256')}",
                    {
                        "requested": wanted,
                        "available": [e.get("key", {}).get("key") for e in result["dependencies"]],
                    },
                ))
        explanation = self._explain_dependency(result, selected) if selected else None
        return {
            "context": result["context"],
            "status": result["status"],
            "subject": result["subject"],
            "explanation": explanation,
            "resolution": result,
        }

    @staticmethod
    def _dep_key_from(dependency: Dict[str, str]) -> str:
        data = _as_dict(dependency, "dependency")
        group = data.get("groupId")
        artifact = data.get("artifactId")
        if not group or not artifact:
            raise UsageError("dependency needs groupId and artifactId")
        dep_type = data.get("type") or "jar"
        classifier = data.get("classifier", "")
        return f"{group}:{artifact}:{dep_type}:{classifier}"

    @staticmethod
    def _explain_dependency(result: Dict[str, Any], dependency: Dict[str, Any]) -> Dict[str, Any]:
        version = dependency.get("version", {})
        evidence = version.get("evidence", {})
        steps: List[Dict[str, Any]] = []

        subject = result.get("subject") or {}
        steps.append({
            "step": 1,
            "fact": "subject POM observed in the requested context",
            "source": subject.get("file"),
            "coordinate": subject.get("coordinate"),
        })
        for depth, node in enumerate(result.get("parent_chain", [])):
            if node.get("parent") is None:
                continue
            steps.append({
                "step": len(steps) + 1,
                "fact": "parent edge selected by the repository search chain",
                "depth": depth,
                "child": node["parent"].get("child"),
                "parent_coordinate": node["parent"].get("parent_coordinate"),
                "selected": node["parent"].get("selected"),
                "search_trail": node["parent"].get("search_trail"),
            })
        steps.append({
            "step": len(steps) + 1,
            "fact": "direct dependency declaration",
            "source": dependency.get("declaration"),
            "raw_version": version.get("raw"),
            "key": dependency.get("key"),
        })
        kind = version.get("source")
        if kind == "direct":
            steps.append({
                "step": len(steps) + 1,
                "fact": "the direct dependency states its own version, so no management "
                        "entry is consulted",
                "resolved": version.get("resolved"),
                "properties": evidence.get("properties", []),
            })
        elif kind == "managed":
            steps.append({
                "step": len(steps) + 1,
                "fact": "no explicit version; the effective dependencyManagement entry for the "
                        "full key supplies it",
                "lookup_key": evidence.get("lookup_key"),
                "managed_from": evidence.get("managed_from"),
                "raw_version": evidence.get("raw_version"),
                "resolved": evidence.get("resolved_version"),
                "declaring_project": evidence.get("declaring_project"),
                "properties": evidence.get("properties", []),
                "overrides": evidence.get("overrides", []),
            })
        else:
            steps.append({
                "step": len(steps) + 1,
                "fact": "no explicit version and no usable management entry; unresolved "
                        "(this subset does not choose a repository version)",
                "reason": evidence.get("reason"),
                "lookup_key": evidence.get("lookup_key"),
            })

        files = []
        for item in dependency.get("files", []):
            row = {
                "role": item.get("role"),
                "extension": item.get("extension"),
                "classifier": item.get("classifier"),
                "expected_path": item.get("expected_path"),
                "observed": item.get("observed"),
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "repo_id": item.get("repo_id"),
                "observation_id": item.get("observation_id"),
            }
            if item.get("snapshot"):
                row["snapshot"] = item["snapshot"]
            if item.get("digest_check"):
                row["digest_check"] = item["digest_check"]
            if item.get("problem"):
                row["problem"] = item["problem"]
            files.append(row)
        if files:
            steps.append({
                "step": len(steps) + 1,
                "fact": "artifact files selected independently (POM, main artifact, every "
                        "classifier)",
                "files": files,
            })
        return {
            "dependency": dependency.get("key", {}).get("key"),
            "status": dependency.get("status"),
            "final_version": version.get("resolved"),
            "version_source": kind,
            "steps": steps,
            "problems": dependency.get("problems", []),
            "missing": dependency.get("missing", []),
            "unsupported": dependency.get("unsupported", []),
        }

    # -------------------------------------------------------------- compare
    def compare(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        """Compare two explicit resolution requests (not two database states)."""
        result_a = self.resolve(**dict(left))
        result_b = self.resolve(**dict(right))
        return compare_results(result_a, result_b)

    def compare_resolved(self, result_a: Dict[str, Any], result_b: Dict[str, Any]) -> Dict[str, Any]:
        return compare_results(result_a, result_b)

    # --------------------------------------------------------------- helper
    def _request(
        self,
        *,
        chain: Sequence[Dict[str, str]],
        coordinate: Optional[Dict[str, str]],
        observation: Optional[Dict[str, str]],
        pom: Optional[str],
    ) -> ResolutionRequest:
        selections = _chain_from(chain)
        root = _root_from(coordinate, observation, pom)
        return ResolutionRequest(chain=selections, root=root)
