"""Manifest loading, hashing and registration planning.

The input manifest is data, not code.  Every listed file is read exactly once,
hashed immediately and stored as bytes inside the new observation, so later
changes to the manifest directory cannot change what an observation means.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .errors import Problem

# ---------------------------------------------------------------------------
# File classification.  Anything else is still recorded (raw bytes + digest)
# but has no semantic role, which is reported as ``unsupported-file``.
# ---------------------------------------------------------------------------

DIGEST_RE = re.compile(r"^(?:sha1|sha256|md5|hash):([0-9a-fA-F]+)$")
PATH_RE = re.compile(r"^(?:path:)?(.+)$")
MAVEN_METADATA_NAME = "maven-metadata.xml"


def classify_path(path: str) -> str:
    """Return ``pom``, ``metadata`` or ``other`` for a repository-relative path."""
    if os.path.basename(path) == MAVEN_METADATA_NAME:
        return "metadata"
    if path.lower().endswith(".pom"):
        return "pom"
    return "other"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    """One observed file fact (before the observation row exists)."""

    path: str            # repository-relative path, '/' separated
    sha256: str          # actual digest of the stored bytes
    content: bytes
    declared_digest: Optional[Tuple[str, str]] = None   # (algo, hex) from manifest

    @property
    def kind(self) -> str:
        return classify_path(self.path)

    def problem(self) -> Optional[Problem]:
        """Digest syntax / mismatch problems for this entry."""
        if self.declared_digest is not None:
            algo, expected = self.declared_digest
            actual = hashlib.new(algo, self.content).hexdigest()
            if actual.lower() != expected.lower():
                return Problem(
                    code="digest-mismatch",
                    message=(
                        f"manifest declares {algo}:{expected.lower()} but the file content "
                        f"has {algo}:{actual.lower()}"
                    ),
                    locator=f"manifest:{self.path}",
                    details={"path": self.path, "declared": expected.lower(), "actual": actual.lower()},
                )
        if self.sha256 != sha256_hex(self.content):
            return Problem(
                code="internal-digest-drift",
                message="registered digest does not match the stored bytes",
                locator=f"observation-file:{self.path}",
            )
        return None


@dataclass(frozen=True)
class Manifest:
    repo_id: str
    observation_id: str
    observed_at: str
    entries: Tuple[ManifestEntry, ...]


class ManifestError(Exception):
    def __init__(self, problems: List[Problem]):
        super().__init__("\n".join(p.render() for p in problems))
        self.problems = problems


def _normalize_observed_at(value: str) -> str:
    return value.strip()


def _parse_document(raw: str, origin: str) -> Tuple[str, List[Dict[str, str]]]:
    """Parse manifest JSON into (repo_id, observation_id, observed_at, list)."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError([
            Problem("manifest-invalid-json", f"cannot parse manifest JSON: {exc}", origin)
        ])
    if not isinstance(doc, dict):
        raise ManifestError([Problem("manifest-invalid", "manifest root must be an object", origin)])

    problems: List[Problem] = []
    observation = doc.get("observation", doc)
    if not isinstance(observation, dict):
        problems.append(Problem("manifest-invalid", "'observation' must be an object", origin))
        raise ManifestError(problems)

    repo_id = observation.get("repo_id")
    observation_id = observation.get("observation_id")
    observed_at = observation.get("observed_at")
    for name, value in (
        ("repo_id", repo_id),
        ("observation_id", observation_id),
        ("observed_at", observed_at),
    ):
        if not isinstance(value, str) or not value.strip():
            problems.append(
                Problem(
                    "manifest-missing-field",
                    f"observation needs a non-empty string {name!r}",
                    origin,
                    {"field": name},
                )
            )

    files = doc.get("files")
    if not isinstance(files, list):
        problems.append(Problem("manifest-missing-field", "'files' must be a list", origin, {"field": "files"}))
        raise ManifestError(problems)
    if problems:
        raise ManifestError(problems)

    seen: Dict[str, int] = {}
    entries: List[ManifestEntry] = []
    for index, item in enumerate(files):
        loc = f"{origin}#files/{index}"
        if not isinstance(item, dict):
            problems.append(Problem("manifest-entry-invalid", "file entry must be an object", loc))
            continue
        raw_path = item.get("path")
        raw_digest = item.get("digest", item.get("sha256", item.get("sha1")))
        if not isinstance(raw_path, str) or not raw_path.strip():
            problems.append(Problem("manifest-entry-invalid", "file entry needs a 'path' string", loc))
            continue

        match = PATH_RE.match(raw_path.strip())
        assert match is not None
        path = _normalize_repo_path(match.group(1), loc, problems)
        if path is None:
            continue
        declared = _parse_digest(raw_digest, loc, problems)
        content = _read_entry_bytes(item, path, loc, problems)
        if content is None:
            continue

        digest = sha256_hex(content)
        if path in seen:
            prev = entries[seen[path]]
            if prev.sha256 != digest:
                problems.append(
                    Problem(
                        "manifest-duplicate-path",
                        f"path {path!r} listed twice with different content",
                        loc,
                        {"path": path, "first": prev.sha256, "second": digest},
                    )
                )
                continue
            continue  # exact duplicate entry, harmless
        seen[path] = len(entries)
        entries.append(
            ManifestEntry(path=path, sha256=digest, content=content, declared_digest=declared)
        )

    if not entries and not problems:
        problems.append(Problem("manifest-empty", "manifest lists no files", origin))
    if problems:
        raise ManifestError(problems)
    return repo_id.strip(), observation_id.strip(), _normalize_observed_at(observed_at), entries


