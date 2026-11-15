"""The worked example must match its hand-written expectations."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import SRC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "examples", "run_worked_example.py")


class WorkedExample(unittest.TestCase):
    def test_matches_hand_written_expectations(self):
        completed = subprocess.run(
            [sys.executable, "-I", RUNNER],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("matches hand-written expectations", completed.stdout)

    def test_expected_values_are_not_generated(self):
        """Guard against someone regenerating expected.json from the engine."""
        with open(os.path.join(ROOT, "examples", "expected.json"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Hand-written expectations", text)
        self.assertIn("do not confuse with observed_at timestamps", text)
        self.assertNotIn("context_sha256", text)
        self.assertNotIn("sha256:", text)


if __name__ == "__main__":
    unittest.main()
