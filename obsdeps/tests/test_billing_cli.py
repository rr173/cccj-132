"""End-to-end CLI and immutable SQLite store tests for billing.

The CLI is launched as a subprocess with ``-I`` (isolated mode) exactly like
the main package tests, and expectations are hand-written numbers checked
again inside these tests rather than snapshots emitted by the program.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from support import SRC

from obsdeps.billing import BillingApi

import billing_fixtures as fx

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


def digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def scenario_documents():
    readings = [
        fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
        fx.reading("M1", "2024-03-14T12:00:00-04:00", 6000),
        fx.reading("M1", "2024-03-15T00:00:00-04:00", 12000),
    ]
    periods = [fx.period("A", "M1", "TOU",
                         "2024-03-14T06:00:00-04:00",
                         "2024-03-15T00:00:00-04:00")]
    return fx.meters(), readings, [fx.TARIFF_TOU], periods


class BillingCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        meters, readings, tariffs, periods = scenario_documents()
        cls.paths = {
            "meters": write(cls.directory, "meters.json", meters),
            "readings": write(cls.directory, "readings.json", readings),
            "tariffs": write(cls.directory, "tariffs.json", tariffs),
            "periods": write(cls.directory, "periods.json", periods),
        }
        cls.before = {name: digest(path) for name, path in cls.paths.items()}

    def test_settle_from_files_is_offline_and_immutable(self):
        args = []
        for name, path in self.paths.items():
            args.extend([f"--{name}", path])
        completed = run_cli("billing", "settle", *args)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["summary"],
                         {"periods": 1, "settled": 1, "rejected": 0})
        bill = result["bills"][0]
        # Hand-written expectations from test_billing_handworked.Scenario A.
        self.assertEqual(bill["total_wh"], "9000.000000000")
        self.assertEqual(bill["total_amount"], "1.75")
        # Input files are byte-for-byte untouched.
        for name, path in self.paths.items():
            self.assertEqual(digest(path), self.before[name])

    def test_rejection_exit_code_is_two_but_keeps_good_bills(self):
        bad_periods = [
            fx.period("A", "M1", "TOU",
                      "2024-03-14T06:00:00-04:00",
                      "2024-03-15T00:00:00-04:00"),
            fx.period("BAD", "M1", "TOU",
                      "2030-01-01T00:00:00-05:00",
                      "2030-01-01T03:00:00-05:00"),
        ]
        period_path = write(self.directory, "periods2.json", bad_periods)
        completed = run_cli(
            "billing", "settle",
            "--meters", self.paths["meters"],
            "--readings", self.paths["readings"],
            "--tariffs", self.paths["tariffs"],
            "--periods", period_path)
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertEqual(result["summary"],
                         {"periods": 2, "settled": 1, "rejected": 1})
        self.assertEqual([r["period_id"] for r in result["rejected"]], ["BAD"])
        self.assertIn("missing-boundary-reading",
                      [p["code"] for p in result["rejected"][0]["problems"]])

    def test_missing_file_is_usage_error_not_a_bill(self):
        completed = run_cli(
            "billing", "settle",
            "--meters", os.path.join(self.directory, "nope.json"),
            "--readings", self.paths["readings"],
            "--tariffs", self.paths["tariffs"],
            "--periods", self.paths["periods"])
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"], "billing-input")


class RegisteredSettlement(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        meters, readings, tariffs, periods = scenario_documents()
        self.paths = {
            "meters": write(self.directory, "meters.json", meters),
            "readings": write(self.directory, "readings.json", readings),
            "tariffs": write(self.directory, "tariffs.json", tariffs),
            "periods": write(self.directory, "periods.json", periods),
        }
        self.db = os.path.join(self.directory, "billing.sqlite3")

    def _register(self, *extra):
        args = []
        for name, path in self.paths.items():
            args.extend([f"--{name}", path])
        return run_cli("billing", "register", "--db", self.db, *args, *extra)

    def test_register_then_settle_matches_direct_file_settlement(self):
        completed = self._register("--registered-at", "2024-03-16T00:00:00Z")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "registered")
        self.assertEqual(summary["meters"], 1)
        self.assertEqual(summary["readings"], 3)

        registered = run_cli("billing", "registered", "--db", self.db)
        self.assertEqual(registered.returncode, 0, registered.stderr)
        stored_result = json.loads(registered.stdout)
        with BillingApi() as api:
            direct = api.settle_files(**self.paths)
        stored_bill = stored_result["bills"][0]
        direct_bill = direct["bills"][0]
        self.assertEqual(stored_bill["details"], direct_bill["details"])
        self.assertEqual(stored_bill["total_amount"], direct_bill["total_amount"])

    def test_re_register_identical_content_is_noop(self):
        first = json.loads(self._register().stdout)
        second = json.loads(self._register().stdout)
        self.assertEqual(second["unchanged"]["meters"], 1)
        self.assertEqual(second["unchanged"]["readings"], 3)
        self.assertEqual(second["meters"], 0)
        self.assertNotEqual(first, None)

    def test_conflicting_meter_is_refused(self):
        self.assertEqual(self._register().returncode, 0)
        conflicting_meters = [{"meter_id": "M1",
                               "timezone": "Europe/Berlin"}]
        path = write(self.directory, "meters2.json", conflicting_meters)
        args = [
            "billing", "register", "--db", self.db,
            "--meters", path, "--readings", self.paths["readings"],
            "--tariffs", self.paths["tariffs"],
            "--periods", self.paths["periods"]]
        completed = run_cli(*args)
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["problem"]["code"], "meter-conflict")

    def test_api_registered_roundtrip_and_counts(self):
        with BillingApi(self.db) as api:
            api.register_inputs(**self.paths,
                                registered_at="2024-03-16T00:00:00Z")
            counts = api.store_counts()
            result = api.settle_registered()
        self.assertEqual(counts["billing_meters"], 1)
        self.assertEqual(counts["billing_readings"], 3)
        self.assertEqual(result["bills"][0]["total_wh"], "9000.000000000")


if __name__ == "__main__":
    unittest.main()
