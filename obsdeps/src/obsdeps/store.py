"""SQLite storage for immutable observations.

Design rules enforced here:

* An observation is a ``(repo_id, observation_id)`` pair with a complete file
  listing.  Files store the exact observed bytes plus their digest.
* Once registered, nothing about an observation can be updated or deleted.
  SQLite triggers enforce this even if some future code path tries.
* Registering the same manifest again in a different file order is a no-op:
  the manifest's canonical hash (files sorted by path) is the identity.
* Registering *different* content under an existing observation id is an
  error, never an update.  New material belongs in a new observation.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional

from .errors import Problem, Problematic
from .manifest import Manifest, manifest_to_json, sha256_hex

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    repo_id        TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    registered_at  TEXT NOT NULL,
    PRIMARY KEY (repo_id, observation_id)
);
CREATE TABLE IF NOT EXISTS observation_files (
    repo_id              TEXT NOT NULL,
    observation_id       TEXT NOT NULL,
    path                 TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    sha256               TEXT NOT NULL,
    size                 INTEGER NOT NULL,
    declared_digest_algo TEXT,
    declared_digest      TEXT,
    content              BLOB NOT NULL,
    PRIMARY KEY (repo_id, observation_id, path),
    FOREIGN KEY (repo_id, observation_id)
        REFERENCES observations (repo_id, observation_id)
);
CREATE TRIGGER IF NOT EXISTS observations_immutable_update
BEFORE UPDATE ON observations
BEGIN
    SELECT RAISE(ABORT, 'observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observations_immutable_delete
BEFORE DELETE ON observations
BEGIN
    SELECT RAISE(ABORT, 'observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observation_files_immutable_update
BEFORE UPDATE ON observation_files
BEGIN
    SELECT RAISE(ABORT, 'observation files are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observation_files_immutable_delete
BEFORE DELETE ON observation_files
BEGIN
    SELECT RAISE(ABORT, 'observation files are immutable');
END;
"""


@dataclass(frozen=True)
class Observation:
    repo_id: str
    observation_id: str
    observed_at: str
    manifest_sha256: str
    registered_at: str


