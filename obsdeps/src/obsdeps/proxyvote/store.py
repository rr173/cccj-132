"""Immutable SQLite storage for proxy-vote reconciliations.

Each reconciliation is stored under a deterministic ``reconciliation_id``: a
SHA-256 fingerprint of the request (meeting id, requested record date and
cutoff) *and* the exact bytes of the four source documents.  Running the same
inputs again reuses the same id, so results are stable and never duplicated.

The result document is stored as canonical JSON and is readable by a fresh
process with nothing but the database and the read API in this module -- no
source file needs to exist any more.  Stored reconciliations are append-only
(SQLite triggers refuse ``UPDATE``/``DELETE``) and the query layer never
recomputes a verdict: it re-reads the stored document, so a later run cannot
change an earlier answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


STORE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxyvote_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proxyvote_meetings (
    meeting_id TEXT PRIMARY KEY,
    record_date TEXT NOT NULL,
    cutoff_at   TEXT NOT NULL,
    last_reconciliation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proxyvote_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    meeting_id        TEXT NOT NULL,
    record_date       TEXT NOT NULL,
    cutoff_at         TEXT NOT NULL,
    cutoff_utc        TEXT NOT NULL,
    engine_version    TEXT NOT NULL,
    complete          INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    sources_json      TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proxyvote_position_results (
    reconciliation_id TEXT NOT NULL,
    position_id       TEXT NOT NULL,
    holder_id         TEXT NOT NULL,
    status            TEXT NOT NULL,
    result_json       TEXT NOT NULL,
    PRIMARY KEY (reconciliation_id, position_id)
);
CREATE TABLE IF NOT EXISTS proxyvote_proposal_results (
    reconciliation_id TEXT NOT NULL,
    proposal_id       TEXT NOT NULL,
    by_choice_json    TEXT NOT NULL,
    abstain           INTEGER NOT NULL,
    contested         INTEGER NOT NULL,
    invalid_count     INTEGER NOT NULL,
    result_json       TEXT NOT NULL,
    PRIMARY KEY (reconciliation_id, proposal_id)
);
CREATE TRIGGER IF NOT EXISTS proxyvote_reconciliations_immutable_update
BEFORE UPDATE ON proxyvote_reconciliations BEGIN
    SELECT RAISE(ABORT, 'proxy-vote reconciliations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS proxyvote_reconciliations_immutable_delete
BEFORE DELETE ON proxyvote_reconciliations BEGIN
    SELECT RAISE(ABORT, 'proxy-vote reconciliations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS proxyvote_position_results_immutable_update
BEFORE UPDATE ON proxyvote_position_results BEGIN
    SELECT RAISE(ABORT, 'proxy-vote position results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS proxyvote_position_results_immutable_delete
BEFORE DELETE ON proxyvote_position_results BEGIN
    SELECT RAISE(ABORT, 'proxy-vote position results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS proxyvote_proposal_results_immutable_update
BEFORE UPDATE ON proxyvote_proposal_results BEGIN
    SELECT RAISE(ABORT, 'proxy-vote proposal results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS proxyvote_proposal_results_immutable_delete
BEFORE DELETE ON proxyvote_proposal_results BEGIN
    SELECT RAISE(ABORT, 'proxy-vote proposal results are immutable');
END;
"""


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def deterministic_reconciliation_id(meeting_id: str, record_date: str,
                                    cutoff_at: str,
                                    sources: Dict[str, Dict[str, Any]]) -> str:
    """Fingerprint the request plus the exact source bytes (via their digests)."""
    document = {
        "meeting_id": meeting_id,
        "record_date": record_date,
        "cutoff_at": cutoff_at,
        "sources": {name: info.get("sha256", "")
                    for name, info in sorted(sources.items())},
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"pv-{digest[:20]}"


class ProxyVoteStore:
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
            "INSERT OR IGNORE INTO proxyvote_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(STORE_SCHEMA_VERSION)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProxyVoteStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- writing
    def save(self, result: Dict[str, Any], *, sources: Dict[str, Dict[str, Any]],
             created_at: str) -> Dict[str, Any]:
        meeting_id = result["meeting_id"]
        record_date = result["record_date"]
        cutoff_at = result["cutoff_at"]
        reconciliation_id = deterministic_reconciliation_id(
            meeting_id, record_date, cutoff_at, sources)
        payload = canonical_json(result)
        existing = self._conn.execute(
            "SELECT input_fingerprint, payload_json, created_at"
            " FROM proxyvote_reconciliations WHERE reconciliation_id = ?",
            (reconciliation_id,)).fetchone()
        if existing is not None:
            return {
                "status": "already-registered",
                "reconciliation_id": reconciliation_id,
                "created_at": existing["created_at"],
                "complete": bool(json.loads(existing["payload_json"])["complete"]),
            }
        with self._conn:
            self._conn.execute(
                "INSERT INTO proxyvote_reconciliations"
                " (reconciliation_id, meeting_id, record_date, cutoff_at,"
                "  cutoff_utc, engine_version, complete, input_fingerprint,"
                "  sources_json, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reconciliation_id, meeting_id, record_date, cutoff_at,
                 result["cutoff_utc"], result["engine_version"],
                 1 if result["complete"] else 0,
                 reconciliation_id, canonical_json(sources), payload,
                 created_at))
            for position in result["positions"]:
                self._conn.execute(
                    "INSERT INTO proxyvote_position_results"
                    " (reconciliation_id, position_id, holder_id, status,"
                    "  result_json) VALUES (?, ?, ?, ?, ?)",
                    (reconciliation_id, position["position_id"],
                     position["holder_id"], position["status"],
                     canonical_json(position)))
            for proposal in result["proposals"]:
                self._conn.execute(
                    "INSERT INTO proxyvote_proposal_results"
                    " (reconciliation_id, proposal_id, by_choice_json,"
                    "  abstain, contested, invalid_count, result_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (reconciliation_id, proposal["proposal_id"],
                     canonical_json(proposal["by_choice"]),
                     proposal["abstain"], proposal["contested"],
                     proposal["invalid"], canonical_json(proposal)))
            self._conn.execute(
                "INSERT INTO proxyvote_meetings"
                " (meeting_id, record_date, cutoff_at, last_reconciliation_id)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(meeting_id) DO UPDATE SET"
                "  record_date = excluded.record_date,"
                "  cutoff_at = excluded.cutoff_at,"
                "  last_reconciliation_id = excluded.last_reconciliation_id",
                (meeting_id, record_date, cutoff_at, reconciliation_id))
        return {"status": "registered",
                "reconciliation_id": reconciliation_id,
                "created_at": created_at,
                "complete": bool(result["complete"])}

    # ------------------------------------------------------------- reading
    def list_reconciliations(self,
                             meeting_id: Optional[str] = None
                             ) -> List[Dict[str, Any]]:
        if meeting_id is None:
            rows = self._conn.execute(
                "SELECT reconciliation_id, meeting_id, record_date, cutoff_at,"
                "       cutoff_utc, complete, created_at"
                " FROM proxyvote_reconciliations"
                " ORDER BY meeting_id, created_at, reconciliation_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT reconciliation_id, meeting_id, record_date, cutoff_at,"
                "       cutoff_utc, complete, created_at"
                " FROM proxyvote_reconciliations WHERE meeting_id = ?"
                " ORDER BY created_at, reconciliation_id", (meeting_id,)).fetchall()
        return [_reconciliation_row(row) for row in rows]

    def latest(self, meeting_id: str,
               *, record_date: Optional[str] = None,
               cutoff_at: Optional[str] = None
               ) -> Optional[Tuple[str, Dict[str, Any]]]:
        query = ("SELECT reconciliation_id, payload_json"
                 " FROM proxyvote_reconciliations WHERE meeting_id = ?")
        arguments: List[Any] = [meeting_id]
        if record_date is not None:
            query += " AND record_date = ?"
            arguments.append(record_date)
        if cutoff_at is not None:
            query += " AND cutoff_at = ?"
            arguments.append(cutoff_at)
        query += " ORDER BY created_at DESC, reconciliation_id DESC LIMIT 1"
        row = self._conn.execute(query, arguments).fetchone()
        if row is None:
            return None
        return row["reconciliation_id"], json.loads(row["payload_json"])

    def document(self, reconciliation_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT payload_json FROM proxyvote_reconciliations"
            " WHERE reconciliation_id = ?", (reconciliation_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def counts(self) -> Dict[str, int]:
        return {
            table: self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("proxyvote_reconciliations",
                          "proxyvote_position_results",
                          "proxyvote_proposal_results")
        }


def _reconciliation_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "reconciliation_id": row["reconciliation_id"],
        "meeting_id": row["meeting_id"],
        "record_date": row["record_date"],
        "cutoff_at": row["cutoff_at"],
        "cutoff_utc": row["cutoff_utc"],
        "complete": bool(row["complete"]),
        "created_at": row["created_at"],
    }
