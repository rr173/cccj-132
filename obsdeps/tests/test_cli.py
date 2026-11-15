"""Command line behavior: JSON output, exit codes, readable arguments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from support import SRC, make_api
from fixtures import Bundle, dep, dep_path, pom


CLI = os.path.join(os.path.dirname(SRC), "tools", "obsdeps_cli.py")


def run_cli(*args: str):
    environment = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, "-I", CLI, *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tempfile.gettempdir(),
    )
    return completed


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        bundle = Bundle()
        bundle.observation("R1", "obs-child", "2024-01-01T00:00:00Z")
        bundle.observation("R1", "obs-parent", "2024-01-02T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs-child", "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0", parent=("com.acme", "parent", "1.0"),
                management=[("com.acme", "lib", "${release}")],
                dependencies=[dep("com.acme", "lib", None)]),
        )
        bundle.add_coordinate_pom(
            "R1", "obs-parent", "com.acme", "parent", "1.0",
            pom("com.acme", "parent", "1.0", properties={"release": "2.0"}),
        )
        bundle.add_coordinate_pom(
            "R1", "obs-parent", "com.acme", "lib", "2.0", pom("com.acme", "lib", "2.0"))
        bundle.add("R1", "obs-parent", dep_path("com.acme", "lib", "2.0", "jar"), "jar")
        cls.manifests = {}
        for key in bundle.observations:
            path = os.path.join(cls.directory, f"{key[0]}__{key[1]}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(bundle.manifest(*key), handle)
            cls.manifests[key] = path

    def setUp(self):
        # Each test starts from an empty database and registers the same two
        # observations, so test order cannot affect outcomes.
        self.db = os.path.join(tempfile.mkdtemp(), "obsdeps.sqlite3")
        for path in self.manifests.values():
            completed = run_cli("register", "--db", self.db, "--manifest", path)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_register_then_resolve(self):
        fresh = os.path.join(tempfile.mkdtemp(), "fresh.sqlite3")
        for path in self.manifests.values():
            completed = run_cli("register", "--db", fresh, "--manifest", path)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "registered")

        completed = run_cli(
            "resolve", "--db", fresh,
            "--chain", "R1=obs-parent,R1=obs-child",
            "--coordinate", "com.acme:app:1.0",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["dependencies"][0]["version"]["resolved"], "2.0")

    def test_blocked_exit_code_is_two(self):
        completed = run_cli(
            "resolve", "--db", self.db,
            "--chain", "R1=obs-child",
            "--coordinate", "com.acme:app:1.0",
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "blocked")

    def test_explain_filters_one_dependency(self):
        completed = run_cli(
            "explain", "--db", self.db,
            "--chain", "R1=obs-parent,R1=obs-child",
            "--coordinate", "com.acme:app:1.0",
            "--dependency", "com.acme:lib",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["explanation"]["dependency"], "com.acme:lib:jar:")
        self.assertEqual(payload["explanation"]["final_version"], "2.0")

    def test_compare_output(self):
        completed = run_cli(
            "compare", "--db", self.db,
            "--a-chain", "R1=obs-parent,R1=obs-child",
            "--b-chain", "R1=obs-child",
            "--a-coordinate", "com.acme:app:1.0",
            "--b-coordinate", "com.acme:app:1.0",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status_a"], "resolved")
        self.assertEqual(payload["status_b"], "blocked")

    def test_usage_error_is_json_with_exit_one(self):
        completed = run_cli("resolve", "--db", self.db, "--chain", "not-a-pair",
                            "--coordinate", "com.acme:app:1.0")
        self.assertEqual(completed.returncode, 2)  # argparse usage exit
        completed = run_cli("resolve", "--db", self.db, "--chain", "R1=obs-child",
                            "--pom", "a/b.pom")
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"], "usage")

    def test_observations_listing(self):
        completed = run_cli("observations", "--db", self.db)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        listing = json.loads(completed.stdout)
        self.assertEqual(len(listing), 2)


if __name__ == "__main__":
    unittest.main()
