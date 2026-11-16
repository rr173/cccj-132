"""SQLite storage and the current-state derivation for station observations.

Two kinds of data live here:

1. **Evidence (append-only per run)** -- every accepted station observation
   with its run, provider, page sequence and original field locator, every
   rejected record with its row position, every page-level failure with the
   cursor that produced it, and explicit canonical-id conflicts. A repeated run
   under the same ``run_id`` replaces *that run's* evidence atomically inside
   one transaction; other runs are untouched.
2. **Derived current state (``station_current``)** -- rebuilt from *all* stored
   observations. Page arrival order never decides a value: per
   ``(provider_id, station_id)`` only observations carrying the newest
   observation timestamp can define the current state. Tied-newest records that
   disagree on status or connector counts put the station in ``CONFLICT`` with
   every tied record retained as evidence instead of picking one.

Identity rules enforced here:

* a provider station id is only meaningful inside its provider -- the primary
  identity is ``(provider_id, station_id)``;
* two providers merge into one queryable group *only* through an identical,
  explicit canonical id; missing canonical ids never merge, even when name and
  address are equal;
* the same provider station id observed with contradictory explicit canonical
  ids is recorded in ``station_canonical_conflicts`` and never silently rebound.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..errors import Problem
from .adapters import NormalizedStation
from .engine import CollectionResult, SourceRun

STORE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_sources (
    provider_id  TEXT PRIMARY KEY,
    endpoint     TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_run_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_runs (
    run_id           TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    source_count     INTEGER NOT NULL,
    complete_count   INTEGER NOT NULL,
    incomplete_count INTEGER NOT NULL,
    status           TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_run_sources (
    run_id            TEXT NOT NULL,
    provider_id       TEXT NOT NULL,
    endpoint          TEXT NOT NULL,
    adapter_type      TEXT NOT NULL,
    status            TEXT NOT NULL,
    page_count        INTEGER NOT NULL,
    stations_accepted INTEGER NOT NULL,
    records_rejected  INTEGER NOT NULL,
    page_problem_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, provider_id)
);
CREATE TABLE IF NOT EXISTS station_pages (
    run_id           TEXT NOT NULL,
    provider_id      TEXT NOT NULL,
    page_seq         INTEGER NOT NULL,
    request_cursor   TEXT,
    next_cursor      TEXT,
    stations_accepted INTEGER NOT NULL,
    records_rejected INTEGER NOT NULL,
    PRIMARY KEY (run_id, provider_id, page_seq)
);
CREATE TABLE IF NOT EXISTS station_observations (
    observation_id TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    provider_id    TEXT NOT NULL,
    station_id     TEXT NOT NULL,
    obs_seq        INTEGER NOT NULL,
    canonical_id   TEXT,
    name           TEXT NOT NULL,
    address        TEXT NOT NULL,
    latitude       REAL NOT NULL,
    longitude      REAL NOT NULL,
    observed_at    TEXT NOT NULL,
    status         TEXT NOT NULL,
    connectors_json TEXT NOT NULL,
    raw_locator    TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    UNIQUE (run_id, provider_id, station_id, obs_seq)
);
CREATE TABLE IF NOT EXISTS station_record_problems (
    problem_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    provider_id  TEXT NOT NULL,
    page_seq     INTEGER NOT NULL,
    raw_locator  TEXT NOT NULL,
    code         TEXT NOT NULL,
    message      TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_page_problems (
    problem_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    provider_id  TEXT NOT NULL,
    page_seq     INTEGER NOT NULL,
    cursor       TEXT,
    code         TEXT NOT NULL,
    message      TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_canonical_conflicts (
    conflict_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    provider_id  TEXT NOT NULL,
    station_id   TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    raw_locator  TEXT NOT NULL,
    UNIQUE (run_id, provider_id, station_id, canonical_id, raw_locator)
);
CREATE TABLE IF NOT EXISTS station_current (
    station_key        TEXT PRIMARY KEY,
    provider_id        TEXT NOT NULL,
    station_id         TEXT NOT NULL,
    canonical_id       TEXT,
    name               TEXT NOT NULL,
    address            TEXT NOT NULL,
    latitude           REAL NOT NULL,
    longitude          REAL NOT NULL,
    status             TEXT NOT NULL,
    connectors_json    TEXT NOT NULL,
    conflict           INTEGER NOT NULL,
    conflict_reasons_json TEXT NOT NULL,
    latest_observed_at TEXT NOT NULL,
    evidence_count     INTEGER NOT NULL,
    evidence_ids_json  TEXT NOT NULL,
    UNIQUE (provider_id, station_id)
);
"""

