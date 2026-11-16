"""Input validation, isolation and rejection-code tests for proxy votes.

Each invalid input is injected into an otherwise-valid meeting.  The rule under
test: the offending *position* (or row) is reported with a stable code and the
run is marked incomplete, while other positions still reconcile and remain
locatable.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from support import SRC  # noqa: F401

from obsdeps.proxyvote import ProxyVoteApi, ProxyVoteInputError

import proxyvote_fixtures as fx


class Validation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def reconcile_rows(self, roster, proposals, grants, ballots,
                       *, cutoff_at=fx.CUTOFF):
        with ProxyVoteApi() as api:
            return api.reconcile_documents(
                meeting_id=fx.MEETING, record_date=fx.RECORD_DATE,
                cutoff_at=cutoff_at, roster=roster, proposals=proposals,
                grants=grants, ballots=ballots)

    def test_hostile_grantor_and_missing_parent_are_locatable(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 100), fx.roster_row("R", "K", 10)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 100),
             # R's root grant comes from the wrong person.
             fx.grant_row("G2", "R", "H", "A", 0, 10),
             # P's child grant names a parent that does not exist.
             fx.grant_row("G3", "P", "A", "B", 10, 20, parent_grant_id="NOPE")],
            [fx.ballot_row("B1", "P", 0, 100, "A")])
        positions = {p["position_id"]: p for p in result["positions"]}
        self.assertEqual(positions["P"]["status"], "incomplete")
        self.assertEqual(positions["R"]["status"], "incomplete")
        self.assertFalse(result["complete"])
        codes = {p["code"] for position in result["positions"]
                 for p in position["problems"]}
        self.assertIn("grant-parent-unknown", codes)
        self.assertIn("grant-grantor-mismatch", codes)
        # Problem locators remain present, so a bad position is findable.
        for position in result["positions"]:
            for problem in position["problems"]:
                self.assertTrue(problem["locator"])
                self.assertEqual(problem["details"]["position_id"],
                                 position["position_id"])

    def test_one_bad_position_does_not_hide_the_good_one(self):
        result = self.reconcile_rows(
            [fx.roster_row("GOOD", "H", 10), fx.roster_row("BAD", "K", 10)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "GOOD", "H", "A", 0, 10),
             fx.grant_row("G2", "BAD", "K", "Z", 0, 40)],   # exceeds shares
            [fx.ballot_row("B1", "GOOD", 0, 10, "A")])
        positions = {p["position_id"]: p for p in result["positions"]}
        self.assertEqual(positions["GOOD"]["status"], "reconciled")
        self.assertEqual(
            positions["GOOD"]["proposals"][0]["by_choice"]["X"], 10)
        self.assertEqual(positions["BAD"]["status"], "incomplete")
        self.assertIn("grant-exceeds-position",
                      [p["code"] for p in positions["BAD"]["problems"]])
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["incomplete_positions"], 1)

    def test_empty_proposal_set_is_an_input_error(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10)], [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 10, proposal_ids=())], [])
        self.assertFalse(result["complete"])
        self.assertIn("grant-empty-proposals",
                      [p["code"] for p in result["problems"]])

    def test_duplicate_grant_id_quarantines_only_its_position(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 100), fx.roster_row("R", "K", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G", "P", "H", "A", 0, 50),
             fx.grant_row("G", "P", "H", "B", 50, 100),
             fx.grant_row("GR", "R", "K", "Z", 0, 100)],
            [fx.ballot_row("B1", "R", 0, 100, "Z")])
        positions = {p["position_id"]: p for p in result["positions"]}
        self.assertIn("grant-duplicate-id",
                      [p["code"] for p in positions["P"]["problems"]])
        self.assertEqual(positions["R"]["status"], "reconciled")
        self.assertEqual(positions["R"]["proposals"][0]["by_choice"]["X"], 100)

    def test_grant_revoked_before_effective_is_an_input_error(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10)], [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 10,
                          effective_at="2026-03-04T00:00:00Z",
                          revoked_at="2026-03-03T00:00:00Z")], [])
        self.assertFalse(result["complete"])
        self.assertIn("grant-time-invalid",
                      [p["code"] for p in result["positions"][0]["problems"]])

    def test_malformed_ballot_row_is_scoped_to_its_position(self):
        ballots = [fx.ballot_row("B1", "P", 0, 5, "A")]
        ballots.append({"meeting_id": fx.MEETING, "ballot_id": "B2",
                        "position_id": "P", "unit_start": 1, "unit_end": 2,
                        "voter": "A", "proposal_id": "Q",
                        "choice": "NOT-AN-OPTION",
                        "submitted_at": "2026-03-04T00:00:00Z"})
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10), fx.roster_row("R", "K", 10)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 5)], ballots)
        positions = {p["position_id"]: p for p in result["positions"]}
        self.assertIn("ballot-choice-invalid",
                      [p["code"] for p in positions["P"]["problems"]])
        self.assertEqual(positions["R"]["status"], "reconciled")
        self.assertFalse(result["complete"])

    def test_duplicate_position_id_is_an_input_error(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10), fx.roster_row("P", "H", 20)],
            [fx.proposal_row("Q")], [], [])
        self.assertFalse(result["complete"])
        self.assertIn("row-duplicate-position",
                      [p["code"] for p in result["problems"]])

    def test_roster_record_date_must_match_the_request(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10, record_date="2026-02-01")],
            [fx.proposal_row("Q")], [], [])
        self.assertFalse(result["complete"])
        self.assertIn("row-wrong-type",
                      [p["code"] for p in result["problems"]])

    def test_ballot_referencing_unknown_position_is_reported(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10)], [fx.proposal_row("Q")], [],
            [fx.ballot_row("B1", "GHOST", 0, 5, "H")])
        self.assertFalse(result["complete"])
        self.assertIn("ballot-unknown-position",
                      [p["code"] for p in result["problems"]])

    def test_file_and_document_modes_agree_on_the_same_input(self):
        directory = tempfile.mkdtemp()
        documents = fx.worked_documents(directory)
        with ProxyVoteApi() as api:
            from_files = api.reconcile_files(**documents.arguments())
            from_rows = api.reconcile_documents(**documents.documents())
        self.assertEqual(from_files["positions"], from_rows["positions"])
        self.assertEqual(from_files["proposals"], from_rows["proposals"])
        self.assertEqual(from_files["sources"]["roster"]["sha256"],
                         from_rows["sources"]["roster"]["sha256"])

    def test_unreadable_document_raises(self):
        with ProxyVoteApi() as api:
            with self.assertRaises(ProxyVoteInputError):
                api.reconcile_files(
                    meeting_id=fx.MEETING, record_date=fx.RECORD_DATE,
                    cutoff_at=fx.CUTOFF,
                    roster=os.path.join(self.directory, "missing.jsonl"),
                    proposals=os.path.join(self.directory, "missing2.jsonl"),
                    grants=os.path.join(self.directory, "missing3.jsonl"),
                    ballots=os.path.join(self.directory, "missing4.jsonl"))

    def test_malformed_json_line_raises_for_the_whole_file(self):
        roster = fx.write_jsonl(os.path.join(self.directory, "roster.jsonl"),
                                [fx.roster_row("P", "H", 10)])
        proposals = fx.write_jsonl(
            os.path.join(self.directory, "proposals.jsonl"), [fx.proposal_row("Q")])
        grants = fx.write_jsonl(os.path.join(self.directory, "grants.jsonl"), [])
        ballots = os.path.join(self.directory, "ballots.jsonl")
        with open(ballots, "w", encoding="utf-8") as handle:
            handle.write('{"meeting_id": "M-2026", "ballot_id": "B1"\n')
        with ProxyVoteApi() as api:
            with self.assertRaises(ProxyVoteInputError) as raised:
                api.reconcile_files(
                    meeting_id=fx.MEETING, record_date=fx.RECORD_DATE,
                    cutoff_at=fx.CUTOFF, roster=roster, proposals=proposals,
                    grants=grants, ballots=ballots)
        self.assertIn("invalid JSON", raised.exception.message)

    def test_negative_and_zero_share_ranges_are_refused(self):
        result = self.reconcile_rows(
            [fx.roster_row("P", "H", 10)], [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 5, 5)], [])
        self.assertFalse(result["complete"])
        self.assertIn("grant-unit-invalid",
                      [p["code"] for p in result["positions"][0]["problems"]])

    def test_cutoff_before_record_date_is_refused_before_parsing(self):
        with ProxyVoteApi() as api:
            with self.assertRaises(ProxyVoteInputError):
                api.reconcile_documents(
                    meeting_id=fx.MEETING, record_date="2026-03-05",
                    cutoff_at="2026-03-01T00:00:00Z",
                    roster=[], proposals=[], grants=[], ballots=[])


if __name__ == "__main__":
    unittest.main()