@dataclass(frozen=True)
class FileFact:
    repo_id: str
    observation_id: str
    path: str
    kind: str
    sha256: str
    size: int
    content: bytes
    declared_digest_algo: Optional[str] = None
    declared_digest: Optional[str] = None

    @property
    def locator(self) -> str:
        return f"{self.repo_id}/{self.observation_id}/{self.path}"

    def locator_at(self, line: Optional[int] = None) -> str:
        if line is None:
            return self.locator
        return f"{self.locator}:{line}"

    def to_ref(self) -> Dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "observation_id": self.observation_id,
            "path": self.path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class FileSummary:
    path: str
    kind: str
    sha256: str
    size: int

    def to_dict(self) -> Dict[str, object]:
        return {"path": self.path, "kind": self.kind, "sha256": self.sha256, "size": self.size}


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        self._file_index: Dict[tuple, Dict[str, FileSummary]] = {}

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- registering
    def register(self, manifest: Manifest, *, registered_at: Optional[str] = None) -> Dict[str, object]:
        """Store an observation.  Idempotent for identical manifests."""
        canonical = manifest_to_json(manifest)
        manifest_hash = sha256_hex(canonical.encode("utf-8"))
        existing = self.get_observation(manifest.repo_id, manifest.observation_id)
        if existing is not None:
            if existing.manifest_sha256 == manifest_hash:
                return {
                    "status": "unchanged",
                    "repo_id": existing.repo_id,
                    "observation_id": existing.observation_id,
                    "observed_at": existing.observed_at,
                    "manifest_sha256": existing.manifest_sha256,
                    "files": self.count_files(existing.repo_id, existing.observation_id),
                }
            raise Problematic(
                Problem(
                    "observation-conflict",
                    (
                        "observation already registered with different content; "
                        "an observation is immutable, register new material under a new observation_id"
                    ),
                    f"observation:{manifest.repo_id}/{manifest.observation_id}",
                    {"existing_manifest_sha256": existing.manifest_sha256, "new_manifest_sha256": manifest_hash},
                )
            )

        stamp = registered_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = sorted(manifest.entries, key=lambda e: e.path)
        with self._conn:
            self._conn.execute(
                "INSERT INTO observations (repo_id, observation_id, observed_at, manifest_sha256, registered_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (manifest.repo_id, manifest.observation_id, manifest.observed_at, manifest_hash, stamp),
            )
            self._conn.executemany(
                "INSERT INTO observation_files"
                " (repo_id, observation_id, path, kind, sha256, size, declared_digest_algo, declared_digest, content)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        manifest.repo_id,
                        manifest.observation_id,
                        entry.path,
                        entry.kind,
                        entry.sha256,
                        len(entry.content),
                        entry.declared_digest[0] if entry.declared_digest else None,
                        entry.declared_digest[1] if entry.declared_digest else None,
                        entry.content,
                    )
                    for entry in entries
                ],
            )
        self._file_index.pop((manifest.repo_id, manifest.observation_id), None)
        return {
            "status": "registered",
            "repo_id": manifest.repo_id,
            "observation_id": manifest.observation_id,
            "observed_at": manifest.observed_at,
            "manifest_sha256": manifest_hash,
            "files": len(entries),
        }

    # ----------------------------------------------------------------- lookup
    def get_observation(self, repo_id: str, observation_id: str) -> Optional[Observation]:
        row = self._conn.execute(
            "SELECT repo_id, observation_id, observed_at, manifest_sha256, registered_at"
            " FROM observations WHERE repo_id = ? AND observation_id = ?",
            (repo_id, observation_id),
        ).fetchone()
        if row is None:
            return None
        return Observation(
            repo_id=row["repo_id"],
            observation_id=row["observation_id"],
            observed_at=row["observed_at"],
            manifest_sha256=row["manifest_sha256"],
            registered_at=row["registered_at"],
        )

    def list_observations(self) -> List[Observation]:
        rows = self._conn.execute(
            "SELECT repo_id, observation_id, observed_at, manifest_sha256, registered_at"
            " FROM observations ORDER BY repo_id, observed_at, observation_id"
        ).fetchall()
        return [
            Observation(r["repo_id"], r["observation_id"], r["observed_at"], r["manifest_sha256"], r["registered_at"])
            for r in rows
        ]

    def count_files(self, repo_id: str, observation_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM observation_files WHERE repo_id = ? AND observation_id = ?",
            (repo_id, observation_id),
        ).fetchone()
        return int(row["n"])

    def file_summaries(self, repo_id: str, observation_id: str) -> Dict[str, FileSummary]:
        key = (repo_id, observation_id)
        cached = self._file_index.get(key)
        if cached is not None:
            return cached
        rows = self._conn.execute(
            "SELECT path, kind, sha256, size FROM observation_files"
            " WHERE repo_id = ? AND observation_id = ? ORDER BY path",
            (repo_id, observation_id),
        ).fetchall()
        index = {
            r["path"]: FileSummary(path=r["path"], kind=r["kind"], sha256=r["sha256"], size=int(r["size"]))
            for r in rows
        }
        self._file_index[key] = index
        return index

    def read_file(self, repo_id: str, observation_id: str, path: str) -> Optional[FileFact]:
        row = self._conn.execute(
            "SELECT repo_id, observation_id, path, kind, sha256, size, content,"
            " declared_digest_algo, declared_digest FROM observation_files"
            " WHERE repo_id = ? AND observation_id = ? AND path = ?",
            (repo_id, observation_id, path),
        ).fetchone()
        if row is None:
            return None
        return self._file(row)

    def files_of_kind(self, repo_id: str, observation_id: str, kind: str) -> List[FileFact]:
        rows = self._conn.execute(
            "SELECT repo_id, observation_id, path, kind, sha256, size, content,"
            " declared_digest_algo, declared_digest FROM observation_files"
            " WHERE repo_id = ? AND observation_id = ? AND kind = ? ORDER BY path",
            (repo_id, observation_id, kind),
        ).fetchall()
        return [self._file(r) for r in rows]

    @staticmethod
    def _file(row: sqlite3.Row) -> FileFact:
        return FileFact(
            repo_id=row["repo_id"],
            observation_id=row["observation_id"],
            path=row["path"],
            kind=row["kind"],
            sha256=row["sha256"],
            size=int(row["size"]),
            content=bytes(row["content"]),
            declared_digest_algo=row["declared_digest_algo"],
            declared_digest=row["declared_digest"],
        )
