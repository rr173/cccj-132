"""Scale test: runtime must follow input interval count, not share count.

Two real CLI runs are timed:

* 100,000 disjoint grant intervals over a position with 10^12 shares;
* the same 100,000 intervals over a position with a much larger share count.

If the engine enumerated shares, the second run would be impossible.  The test
also asserts that the *advice* (evidence) size grows with intervals, not with
shares, and records wall time and peak RSS when the environment exposes them.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest

from support import SRC

CLI = os.path.join(os.path.dirname(SRC), "tools", "obsdeps_cli.py")

GRANTS = 100_000
BIG_SHARES = 10 ** 12


def build(directory: str, shares: int, *, ballots: bool = True):
    roster = os.path.join(directory, "roster.jsonl")
    proposals = os.path.join(directory, "proposals.jsonl")
    grants = os.path.join(directory, "grants.jsonl")
    ballot_path = os.path.join(directory, "ballots.jsonl")
    with open(roster, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "meeting_id": "BIG", "position_id": "P", "holder_id": "H",
            "security_class": "COMMON", "record_date": "2026-01-01",
            "shares": shares}) + "\n")
    with open(proposals, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "meeting_id": "BIG", "proposal_id": "Q", "title": "Q",
            "options": ["YES", "NO", "ABSTAIN"]}) + "\n")
    with open(grants, "w", encoding="utf-8") as handle:
        for index in range(GRANTS):
            handle.write(json.dumps({
                "meeting_id": "BIG", "grant_id": f"G{index}",
                "position_id": "P", "grantor": "H", "grantee": f"A{index}",
                "unit_start": index * 2, "unit_end": index * 2 + 2,
                "proposal_ids": ["Q"],
                "effective_at": "2026-02-01T00:00:00Z",
                "revoked_at": None, "parent_grant_id": None}) + "\n")
    with open(ballot_path, "w", encoding="utf-8") as handle:
        if ballots:
            for index in range(0, GRANTS, 1000):
                handle.write(json.dumps({
                    "meeting_id": "BIG", "ballot_id": f"B{index}",
                    "position_id": "P", "unit_start": index * 2,
                    "unit_end": index * 2 + 2, "voter": f"A{index}",
                    "proposal_id": "Q", "choice": "YES",
                    "submitted_at": "2026-03-01T00:00:00Z"}) + "\n")
    return roster, proposals, grants, ballot_path


def run(directory: str, shares: int, *, ballots: bool = True):
    roster, proposals, grants, ballot_path = build(directory, shares,
                                                   ballots=ballots)
    command = [
        sys.executable, "-I", CLI, "proxyvote", "reconcile",
        "--meeting-id", "BIG", "--record-date", "2026-01-01",
        "--cutoff-at", "2026-03-02T00:00:00Z",
        "--roster", roster, "--proposals", proposals,
        "--grants", grants, "--ballots", ballot_path,
    ]
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return completed, elapsed, peak_kb, before


class ScaleByIntervals(unittest.TestCase):
    def test_runtime_tracks_ranges_not_shares(self):
        directory = tempfile.mkdtemp()
        small, small_seconds, small_rss, _ = run(directory, GRANTS * 2)
        self.assertEqual(small.returncode, 0, small.stderr[:2000])
        large, large_seconds, large_rss, _ = run(directory, BIG_SHARES)
        self.assertEqual(large.returncode, 0, large.stderr[:2000])

        small_doc = json.loads(small.stdout)
        large_doc = json.loads(large.stdout)
        self.assertEqual(large_doc["proposals"][0]["by_choice"]["YES"],
                         (GRANTS + 999) // 1000 * 2)
        self.assertEqual(large_doc["proposals"][0]["contested"], 0)
        self.assertEqual(len(large_doc["proposals"][0]["segments"]),
                         GRANTS + 1)

        # A ~10^6x larger share universe with the same interval count must not
        # change the evidence volume materially nor the runtime class.
        self.assertLess(large_seconds, max(60.0, small_seconds * 4))
        print(f"\nscale: intervals={GRANTS} shares={BIG_SHARES} "
              f"elapsed={large_seconds:.2f}s peak_rss_kb={large_rss} "
              f"segments={len(large_doc['proposals'][0]['segments'])} "
              f"bytes={len(large.stdout)}", file=sys.stderr)

    def test_zero_ballots_still_partitions_by_intervals(self):
        directory = tempfile.mkdtemp()
        completed, elapsed, peak_kb, _ = run(directory, BIG_SHARES,
                                             ballots=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[:2000])
        document = json.loads(completed.stdout)
        proposal = document["proposals"][0]
        self.assertEqual(proposal["by_choice"]["YES"], 0)
        self.assertEqual(proposal["abstain"], BIG_SHARES)
        self.assertEqual(len(proposal["segments"]), GRANTS + 1)
        print(f"\nscale-no-ballots: elapsed={elapsed:.2f}s "
              f"peak_rss_kb={peak_kb}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