CONFLICT_STATUS = "CONFLICT"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def station_key(provider_id: str, station_id: str) -> str:
    return f"{provider_id}:{station_id}"


def station_to_dict(station: NormalizedStation) -> Dict[str, Any]:
    return {
        "provider_id": station.provider_id,
        "station_id": station.station_id,
        "canonical_id": station.canonical_id,
        "name": station.name,
        "address": station.address,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "observed_at": station.observed_at,
        "status": station.status,
        "connectors": [
            {"type": t, "available": a, "total": n}
            for (t, a, n) in station.connectors
        ],
        "raw_locator": station.raw_locator,
    }


def deterministic_run_id(result: CollectionResult) -> str:
    """Fingerprint exactly the evidence the run observed.

    The same fixture re-collected therefore yields the same run id, so the
    replacement semantics give byte-stable reruns.
    """
    document = {
        "sources": [
            {
                "provider_id": run.provider_id,
                "endpoint": run.endpoint,
                "adapter_type": run.adapter_type,
                "status": run.status,
                "stations": [station_to_dict(s) for s in run.stations],
                "record_problems": [p.to_dict() for p in run.record_problems],
                "page_problems": [p.to_dict() for p in run.page_problems],
                "pages": [
                    {"page_seq": p.page_seq, "request_cursor": p.request_cursor,
                     "next_cursor": p.next_cursor,
                     "stations_accepted": p.stations_accepted,
                     "records_rejected": p.records_rejected}
                    for p in run.pages
                ],
            }
            for run in result.sources
        ]
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"run-{digest[:16]}"


class StationStore:
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
            "INSERT OR IGNORE INTO station_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(STORE_SCHEMA_VERSION)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StationStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- writing
    def save_run(self, result: CollectionResult, *, run_id: str,
                 started_at: Optional[str] = None) -> Dict[str, Any]:
        stamp = started_at or utc_stamp()
        fingerprint = deterministic_run_id(result)
        complete = sum(1 for s in result.sources if s.status == "complete")
        incomplete = len(result.sources) - complete
        with self._conn:
            self._replace_run_scoped_rows(run_id)
            self._conn.execute(
                "INSERT INTO station_runs (run_id, started_at, source_count,"
                " complete_count, incomplete_count, status, input_fingerprint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, stamp, len(result.sources), complete, incomplete,
                 "complete" if incomplete == 0 else "incomplete",
                 fingerprint))
            for run in result.sources:
                self._save_source_run(run, run_id, stamp)
            self._rebuild_current()
        return {
            "run_id": run_id,
            "started_at": stamp,
            "input_fingerprint": fingerprint,
            **result.summary(),
        }

    def _replace_run_scoped_rows(self, run_id: str) -> None:
        for table in ("station_run_sources", "station_pages",
                      "station_observations", "station_record_problems",
                      "station_page_problems", "station_canonical_conflicts"):
            self._conn.execute(f"DELETE FROM {table} WHERE run_id = ?",
                               (run_id,))
        self._conn.execute("DELETE FROM station_runs WHERE run_id = ?",
                           (run_id,))

    def _save_source_run(self, run: SourceRun, run_id: str, stamp: str) -> None:
        self._conn.execute(
            "INSERT INTO station_run_sources (run_id, provider_id, endpoint,"
            " adapter_type, status, page_count, stations_accepted,"
            " records_rejected, page_problem_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, run.provider_id, run.endpoint, run.adapter_type,
             run.status, len(run.pages), len(run.stations),
             len(run.record_problems), len(run.page_problems)))
        self._conn.execute(
            "INSERT INTO station_sources (provider_id, endpoint, adapter_type,"
            " first_seen_at, last_run_id) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(provider_id) DO UPDATE SET endpoint=excluded.endpoint,"
            " adapter_type=excluded.adapter_type, last_run_id=excluded.last_run_id",
            (run.provider_id, run.endpoint, run.adapter_type, stamp, run_id))

        for page in run.pages:
            self._conn.execute(
                "INSERT INTO station_pages (run_id, provider_id, page_seq,"
                " request_cursor, next_cursor, stations_accepted,"
                " records_rejected) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, run.provider_id, page.page_seq, page.request_cursor,
                 page.next_cursor, page.stations_accepted,
                 page.records_rejected))

        seq_counter: Dict[Tuple[str, str], int] = {}
        for station in run.stations:
            key = (station.provider_id, station.station_id)
            seq_counter[key] = seq_counter.get(key, 0) + 1
            obs_seq = seq_counter[key]
            observation_id = (f"{run_id}:{station.provider_id}:"
                              f"{station.station_id}:{obs_seq}")
            self._conn.execute(
                "INSERT INTO station_observations (observation_id, run_id,"
                " provider_id, station_id, obs_seq, canonical_id, name,"
                " address, latitude, longitude, observed_at, status,"
                " connectors_json, raw_locator, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (observation_id, run_id, station.provider_id,
                 station.station_id, obs_seq, station.canonical_id,
                 station.name, station.address, station.latitude,
                 station.longitude, station.observed_at, station.status,
                 canonical_json([
                     {"type": t, "available": a, "total": n}
                     for (t, a, n) in station.connectors]),
                 station.raw_locator,
                 canonical_json(station_to_dict(station))))

        for problem in run.record_problems:
            self._insert_problem("station_record_problems", run_id,
                                 run, problem)
        for problem in run.page_problems:
            self._insert_problem("station_page_problems", run_id,
                                 run, problem,
                                 cursor=problem.details.get("cursor"))

        # Canonical-id contradictions observed inside this single run. The
        # cross-run view is recomputed separately in _rebuild_current.
        canonical_seen: Dict[Tuple[str, str], Dict[str, NormalizedStation]] = {}
        for station in run.stations:
            if station.canonical_id is None:
                continue
            bucket = canonical_seen.setdefault(
                (station.provider_id, station.station_id), {})
            if station.canonical_id not in bucket:
                bucket[station.canonical_id] = station
        for (provider_id, station_id), variants in canonical_seen.items():
            if len(variants) <= 1:
                continue
            for canonical_id, station in sorted(variants.items()):
                self._conn.execute(
                    "INSERT OR IGNORE INTO station_canonical_conflicts"
                    " (run_id, provider_id, station_id, canonical_id,"
                    " observed_at, raw_locator) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, provider_id, station_id, canonical_id,
                     station.observed_at, station.raw_locator))

    def _insert_problem(self, table: str, run_id: str, run: SourceRun,
                        problem: Problem, *, cursor: Optional[str] = None) -> None:
        page_seq = problem.details.get("page_seq")
        if page_seq is None:
            # Record problems locate the page through their raw locator:
            # "provider/pageN/...".
            page_seq = self._page_seq_from_locator(problem.locator)
        if table == "station_record_problems":
            self._conn.execute(
                "INSERT INTO station_record_problems (run_id, provider_id,"
                " page_seq, raw_locator, code, message, details_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, run.provider_id, page_seq, problem.locator,
                 problem.code, problem.message,
                 canonical_json(problem.details)))
        else:
            self._conn.execute(
                "INSERT INTO station_page_problems (run_id, provider_id,"
                " page_seq, cursor, code, message, details_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, run.provider_id, page_seq, cursor, problem.code,
                 problem.message, canonical_json(problem.details)))

    @staticmethod
    def _page_seq_from_locator(locator: str) -> int:
        try:
            fragment = locator.split("/", 2)[1]
            return int(fragment.replace("page", ""))
        except (IndexError, ValueError):
            return 0

    # ------------------------------------------------------------- derive
    def _rebuild_current(self) -> None:
        """Recompute station_current from every stored observation."""
        self._conn.execute("DELETE FROM station_current")
        rows = self._conn.execute(
            "SELECT * FROM station_observations"
            " ORDER BY provider_id, station_id, observed_at, observation_id"
        ).fetchall()
        grouped: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault((row["provider_id"], row["station_id"]),
                               []).append(row)

        for (provider_id, station_id), observations in sorted(grouped.items()):
            latest = max(o["observed_at"] for o in observations)
            winners = [o for o in observations if o["observed_at"] == latest]
            state_signatures = {
                (o["status"], o["connectors_json"]) for o in winners
            }
            reasons: List[str] = []
            state_conflict = len(state_signatures) > 1
            if state_conflict:
                reasons.append("same-timestamp-state-conflict")

            explicit_canonicals = sorted({
                o["canonical_id"] for o in observations
                if o["canonical_id"] is not None
            })
            canonical_conflict = len(explicit_canonicals) > 1
            if canonical_conflict:
                reasons.append("canonical-id-conflict")
            canonical_id = (explicit_canonicals[0]
                            if len(explicit_canonicals) == 1 else None)

            representative = winners[0]  # deterministic: ordered query
            if state_conflict:
                status = CONFLICT_STATUS
                connectors_json = canonical_json([])
            else:
                status = representative["status"]
                connectors_json = representative["connectors_json"]

            self._conn.execute(
                "INSERT INTO station_current (station_key, provider_id,"
                " station_id, canonical_id, name, address, latitude,"
                " longitude, status, connectors_json, conflict,"
                " conflict_reasons_json, latest_observed_at, evidence_count,"
                " evidence_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (station_key(provider_id, station_id), provider_id,
                 station_id, canonical_id, representative["name"],
                 representative["address"], representative["latitude"],
                 representative["longitude"], status, connectors_json,
                 1 if reasons else 0, canonical_json(reasons), latest,
                 len(winners),
                 canonical_json([o["observation_id"] for o in winners])))

    # ------------------------------------------------------------- reading
    def list_stations(self, *, provider_id: Optional[str] = None,
                      status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM station_current WHERE 1=1"
        params: List[Any] = []
        if provider_id is not None:
            sql += " AND provider_id = ?"
            params.append(provider_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY provider_id, station_id"
        return [self._current_dict(row)
                for row in self._conn.execute(sql, params).fetchall()]

    def get_station(self, provider_id: str, station_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM station_current WHERE provider_id = ?"
            " AND station_id = ?", (provider_id, station_id)).fetchone()
        if row is None:
            return None
        document = self._current_dict(row)
        document["evidence"] = self._evidence(row["evidence_ids_json"])
        document["observation_history"] = self._all_observations(
            provider_id, station_id)
        document["canonical_conflicts"] = self._canonical_conflicts(
            provider_id, station_id)
        # Canonical variants are also derivable from the raw observations, so
        # a contradiction spread across separate runs still has evidence even
        # when no single run observed both ids.
        document["canonical_variants"] = [
            {"canonical_id": r["canonical_id"],
             "observed_at": r["observed_at"], "run_id": r["run_id"],
             "raw_locator": r["raw_locator"]}
            for r in self._conn.execute(
                "SELECT canonical_id, observed_at, run_id, raw_locator"
                " FROM station_observations WHERE provider_id = ?"
                " AND station_id = ? AND canonical_id IS NOT NULL"
                " ORDER BY observed_at, run_id",
                (provider_id, station_id)).fetchall()]
        return document

    def _all_observations(self, provider_id: str,
                          station_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM station_observations WHERE provider_id = ?"
            " AND station_id = ? ORDER BY observed_at, run_id, obs_seq",
            (provider_id, station_id)).fetchall()
        return [self._observation_dict(row) for row in rows]

    def get_canonical_group(self, canonical_id: str) -> Optional[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM station_current WHERE canonical_id = ?"
            " ORDER BY provider_id, station_id", (canonical_id,)).fetchall()
        if not rows:
            return None
        return {
            "canonical_id": canonical_id,
            "member_count": len(rows),
            "members": [self.get_station(row["provider_id"],
                                         row["station_id"]) for row in rows],
        }

    def list_runs(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM station_runs ORDER BY run_id").fetchall()]

    def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self._conn.execute(
            "SELECT * FROM station_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        sources = [dict(row) for row in self._conn.execute(
            "SELECT * FROM station_run_sources WHERE run_id = ?"
            " ORDER BY provider_id", (run_id,)).fetchall()]
        pages = [dict(row) for row in self._conn.execute(
            "SELECT * FROM station_pages WHERE run_id = ?"
            " ORDER BY provider_id, page_seq", (run_id,)).fetchall()]
        return {**dict(run), "sources": sources, "pages": pages,
                "record_problems": self._problems(
                    "station_record_problems", run_id),
                "page_problems": self._problems(
                    "station_page_problems", run_id)}

    def list_problems(self, *, run_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "record_problems": self._problems(
                "station_record_problems", run_id),
            "page_problems": self._problems(
                "station_page_problems", run_id),
            "canonical_conflicts": [
                dict(row) for row in self._conn.execute(
                    "SELECT * FROM station_canonical_conflicts"
                    + (" WHERE run_id = ?" if run_id else "")
                    + " ORDER BY provider_id, station_id, canonical_id, run_id",
                    (run_id,) if run_id else ()).fetchall()
            ],
        }

    def list_sources(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM station_sources ORDER BY provider_id").fetchall()]

    # ------------------------------------------------------------- helpers
    def _evidence(self, evidence_ids_json: str) -> List[Dict[str, Any]]:
        evidence_ids = json.loads(evidence_ids_json)
        if not evidence_ids:
            return []
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = self._conn.execute(
            f"SELECT * FROM station_observations WHERE observation_id IN"
            f" ({placeholders}) ORDER BY observed_at, observation_id",
            evidence_ids).fetchall()
        return [self._observation_dict(row) for row in rows]

    def _canonical_conflicts(self, provider_id: str,
                             station_id: str) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(
            "SELECT run_id, provider_id, station_id, canonical_id,"
            " observed_at, raw_locator FROM station_canonical_conflicts"
            " WHERE provider_id = ? AND station_id = ?"
            " ORDER BY run_id, canonical_id",
            (provider_id, station_id)).fetchall()]

    def _problems(self, table: str,
                  run_id: Optional[str]) -> List[Dict[str, Any]]:
        where = " WHERE run_id = ?" if run_id else ""
        params = (run_id,) if run_id else ()
        rows = self._conn.execute(
            f"SELECT * FROM {table}{where}"
            " ORDER BY provider_id, page_seq, problem_id",
            params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    @staticmethod
    def _current_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "station_key": row["station_key"],
            "provider_id": row["provider_id"],
            "station_id": row["station_id"],
            "canonical_id": row["canonical_id"],
            "name": row["name"],
            "address": row["address"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "status": row["status"],
            "connectors": json.loads(row["connectors_json"]),
            "conflict": bool(row["conflict"]),
            "conflict_reasons": json.loads(row["conflict_reasons_json"]),
            "latest_observed_at": row["latest_observed_at"],
            "evidence_count": row["evidence_count"],
            "evidence_observation_ids": json.loads(row["evidence_ids_json"]),
        }

    @staticmethod
    def _observation_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "observation_id": row["observation_id"],
            "run_id": row["run_id"],
            "provider_id": row["provider_id"],
            "station_id": row["station_id"],
            "canonical_id": row["canonical_id"],
            "name": row["name"],
            "address": row["address"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "observed_at": row["observed_at"],
            "status": row["status"],
            "connectors": json.loads(row["connectors_json"]),
            "raw_locator": row["raw_locator"],
        }
