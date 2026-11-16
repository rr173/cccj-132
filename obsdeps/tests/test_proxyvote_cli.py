"""CLI, SQLite immutability and stored-result query tests for proxy votes.

The CLI runs as a subprocess in isolated mode (``-I``).  Expectations are the
paper numbers from ``test_proxyvote_handworked``; the stored-result queries are
checked against a *fresh process* that opens only the database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from support import SRC

CLI = os.path.join(os.path.dirname(SRC), "tools", "obsdeps_cli.py")

import proxyvote_fixtures as fx


def run_cli(*args: str, cwd: str = None):
    return subprocess.run(
        [sys.executable, "-I", CLI, *args],
        capture_output=True, text=True, cwd=cwd or tempfile.gettempdir())


def digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class ReconcileCli(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.documents = fx.worked_documents(self.directory)

    def _args(self):
        return [
            "--meeting-id", fx.MEETING, "--record-date", fx.RECORD_DATE,
            "--cutoff-at", fx.CUTOFF,
            "--roster", self.documents.paths["roster"],
            "--proposals", self.documents.paths["proposals"],
            "--grants", self.documents.paths["grants"],
            "--ballots", self.documents.paths["ballots"],
        ]

    def test_reconcile_is_deterministic_and_never_writes_inputs(self):
        before = {name: digest(path)
                  for name, path in self.documents.paths.items()}
        first = run_cli("proxyvote", "reconcile", *self._args())
        second = run_cli("proxyvote", "reconcile", *self._args())
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        result = json.loads(first.stdout)
        self.assertEqual(result["proposals"][0]["by_choice"]["X"], 100)
        self.assertTrue(result["complete"])
        self.assertEqual(result["meeting_id"], fx.MEETING)
        self.assertEqual(result["record_date"], fx.RECORD_DATE)
        self.assertEqual(result["cutoff_at"], fx.CUTOFF)
        for name, path in self.documents.paths.items():
            self.assertEqual(digest(path), before[name])

    def test_incomplete_run_exits_two_but_prints_results(self):
        bad = json.loads(json.dumps(self.documents.rows["grants"]))
        bad.append(fx.grant_row("GX", "P", "A", "B", 10, 200,
                                parent_grant_id="G1"))
        path = fx.write_jsonl(os.path.join(self.directory, "bad_grants.jsonl"),
                              bad)
        args = self._args()
        args[args.index("--grants") + 1] = path
        completed = run_cli("proxyvote", "reconcile", *args)
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertFalse(result["complete"])
        self.assertEqual(result["positions"][0]["status"], "incomplete")
        self.assertIn("grant-exceeds-parent",
                      [p["code"] for p in result["positions"][0]["problems"]])

    def test_missing_file_is_usage_error(self):
        args = self._args()
        args[args.index("--roster") + 1] = os.path.join(
            self.directory, "absent.jsonl")
        completed = run_cli("proxyvote", "reconcile", *args)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stdout)["error"],
                         "proxyvote-input")


class StoredQueries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.documents = fx.worked_documents(self.directory, with_c=True)
        self.db = os.path.join(self.directory, "proxyvote.sqlite3")

    def _store_args(self):
        return [
            "proxyvote", "store", "--db", self.db,
            "--created-at", "2026-03-06T00:00:00Z",
            "--meeting-id", fx.MEETING, "--record-date", fx.RECORD_DATE,
            "--cutoff-at", fx.CUTOFF,
            "--roster", self.documents.paths["roster"],
            "--proposals", self.documents.paths["proposals"],
            "--grants", self.documents.paths["grants"],
            "--ballots", self.documents.paths["ballots"],
        ]

    def test_store_then_query_in_a_fresh_process(self):
        stored = run_cli(*self._store_args())
        self.assertEqual(stored.returncode, 0, stored.stderr)
        outcome = json.loads(stored.stdout)
        self.assertEqual(outcome["status"], "registered")
        reconciliation_id = outcome["reconciliation_id"]

        summary = run_cli("proxyvote", "summary", "--db", self.db,
                          "--reconciliation-id", reconciliation_id)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        payload = json.loads(summary.stdout)
        self.assertEqual(payload["proposals"][0]["by_choice"]["X"], 85)
        self.assertEqual(payload["proposals"][0]["contested"], 15)
        self.assertTrue(payload["complete"])

        evidence = run_cli("proxyvote", "evidence", "--db", self.db,
                           "--reconciliation-id", reconciliation_id,
                           "--proposal-id", "Q")
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        segments = json.loads(evidence.stdout)["segments"]
        self.assertEqual(
            sum(s["unit_end"] - s["unit_start"] for s in segments), 100)
        self.assertEqual(
            [s["outcome"] for s in segments],
            ["X", "X", "contested", "contested", "X", "X", "X"])

        position = run_cli("proxyvote", "position", "--db", self.db,
                           "--reconciliation-id", reconciliation_id,
                           "--position-id", "P")
        self.assertEqual(position.returncode, 0, position.stderr)
        self.assertEqual(json.loads(position.stdout)["shares"], 100)

    def test_restoring_identical_inputs_is_a_noop(self):
        first = json.loads(run_cli(*self._store_args()).stdout)
        second = json.loads(run_cli(*self._store_args()).stdout)
        self.assertEqual(first["reconciliation_id"], second["reconciliation_id"])
        self.assertEqual(first["status"], "registered")
        self.assertEqual(second["status"], "already-registered")
        listing = json.loads(run_cli("proxyvote", "list", "--db", self.db,
                                     "--meeting-id", fx.MEETING).stdout)
        self.assertEqual(len(listing), 1)

    def test_sql_rows_match_the_cli_json(self):
        stored = json.loads(run_cli(*self._store_args()).stdout)
        summary = json.loads(run_cli(
            "proxyvote", "summary", "--db", self.db,
            "--reconciliation-id", stored["reconciliation_id"]).stdout)
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT proposal_id, by_choice_json, abstain, contested,"
            "       invalid_count FROM proxyvote_proposal_results"
            " WHERE reconciliation_id = ?", (stored["reconciliation_id"],)
        ).fetchall()
        positions = connection.execute(
            "SELECT position_id, holder_id, status FROM proxyvote_position_results"
            " WHERE reconciliation_id = ?", (stored["reconciliation_id"],)
        ).fetchall()
        connection.close()
        by_proposal = {row["proposal_id"]: row for row in rows}
        for proposal in summary["proposals"]:
            row = by_proposal[proposal["proposal_id"]]
            self.assertEqual(json.loads(row["by_choice_json"]),
                             proposal["by_choice"])
            self.assertEqual(row["abstain"], proposal["abstain"])
            self.assertEqual(row["contested"], proposal["contested"])
            self.assertEqual(row["invalid_count"], proposal["invalid"])
        self.assertEqual([(row["position_id"], row["holder_id"],
                           row["status"]) for row in positions],
                         [("P", "H", "reconciled")])

    def test_database_rows_are_immutable(self):
        run_cli(*self._store_args())
        connection = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE proxyvote_reconciliations SET complete = 0")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM proxyvote_position_results")
        connection.close()

    def test_stored_result_survives_deleting_the_source_files(self):
        stored = json.loads(run_cli(*self._store_args()).stdout)
        for path in self.documents.paths.values():
            os.unlink(path)
        summary = run_cli("proxyvote", "summary", "--db", self.db,
                          "--reconciliation-id",
                          stored["reconciliation_id"])
        self.assertEqual(summary.returncode, 0, summary.stderr)
        payload = json.loads(summary.stdout)
        self.assertEqual(payload["sources"]["roster"]["records"], 1)
        self.assertEqual(payload["proposals"][0]["contested"], 15)

    def test_unknown_reconciliation_query_is_a_clean_error(self):
        run_cli(*self._store_args())
        completed = run_cli("proxyvote", "summary", "--db", self.db,
                            "--reconciliation-id", "pv-does-not-exist")
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"], "proxyvote-request")
        self.assertEqual(payload["code"], "reconciliation-unknown")

    def test_a_second_version_of_the_meeting_does_not_replace_the_first(self):
        # Re-running the same version is a no-op; a *different* version of the
        # meeting is a separate reconciliation and both stay queryable, so a
        # later registration can never silently replace the requested one.
        first = json.loads(run_cli(*self._store_args()).stdout)
        second_args = self._store_args()
        second_args[second_args.index("--record-date") + 1] = "2026-03-02"
        second_args[second_args.index("--roster") + 1] = fx.write_jsonl(
            os.path.join(self.directory, "roster_v2.jsonl"),
            [fx.roster_row("P", "H", 90, record_date="2026-03-02")])
        second = run_cli(*second_args)
        self.assertEqual(second.returncode, 0, second.stderr)
        listing = json.loads(run_cli("proxyvote", "list", "--db", self.db,
                                     "--meeting-id", fx.MEETING).stdout)
        self.assertEqual(len(listing), 2)
        self.assertEqual({row["record_date"] for row in listing},
                         {"2026-03-01", "2026-03-02"})
        # The first request's stored answer still reports its own version.
        summary = json.loads(run_cli(
            "proxyvote", "summary", "--db", self.db,
            "--reconciliation-id",
            first["reconciliation_id"]).stdout)
        self.assertEqual(summary["record_date"], "2026-03-01")
        self.assertEqual(summary["proposals"][0]["by_choice"]["X"], 85)


if __name__ == "__main__":
    unittest.main()