def _normalize_repo_path(path: str, loc: str, problems: List[Problem]) -> Optional[str]:
    stripped = path.strip().replace("\\", "/")
    while stripped.startswith("./"):
        stripped = stripped[2:]
    if stripped.startswith("/"):
        problems.append(Problem("manifest-path-invalid", "path must be repository-relative", loc))
        return None
    parts = [p for p in stripped.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        problems.append(Problem("manifest-path-invalid", f"invalid repository path {path!r}", loc))
        return None
    return "/".join(parts)


def _parse_digest(raw: object, loc: str, problems: List[Problem]) -> Optional[Tuple[str, str]]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        problems.append(Problem("manifest-digest-invalid", "digest must be a string", loc))
        return None
    value = raw.strip()
    match = DIGEST_RE.match(value) or re.match(r"^([0-9a-fA-F]+)$", value)
    if not match:
        problems.append(
            Problem(
                "manifest-digest-invalid",
                "digest must look like 'sha256:<hex>', 'sha1:<hex>' or bare hex",
                loc,
                {"digest": raw},
            )
        )
        return None
    if match.lastindex == 2:  # algo:hex form
        algo = match.group(1).lower()
        expected_len = {"sha1": 40, "sha256": 64, "md5": 32}.get(algo)
        if expected_len is not None and len(match.group(2)) != expected_len:
            problems.append(
                Problem("manifest-digest-invalid", f"{algo} digest has wrong length", loc, {"digest": raw})
            )
            return None
        return algo, match.group(2)
    bare = match.group(1)
    algo = {40: "sha1", 64: "sha256", 32: "md5"}.get(len(bare))
    if algo is None:
        problems.append(
            Problem("manifest-digest-invalid", "bare digest length is not sha1/sha256/md5", loc, {"digest": raw})
        )
        return None
    return algo, bare


def _read_entry_bytes(
    item: Dict[str, object], path: str, loc: str, problems: List[Problem]
) -> Optional[bytes]:
    data = item.get("content")
    if data is not None:
        if not isinstance(data, str):
            problems.append(
                Problem("manifest-entry-invalid", "'content' must be a string (UTF-8 text)", loc)
            )
            return None
        return data.encode("utf-8")
    content_file = item.get("content_file", item.get("source"))
    if not isinstance(content_file, str) or not content_file:
        problems.append(
            Problem(
                "manifest-entry-invalid",
                "file entry needs 'content' (inline text) or 'content_file' (path)",
                loc,
            )
        )
        return None
    if not os.path.isfile(content_file):
        problems.append(
            Problem(
                "manifest-content-missing",
                f"content_file does not exist: {content_file}",
                loc,
                {"content_file": content_file},
            )
        )
        return None
    with open(content_file, "rb") as handle:
        return handle.read()


def load_manifest_text(raw: str, origin: str) -> Manifest:
    repo_id, observation_id, observed_at, entries = _parse_document(raw, origin)
    return Manifest(repo_id, observation_id, observed_at, tuple(entries))


def load_manifest_file(path: str) -> Manifest:
    if not os.path.isfile(path):
        raise ManifestError([Problem("manifest-missing", f"manifest file not found: {path}", path)])
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    return load_manifest_text(raw, path)


def manifest_problems(manifest: Manifest) -> List[Problem]:
    """Digest problems that must abort registration before anything is stored."""
    problems = []
    for entry in manifest.entries:
        problem = entry.problem()
        if problem is not None:
            problems.append(problem)
    return problems


def manifest_to_json(manifest: Manifest) -> str:
    """Canonical rendering, used for hashing and documentation only.

    Registration order never depends on this text; it is sorted by path.
    """
    doc = {
        "repo_id": manifest.repo_id,
        "observation_id": manifest.observation_id,
        "observed_at": manifest.observed_at,
        "files": [
            {
                "path": entry.path,
                "sha256": entry.sha256,
            }
            for entry in sorted(manifest.entries, key=lambda e: e.path)
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def ensure_registrable(manifest: Manifest) -> None:
    problems = manifest_problems(manifest)
    if problems:
        raise ManifestError(problems)
