"""SQLite storage for the offline settlement inputs.

Mirrors the main package's immutability discipline: once a meter, tariff,
reading or period is registered it cannot be updated or deleted (SQLite
triggers enforce it).  Registering the *same* entity again is a no-op;
registering different content under the same identity is refused rather than
silently overwritten.

The tables store the canonical JSON of each input row, so a stored settlement
can always be re-run from exactly the bytes it was first given.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from ..errors import Problem, Problematic

STORE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS billing_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_batches (
    batch_id       TEXT PRIMARY KEY,
    registered_at  TEXT NOT NULL,
    note           TEXT
);
CREATE TABLE IF NOT EXISTS billing_meters (
    meter_id      TEXT PRIMARY KEY,
    timezone      TEXT NOT NULL,
    modulus       TEXT,
    payload_json  TEXT NOT NULL,
    batch_id      TEXT,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_tariffs (
    tariff_id     TEXT PRIMARY KEY,
    payload_json  TEXT NOT NULL,
    batch_id      TEXT,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_readings (
    reading_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id      TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    watt_hours    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    batch_id      TEXT,
    registered_at TEXT NOT NULL,
    UNIQUE (meter_id, timestamp, watt_hours)
);
CREATE TABLE IF NOT EXISTS billing_periods (
    period_id     TEXT PRIMARY KEY,
    meter_id      TEXT NOT NULL,
    tariff_id     TEXT NOT NULL,
    start_text    TEXT NOT NULL,
    end_text      TEXT NOT NULL,
    currency      TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    batch_id      TEXT,
    registered_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS billing_meters_immutable_update
BEFORE UPDATE ON billing_meters BEGIN
    SELECT RAISE(ABORT, 'billing meters are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_meters_immutable_delete
BEFORE DELETE ON billing_meters BEGIN
    SELECT RAISE(ABORT, 'billing meters are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_tariffs_immutable_update
BEFORE UPDATE ON billing_tariffs BEGIN
    SELECT RAISE(ABORT, 'billing tariffs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_tariffs_immutable_delete
BEFORE DELETE ON billing_tariffs BEGIN
    SELECT RAISE(ABORT, 'billing tariffs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_readings_immutable_update
BEFORE UPDATE ON billing_readings BEGIN
    SELECT RAISE(ABORT, 'billing readings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_readings_immutable_delete
BEFORE DELETE ON billing_readings BEGIN
    SELECT RAISE(ABORT, 'billing readings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_periods_immutable_update
BEFORE UPDATE ON billing_periods BEGIN
    SELECT RAISE(ABORT, 'billing periods are immutable');
END;
CREATE TRIGGER IF NOT EXISTS billing_periods_immutable_delete
BEFORE DELETE ON billing_periods BEGIN
    SELECT RAISE(ABORT, 'billing periods are immutable');
END;
"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BillingStore:
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
            "INSERT OR IGNORE INTO billing_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(STORE_SCHEMA_VERSION)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BillingStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ registering
    def register_batch(
        self,
        *,
        meters: List[Dict[str, Any]],
        readings: List[Dict[str, Any]],
        tariffs: List[Dict[str, Any]],
        periods: List[Dict[str, Any]],
        batch_id: Optional[str] = None,
        note: Optional[str] = None,
        registered_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        stamp = registered_at or _stamp()
        bid = batch_id or f"batch-{stamp.replace(':', '').replace('-', '')}"
        summary = {"batch_id": bid, "meters": 0, "readings": 0,
                   "tariffs": 0, "periods": 0,
                   "unchanged": {"meters": 0, "readings": 0, "tariffs": 0,
                                 "periods": 0}}
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO billing_batches (batch_id, registered_at, note)"
                " VALUES (?, ?, ?)", (bid, stamp, note),
            )
            summary["meters"] = self._insert_meters(meters, bid, stamp, summary)
            summary["tariffs"] = self._insert_tariffs(tariffs, bid, stamp, summary)
            summary["readings"] = self._insert_readings(readings, bid, stamp, summary)
            summary["periods"] = self._insert_periods(periods, bid, stamp, summary)
        return summary

    def _insert_meters(self, rows: List[Dict[str, Any]], bid: str,
                       stamp: str, summary: Dict[str, Any]) -> int:
        inserted = 0
        for row in rows:
            canonical = _canonical(row)
            existing = self._conn.execute(
                "SELECT payload_json FROM billing_meters WHERE meter_id = ?",
                (row["meter_id"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == canonical:
                    summary["unchanged"]["meters"] += 1
                    continue
                raise Problematic(Problem(
                    "meter-conflict",
                    "meter already registered with different content; inputs are immutable",
                    f"meter:{row['meter_id']}",
                ))
            modulus = row.get("modulus")
            self._conn.execute(
                "INSERT INTO billing_meters (meter_id, timezone, modulus, payload_json,"
                " batch_id, registered_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["meter_id"], row["timezone"],
                 None if modulus is None else str(modulus),
                 canonical, bid, stamp),
            )
            inserted += 1
        return inserted

    def _insert_tariffs(self, rows: List[Dict[str, Any]], bid: str,
                        stamp: str, summary: Dict[str, Any]) -> int:
        inserted = 0
        for row in rows:
            canonical = _canonical(row)
            existing = self._conn.execute(
                "SELECT payload_json FROM billing_tariffs WHERE tariff_id = ?",
                (row["tariff_id"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == canonical:
                    summary["unchanged"]["tariffs"] += 1
                    continue
                raise Problematic(Problem(
                    "tariff-conflict",
                    "tariff already registered with different content; inputs are immutable",
                    f"tariff:{row['tariff_id']}",
                ))
            self._conn.execute(
                "INSERT INTO billing_tariffs (tariff_id, payload_json, batch_id,"
                " registered_at) VALUES (?, ?, ?, ?)",
                (row["tariff_id"], canonical, bid, stamp),
            )
            inserted += 1
        return inserted

    def _insert_readings(self, rows: List[Dict[str, Any]], bid: str,
                         stamp: str, summary: Dict[str, Any]) -> int:
        inserted = 0
        for row in rows:
            canonical = _canonical(row)
            existing = self._conn.execute(
                "SELECT payload_json FROM billing_readings"
                " WHERE meter_id = ? AND timestamp = ? AND watt_hours = ?",
                (row["meter_id"], row["timestamp"], str(row["watt_hours"])),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == canonical:
                    summary["unchanged"]["readings"] += 1
                    continue
                raise Problematic(Problem(
                    "reading-conflict",
                    "identical meter/timestamp/value reading stored with different payload",
                    f"meter:{row['meter_id']}",
                    {"timestamp": row["timestamp"]},
                ))
            try:
                self._conn.execute(
                    "INSERT INTO billing_readings (meter_id, timestamp, watt_hours,"
                    " payload_json, batch_id, registered_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (row["meter_id"], row["timestamp"], str(row["watt_hours"]),
                     canonical, bid, stamp),
                )
            except sqlite3.IntegrityError as exc:
                # Same meter/timestamp but different value -> conflict.
                raise Problematic(Problem(
                    "duplicate-reading-conflict",
                    f"a different reading is already stored for this meter and "
                    f"timestamp: {exc}",
                    f"meter:{row['meter_id']}",
                    {"timestamp": row["timestamp"]},
                )) from exc
            inserted += 1
        return inserted

    def _insert_periods(self, rows: List[Dict[str, Any]], bid: str,
                        stamp: str, summary: Dict[str, Any]) -> int:
        inserted = 0
        for row in rows:
            canonical = _canonical(row)
            existing = self._conn.execute(
                "SELECT payload_json FROM billing_periods WHERE period_id = ?",
                (row["period_id"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == canonical:
                    summary["unchanged"]["periods"] += 1
                    continue
                raise Problematic(Problem(
                    "period-conflict",
                    "period already registered with different content; inputs are immutable",
                    f"period:{row['period_id']}",
                ))
            self._conn.execute(
                "INSERT INTO billing_periods (period_id, meter_id, tariff_id, start_text,"
                " end_text, currency, payload_json, batch_id, registered_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["period_id"], row["meter_id"], row["tariff_id"],
                 row["start"], row["end"], row.get("currency", "USD"),
                 canonical, bid, stamp),
            )
            inserted += 1
        return inserted

    # ------------------------------------------------------------- extraction
    def load_documents(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return every stored row as the JSON structure it was registered with."""
        def payloads(table: str) -> List[Dict[str, Any]]:
            rows = self._conn.execute(
                f"SELECT payload_json FROM {table} ORDER BY reading_id"
                if table == "billing_readings"
                else f"SELECT payload_json FROM {table}"
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]

        return {
            "meters": payloads("billing_meters"),
            "readings": payloads("billing_readings"),
            "tariffs": payloads("billing_tariffs"),
            "periods": payloads("billing_periods"),
        }

    def counts(self) -> Dict[str, int]:
        return {
            table: self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("billing_meters", "billing_readings",
                          "billing_tariffs", "billing_periods")
        }
