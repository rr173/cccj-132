#!/usr/bin/env python3
"""Station directory worked example with paper-stated expectations.

Collects three local mock providers (two healthy, one cursor-cycling), stores
them in a temporary SQLite database, and prints/asserts the outcomes that can
be worked out by hand from ``build_stations_fixture.py``:

* identical cursor text in ALPHA and BETA still pages each provider on its own;
* ALPHA A1 keeps its newer 10:00 OPEN state after an older 06:00 row arrives;
* ALPHA A6 is CONFLICT because two 12:00 rows disagree;
* ALPHA A1 and BETA B1 merge under canonical id CAN-RIVERSIDE but keep separate
  provider-scoped evidence;
* ALPHA A2 and BETA B2 share a name and address but stay separate;
* invalid records (counts/coordinates) are rejected with row locators;
* GAMMA stops on a repeated cursor and is reported incomplete while ALPHA and
  BETA remain complete and queryable;
* re-running the same inputs yields the same run id and identical state.

Usage::

    python3 examples/run_stations_example.py [--keep-db PATH]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
for path in (SRC, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from obsdeps.stations import StationApi  # noqa: E402
from obsdeps.stations.transport import MockTransport  # noqa: E402

import build_stations_fixture as builder  # noqa: E402

# Paper expectations, written from the fixture rather than from engine output.
PAPER = {
    "page_cursors": {
        "ALPHA": [None, "cur-a", "cur-b"],
        "BETA": [None, "cur-a"],
        "GAMMA": [None, "loop"],
    },
    "run_status": {"ALPHA": "complete", "BETA": "complete",
                   "GAMMA": "incomplete"},
    "a1": {"status": "OPEN", "latest": "2024-05-01T10:00:00Z",
           "history": ["2024-05-01T06:00:00Z", "2024-05-01T10:00:00Z"]},
    "a6": {"status": "CONFLICT", "evidence": 2},
    "canonical_group": {"canonical_id": "CAN-RIVERSIDE",
                        "members": {("ALPHA", "A1"), ("BETA", "B1")}},
    "twins": {("ALPHA", "A2"), ("BETA", "B2")},
    "rejected_locators": {"ALPHA/page1/stations[2]",
                          "BETA/page1/data.sites[2]"},
    "gamma_problem": "page-cursor-cycle",
}


def check(label, actual, expected):
    status = "ok " if actual == expected else "FAIL"
    print(f"[{status}] {label}: {actual!r}")
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main(argv):
    keep_db = None
    if "--keep-db" in argv:
        keep_db = argv[argv.index("--keep-db") + 1]
    db_path = keep_db or os.path.join(tempfile.mkdtemp(), "stations.sqlite3")
    fixture = builder.build()
    with open(os.path.join(HERE, "stations_sources.json"),
              encoding="utf-8") as handle:
        sources = json.load(handle)["sources"]

    with StationApi(db_path) as api:
        summary = api.collect(
            sources, transport=MockTransport(fixture),
            started_at="2026-11-16T00:00:00Z")
        print("collection summary:", json.dumps(summary, sort_keys=True))
        check("two complete, one incomplete",
              (summary["complete"], summary["incomplete"]), (2, 1))
        check("two invalid records rejected",
              summary["records_rejected"], 2)

        run = api.list_runs()[0]
        detail = api.get_run_detail(run["run_id"])
        cursors = {
            s["provider_id"]: [
                p["request_cursor"] for p in detail["pages"]
                if p["provider_id"] == s["provider_id"]]
            for s in detail["sources"]}
        check("per-provider page cursors", cursors, PAPER["page_cursors"])
        check("run statuses",
              {s["provider_id"]: s["status"] for s in detail["sources"]},
              PAPER["run_status"])

        a1 = api.get_station("ALPHA", "A1")
        check("A1 current status (newer wins)", a1["status"],
              PAPER["a1"]["status"])
        check("A1 latest observation", a1["latest_observed_at"],
              PAPER["a1"]["latest"])
        check("A1 retains superseded older observation",
              [o["observed_at"] for o in a1["observation_history"]],
              PAPER["a1"]["history"])

        a6 = api.get_station("ALPHA", "A6")
        check("A6 conflict status", a6["status"], PAPER["a6"]["status"])
        check("A6 keeps both tied observations", len(a6["evidence"]),
              PAPER["a6"]["evidence"])

        group = api.get_canonical_group("CAN-RIVERSIDE")
        members = {(m["provider_id"], m["station_id"])
                   for m in group["members"]}
        check("canonical merge members", members,
              PAPER["canonical_group"]["members"])
        check("each member keeps provider-scoped evidence",
              [{e["provider_id"] for e in m["evidence"]}
               for m in group["members"]],
              [{"ALPHA"}, {"BETA"}])

        twins = {(s["provider_id"], s["station_id"])
                 for s in api.list_stations()
                 if s["name"] == "Market Square"}
        check("name/address twins without canonical stay separate",
              twins, PAPER["twins"])

        problems = api.list_problems()
        locators = {p["raw_locator"]
                    for p in problems["record_problems"]}
        check("rejected record locators", locators,
              PAPER["rejected_locators"])
        check("GAMMA cursor cycle reported",
              problems["page_problems"][0]["code"],
              PAPER["gamma_problem"])

        # Deterministic rerun: same fixture -> same run id, same rows.
        rerun = api.collect(sources, transport=MockTransport(fixture),
                            started_at="2026-11-16T00:00:00Z")
        check("rerun keeps the same run id", rerun["run_id"], run["run_id"])
        check("still exactly one run stored", len(api.list_runs()), 1)

    print(f"\ndatabase kept at: {db_path}")
    print("try:\n  python3 tools/obsdeps_cli.py stations list --db", db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
