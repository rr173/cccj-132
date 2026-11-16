"""End-to-end CLI tests for the charging-station collector.

The CLI is launched as a subprocess with ``-I`` exactly like the billing CLI
tests. Two independent views are cross-checked in every scenario:

1. the JSON printed by the command, and
2. direct ``sqlite3`` queries against the database file (the way an operator
   would verify the claim without trusting the program).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from support import SRC

import stations_fixtures as fx

CLI = os.path.join(os.path.dirname(SRC), "tools", "obsdeps_cli.py")


def run_cli(*args: str):
    environment = dict(os.environ)
    return subprocess.run(
        [sys.executable, "-I", CLI, *args],
        capture_output=True, text=True, env=environment,
        cwd=tempfile.gettempdir(),
    )


def write(directory: str, name: str, document) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


class StationCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obsdeps-stations-")
        self.db = os.path.join(self.tmp, "stations.sqlite3")
        self.sources_path = write(self.tmp, "sources.json", fx.main_sources())
        self.fixture_path = write(self.tmp, "fixture.json", fx.main_fixture())

    def collect(self):
        return run_cli(
            "stations", "collect", "--db", self.db,
            "--sources", self.sources_path,
            "--mock-fixture", self.fixture_path,
            "--started-at", "2026-11-16T00:00:00Z")

    def test_collect_list_show_and_sqlite_cross_check(self):
        proc = self.collect()
        # All sources completed, but five invalid records were rejected, so
        # the command flags the run with exit code 2 while keeping every
        # valid station queryable.
        self.assertEqual(proc.returncode, 2, proc.stderr)
        summary = json.loads(proc.stdout)
        # Paper totals from stations_fixtures.EXPECTED.
        self.assertEqual(summary["sources"], 2)
        self.assertEqual(summary["complete"], 2)
        self.assertEqual(summary["incomplete"], 0)
        self.assertEqual(summary["records_rejected"], 5)
        self.assertEqual(summary["stations_accepted"], 12)

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row

        # Independent SQLite check of the run bookkeeping.
        run_row = conn.execute(
            "SELECT * FROM station_runs").fetchone()
        self.assertEqual(run_row["status"], "complete")
        self.assertEqual(run_row["run_id"], summary["run_id"])

        # Cursor provenance per provider, including identical cursor text.
        page_rows = conn.execute(
            "SELECT provider_id, page_seq, request_cursor, next_cursor"
            " FROM station_pages ORDER BY provider_id, page_seq").fetchall()
        self.assertEqual(
            [tuple(r) for r in page_rows],
            [("ALPHA", 1, None, "cur-a"),
             ("ALPHA", 2, "cur-a", "cur-b"),
             ("ALPHA", 3, "cur-b", None),
             ("BETA", 1, None, "cur-a"),
             ("BETA", 2, "cur-a", None)])

        # CLI list vs SELECT station_current.
        listed = json.loads(run_cli(
            "stations", "list", "--db", self.db).stdout)
        sql_rows = conn.execute(
            "SELECT provider_id, station_id, status FROM station_current"
            " ORDER BY provider_id, station_id").fetchall()
        self.assertEqual([(s["provider_id"], s["station_id"], s["status"])
                          for s in listed],
                         [tuple(r) for r in sql_rows])

        # Newer-observation rule: A1 stays OPEN even though the older CLOSED
        # record arrived on a later page. Verified both ways.
        shown = json.loads(run_cli(
            "stations", "show", "--db", self.db,
            "--provider", "ALPHA", "--station", "A1").stdout)
        self.assertEqual(shown["status"], "OPEN")
        self.assertEqual(shown["latest_observed_at"],
                         "2024-05-01T10:00:00Z")
        current_row = conn.execute(
            "SELECT status, latest_observed_at FROM station_current"
            " WHERE provider_id='ALPHA' AND station_id='A1'").fetchone()
        self.assertEqual(tuple(current_row),
                         ("OPEN", "2024-05-01T10:00:00Z"))
        # Evidence locator preserved down to the original row position.
        self.assertEqual(shown["evidence"][0]["raw_locator"],
                         "ALPHA/page1/stations[0]")
        evidence_loc = conn.execute(
            "SELECT raw_locator FROM station_observations"
            " WHERE run_id=? AND provider_id='ALPHA' AND station_id='A1'"
            " AND observed_at='2024-05-01T10:00:00Z'",
            (run_row["run_id"],)).fetchone()[0]
        self.assertEqual(evidence_loc, "ALPHA/page1/stations[0]")

        # Same-timestamp contradiction: CONFLICT, both records retained.
        conflict = json.loads(run_cli(
            "stations", "show", "--db", self.db,
            "--provider", "ALPHA", "--station", "A6").stdout)
        self.assertEqual(conflict["status"], "CONFLICT")
        self.assertEqual(len(conflict["evidence"]), 2)
        n_evidence = conn.execute(
            "SELECT COUNT(*) AS n FROM station_observations"
            " WHERE provider_id='ALPHA' AND station_id='A6'").fetchone()["n"]
        self.assertEqual(n_evidence, 2)

        # Canonical merge groups the two providers but keeps separate evidence.
        group = json.loads(run_cli(
            "stations", "canonical", "--db", self.db,
            "--canonical-id", "CAN-RIVERSIDE").stdout)
        self.assertEqual(group["member_count"], 2)
        self.assertEqual({m["provider_id"] for m in group["members"]},
                         {"ALPHA", "BETA"})
        group_sql = conn.execute(
            "SELECT COUNT(*) AS n FROM station_current"
            " WHERE canonical_id='CAN-RIVERSIDE'").fetchone()["n"]
        self.assertEqual(group_sql, 2)

        # Name+address twins without canonical ids stay separate rows.
        twins = conn.execute(
            "SELECT COUNT(*) AS n FROM station_current"
            " WHERE name='Market Square' AND address=?",
            (fx.MARKET_ADDRESS,)).fetchone()["n"]
        self.assertEqual(twins, 2)
        twins_canonical = conn.execute(
            "SELECT COUNT(*) AS n FROM station_current"
            " WHERE name='Market Square' AND canonical_id IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(twins_canonical, 0)

        # Rejected records are locatable rows and the valid siblings survived.
        problems = json.loads(run_cli(
            "stations", "problems", "--db", self.db).stdout)
        locators = sorted(p["raw_locator"]
                          for p in problems["record_problems"])
        self.assertEqual(locators,
                         sorted(fx.EXPECTED["alpha_rejected_locators"]
                                + fx.EXPECTED["beta_rejected_locators"]))
        rejected_sql = conn.execute(
            "SELECT COUNT(*) AS n FROM station_record_problems"
        ).fetchone()["n"]
        self.assertEqual(rejected_sql, 5)

        # Contradictory canonical ids leave evidence and no chosen canonical.
        flip = json.loads(run_cli(
            "stations", "show", "--db", self.db,
            "--provider", "ALPHA", "--station", "A8").stdout)
        self.assertIsNone(flip["canonical_id"])
        self.assertIn("canonical-id-conflict", flip["conflict_reasons"])
        conflict_sql = conn.execute(
            "SELECT DISTINCT canonical_id FROM station_canonical_conflicts"
            " WHERE provider_id='ALPHA' AND station_id='A8'"
        ).fetchall()
        self.assertEqual({r[0] for r in conflict_sql},
                         {"CAN-FLIP-1", "CAN-FLIP-2"})
        conn.close()

    def test_rerun_is_deterministic_and_idempotent(self):
        first = self.collect()
        first_summary = json.loads(first.stdout)
        second = self.collect()
        second_summary = json.loads(second.stdout)
        self.assertEqual(first_summary["run_id"], second_summary["run_id"])
        conn = sqlite3.connect(self.db)
        # One run row and one observation set, not duplicated rows.
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM station_runs").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM station_observations").fetchone()[0], 12)
        conn.close()

    def test_cycle_and_bad_feed_sources_are_incomplete_but_batch_survives(self):
        # GAMMA cycles; BETA is healthy. The run exits 2 but BETA is queryable.
        cycle_fixture = write(
            self.tmp, "cycle.json",
            {**fx.gamma_cycle_fixture(), "BETA": fx.beta_pages()})
        sources = write(
            self.tmp, "mixed-sources.json",
            fx.cycle_sources() + [
                s for s in fx.main_sources() if s["provider_id"] == "BETA"])
        proc = run_cli(
            "stations", "collect", "--db", self.db,
            "--sources", sources, "--mock-fixture", cycle_fixture,
            "--started-at", "2026-11-16T00:00:00Z")
        self.assertEqual(proc.returncode, 2)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["complete"], 1)
        self.assertEqual(summary["incomplete"], 1)

        runs = json.loads(run_cli(
            "stations", "runs", "--db", self.db).stdout)
        detail = json.loads(run_cli(
            "stations", "run", "--db", self.db,
            "--run-id", runs[0]["run_id"]).stdout)
        by_provider = {s["provider_id"]: s for s in detail["sources"]}
        self.assertEqual(by_provider["GAMMA"]["status"], "incomplete")
        self.assertEqual(by_provider["BETA"]["status"], "complete")
        self.assertEqual(detail["page_problems"][0]["code"],
                         "page-cursor-cycle")

        # The healthy source is still fully queryable through the CLI.
        listed = json.loads(run_cli(
            "stations", "list", "--db", self.db,
            "--provider", "BETA").stdout)
        self.assertEqual({s["station_id"] for s in listed},
                         {"B1", "B2", "B3", "B6"})

        conn = sqlite3.connect(self.db)
        # GAMMA's cycling page content was still stored before stopping.
        gamma_stations = conn.execute(
            "SELECT DISTINCT station_id FROM station_observations"
            " WHERE provider_id='GAMMA' ORDER BY station_id").fetchall()
        self.assertEqual([r[0] for r in gamma_stations], ["G1", "G2"])
        conn.close()

    def test_bad_configuration_exits_one_without_touching_database(self):
        bad_sources = write(self.tmp, "bad.json", [
            {"provider_id": "X", "endpoint": "u",
             "adapter_type": "no-such-adapter"}])
        proc = run_cli(
            "stations", "collect", "--db", self.db,
            "--sources", bad_sources, "--mock-fixture", self.fixture_path)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "station-config")
        self.assertIn("adapter_type", payload["source"])
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
