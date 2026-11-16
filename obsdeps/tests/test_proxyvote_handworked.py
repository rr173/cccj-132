"""Hand-worked proxy-vote reconciliations.

Every expectation below is computed on paper in the test's docstring and then
re-derived by the independent per-share enumerator in
``proxyvote_enumerator.py``.  The production engine is only *checked against*
those two sources; it is never used to produce an expectation.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from support import SRC  # noqa: F401  (ensures src/ is on sys.path)

from obsdeps.proxyvote import ProxyVoteApi
from proxyvote_enumerator import enumerate

import proxyvote_fixtures as fx


def by_choice(proposal, option):
    return proposal["by_choice"][option]


def segments(proposal):
    return [(s["unit_start"], s["unit_end"], s["outcome"], s["voter"],
             tuple(s["authority_path"]), tuple(s["ballot_ids"]))
            for s in proposal["segments"]]


def units_covered(proposal):
    return sum(s["unit_end"] - s["unit_start"] for s in proposal["segments"])


class WorkedScenario(unittest.TestCase):
    """The H -> A -> B worked example from the requirement."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def reconcile(self, documents):
        with ProxyVoteApi() as api:
            return api.reconcile_documents(**documents)

    def test_chain_counts_every_share_exactly_once(self):
        # Paper: H [0,20)+[80,100)=40, A [20,40)+[70,80)=20, B [40,70)=30.
        # All three vote X, so X = 40 + 20 + 30 = 90?  No -- the grantee
        # ballots cover exactly what they own: A's ballot is [20,80) but A
        # owns only 30 units, B owns 30, H owns 40; every unit is X exactly
        # once, so X = 100 with no invalid part.  See the fixture ballots:
        # A's ballot is clipped to its ownership, and because it is *inside*
        # a live child grant, the clipped-out [40,70) is "outside authority".
        documents = fx.worked_documents(self.directory).documents()
        result = self.reconcile(documents)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 100)
        self.assertEqual(proposal["abstain"], 0)
        self.assertEqual(proposal["contested"], 0)
        self.assertEqual(units_covered(proposal), 100)
        self.assertEqual(segments(proposal), [
            (0, 20, "X", "H", (), ("BH",)),
            (20, 40, "X", "A", ("G1",), ("BA",)),
            (40, 70, "X", "B", ("G1", "G2"), ("BB",)),
            (70, 80, "X", "A", ("G1",), ("BA",)),
            (80, 100, "X", "H", (), ("BH",)),
        ])
        self.assertTrue(result["complete"])

    def test_independent_enumerator_agrees(self):
        documents = fx.worked_documents(self.directory)
        result = self.reconcile(documents.documents())
        reference = enumerate(
            documents.rows["roster"], documents.rows["grants"],
            documents.rows["ballots"], documents.rows["proposals"],
            cutoff_at=fx.CUTOFF)["Q"]
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"], reference["by_choice"])
        self.assertEqual(proposal["abstain"], reference["abstain"])
        self.assertEqual(proposal["contested"], reference["contested"])
        self.assertEqual(proposal["invalid"], reference["invalid"])
        self.assertEqual(units_covered(proposal), 100)

    def test_adding_conflicting_c_grant_isolates_the_overlap(self):
        # C takes [35,50) from H and votes Y there.  A still owns [20,35) and
        # B [50,70), A also [70,80); the intersection [35,50) has two owners
        # (A on [35,40), B on [40,50)) and C, with X and Y both claimed, so it
        # is contested and nothing else changes.  X = 100 - 15 = 85,
        # contested = 15, and the remaining segment ownership is unchanged.
        documents = fx.worked_documents(self.directory, with_c=True)
        result = self.reconcile(documents.documents())
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 85)
        self.assertEqual(proposal["by_choice"]["Y"], 0)
        self.assertEqual(proposal["contested"], 15)
        self.assertEqual(proposal["abstain"], 0)
        self.assertEqual(units_covered(proposal), 100)
        self.assertEqual(segments(proposal), [
            (0, 20, "X", "H", (), ("BH",)),
            (20, 35, "X", "A", ("G1",), ("BA",)),
            (35, 40, "contested", "", (),
             ("BA", "BC")),
            (40, 50, "contested", "", (), ("BB", "BC")),
            (50, 70, "X", "B", ("G1", "G2"), ("BB",)),
            (70, 80, "X", "A", ("G1",), ("BA",)),
            (80, 100, "X", "H", (), ("BH",)),
        ])
        self.assertTrue(result["complete"])
        reference = enumerate(
            documents.rows["roster"], documents.rows["grants"],
            documents.rows["ballots"], documents.rows["proposals"],
            cutoff_at=fx.CUTOFF)["Q"]
        self.assertEqual(proposal["by_choice"], reference["by_choice"])
        self.assertEqual(proposal["contested"], reference["contested"])

    def test_contested_span_keeps_its_competing_paths(self):
        documents = fx.worked_documents(self.directory, with_c=True)
        result = self.reconcile(documents.documents())
        contested = [s for s in result["proposals"][0]["segments"]
                     if s["outcome"] == "contested"]
        self.assertEqual(len(contested), 2)
        alternatives = contested[0]["alternatives"]
        self.assertEqual(
            {(a["voter"], tuple(a["path"])) for a in alternatives},
            {("A", ("G1",)), ("C", ("G3",))})


class OutputShape(unittest.TestCase):
    """Every total must be recomputable from non-overlapping segments."""

    def test_segments_reconstruct_every_summary_field(self):
        directory = tempfile.mkdtemp()
        documents = fx.worked_documents(directory, with_c=True)
        with ProxyVoteApi() as api:
            result = api.reconcile_documents(**documents.documents())
        self.assertIn("roster_version", result)
        self.assertIn("cutoff_utc", result)
        self.assertEqual(set(result["sources"]),
                         {"roster", "proposals", "grants", "ballots"})
        for name, source in result["sources"].items():
            self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(source["bytes"], 0)

        proposal = result["proposals"][0]
        # Proposal totals equal the sum over position totals.
        positions_total = {
            option: sum(entry["by_choice"][option]
                        for position in result["positions"]
                        for entry in position["proposals"])
            for option in proposal["options"]}
        self.assertEqual(proposal["by_choice"], positions_total)
        # Every segment carries the required evidence and the tiling is exact.
        cursor = 0
        counted: Dict[str, int] = {option: 0 for option in proposal["options"]}
        abstain = contested = 0
        for segment in sorted(proposal["segments"], key=lambda s: s["unit_start"]):
            self.assertEqual(segment["unit_start"], cursor)
            cursor = segment["unit_end"]
            for field in ("position_id", "proposal_id", "unit_start",
                          "unit_end", "units", "outcome", "voter",
                          "authority_path", "ballot_ids", "alternatives"):
                self.assertIn(field, segment)
            units = segment["unit_end"] - segment["unit_start"]
            self.assertEqual(segment["units"], units)
            if segment["outcome"] == "contested":
                contested += units
            elif segment["outcome"] == "abstain":
                abstain += units
            else:
                counted[segment["outcome"]] += units
        self.assertEqual(cursor, 100)
        self.assertEqual(counted, proposal["by_choice"])
        self.assertEqual(abstain, proposal["abstain"])
        self.assertEqual(contested, proposal["contested"])


class AuthorityBoundaries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def reconcile(self, roster, proposals, grants, ballots):
        documents = fx.Documents(self.directory, roster, proposals, grants,
                                 ballots)
        with ProxyVoteApi() as api:
            return api.reconcile_documents(**documents.documents()), documents

    def test_two_positions_one_holder_are_not_merged(self):
        # H owns P1 (10 shares) and P2 (20 shares).  A ballot is only authority
        # for P1; H's ballot on P2 cannot be attributed to P1.
        result, documents = self.reconcile(
            [fx.roster_row("P1", "H", 10), fx.roster_row("P2", "H", 20)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P1", "H", "A", 0, 10)],
            [fx.ballot_row("B1", "P1", 0, 10, "A"),
             fx.ballot_row("B2", "P2", 0, 20, "H")])
        positions = {p["position_id"]: p for p in result["positions"]}
        self.assertEqual(positions["P1"]["proposals"][0]["by_choice"]["X"], 10)
        self.assertEqual(positions["P2"]["proposals"][0]["by_choice"]["X"], 20)
        self.assertEqual([p["holder_id"] for p in result["positions"]],
                         ["H", "H"])

    def test_two_disjoint_grants_for_one_position(self):
        # H grants [0,40) to A and [60,100) to B.  H keeps [40,60).
        result, documents = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 40),
             fx.grant_row("G2", "P", "H", "B", 60, 100)],
            [fx.ballot_row("B1", "P", 0, 40, "A"),
             fx.ballot_row("B2", "P", 60, 100, "B"),
             fx.ballot_row("B3", "P", 0, 100, "H")])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 100)
        self.assertEqual(segments(proposal), [
            (0, 40, "X", "A", ("G1",), ("B1",)),
            (40, 60, "X", "H", (), ("B3",)),
            (60, 100, "X", "B", ("G2",), ("B2",)),
        ])
        self.assertEqual(proposal["invalid"], 80)  # H's 40 + A's 40 outside
        self.assertTrue(result["complete"])

    def test_scope_limited_parent_blocks_child_extra_proposal(self):
        # G1 only authorizes Q1.  On Q2 A has no authority, so A's Q2 ballot
        # is wholly invalid and H's Q2 ballot stands.
        result, documents = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q1"), fx.proposal_row("Q2")],
            [fx.grant_row("G1", "P", "H", "A", 0, 100, proposal_ids=("Q1",))],
            [fx.ballot_row("B1", "P", 0, 100, "A", proposal_id="Q1"),
             fx.ballot_row("B2", "P", 0, 100, "A", proposal_id="Q2",
                           choice="Y"),
             fx.ballot_row("B3", "P", 0, 100, "H", proposal_id="Q2")])
        proposals = {p["proposal_id"]: p for p in result["proposals"]}
        self.assertEqual(proposals["Q1"]["by_choice"]["X"], 100)
        self.assertEqual(proposals["Q1"]["invalid"], 0)
        self.assertEqual(proposals["Q2"]["by_choice"]["X"], 100)
        self.assertEqual(proposals["Q2"]["by_choice"]["Y"], 0)
        self.assertEqual(proposals["Q2"]["invalid"], 100)
        self.assertTrue(result["complete"])

    def test_scope_limited_parent_forbids_child_extra_proposal(self):
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q1"), fx.proposal_row("Q2")],
            [fx.grant_row("G1", "P", "H", "A", 0, 100, proposal_ids=("Q1",)),
             fx.grant_row("G2", "P", "A", "B", 10, 20,
                          proposal_ids=("Q1", "Q2"), parent_grant_id="G1")],
            [])
        self.assertFalse(result["complete"])
        self.assertIn("grant-proposals-exceed-parent",
                      [p["code"] for p in result["positions"][0]["problems"]])

    def test_child_outside_parent_range_quarantines_the_position(self):
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 20, 80),
             fx.grant_row("G2", "P", "A", "B", 10, 50, parent_grant_id="G1")],
            [])
        self.assertFalse(result["complete"])
        position = result["positions"][0]
        self.assertEqual(position["status"], "incomplete")
        self.assertIn("grant-exceeds-parent",
                      [p["code"] for p in position["problems"]])
        self.assertEqual(position["proposals"], [])

    def test_cycle_is_an_input_error(self):
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "A", "B", 0, 50, parent_grant_id="G2"),
             fx.grant_row("G2", "P", "B", "A", 0, 40, parent_grant_id="G1")],
            [])
        self.assertFalse(result["complete"])
        self.assertIn("grant-cycle",
                      [p["code"] for p in result["positions"][0]["problems"]])

    def test_same_grantee_twice_counts_once_and_is_contested(self):
        # H delegates [20,60) to A and also [40,80) to A.  A owns the union
        # once: [20,40) only via G1, [60,80) only via G2, and [40,60) via both.
        # Two live chains reach [40,60), so that span is contested; the rest
        # of A's claim is unaffected and A's ballot still counts there.
        result, documents = self.reconcile(
            [fx.roster_row("P", "H", 100)], [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 20, 60),
             fx.grant_row("G2", "P", "H", "A", 40, 80)],
            [fx.ballot_row("BH", "P", 0, 100, "H"),
             fx.ballot_row("BA", "P", 20, 80, "A")])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 80)
        self.assertEqual(proposal["contested"], 20)
        self.assertEqual(segments(proposal), [
            (0, 20, "X", "H", (), ("BH",)),
            (20, 40, "X", "A", ("G1",), ("BA",)),
            (40, 60, "contested", "", (), ("BA",)),
            (60, 80, "X", "A", ("G2",), ("BA",)),
            (80, 100, "X", "H", (), ("BH",)),
        ])
        reference = enumerate(
            documents.rows["roster"], documents.rows["grants"],
            documents.rows["ballots"], documents.rows["proposals"],
            cutoff_at=fx.CUTOFF)["Q"]
        self.assertEqual(proposal["contested"], reference["contested"])
        self.assertEqual(proposal["by_choice"], reference["by_choice"])

    def test_grant_effective_exactly_at_cutoff_is_live(self):
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 50,
                          effective_at=fx.CUTOFF)],
            [fx.ballot_row("B1", "P", 0, 50, "A")])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 50)
        self.assertEqual(segments(proposal)[0][:5], (0, 50, "X", "A", ("G1",)))

    def test_grant_revoked_exactly_at_cutoff_is_not_live(self):
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 50,
                          effective_at="2026-03-01T00:00:00Z",
                          revoked_at=fx.CUTOFF)],
            [fx.ballot_row("B1", "P", 0, 50, "A")])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 0)
        self.assertEqual(proposal["abstain"], 100)
        self.assertEqual(proposal["invalid"], 50)

    def test_deep_legal_chain_is_handled_without_recursion_limits(self):
        # A 1200-level re-delegation chain is legal: each child sits inside its
        # parent, is issued by the parent's grantee, and names the parent's
        # proposal.  The last grantee owns [0, shares-1200); nobody owns the
        # tail, so the holder keeps it.  The engine must not recurse per level.
        depth = 1200
        shares = 4000
        grants = []
        parent = None
        for level in range(depth):
            grants.append(fx.grant_row(
                f"G{level}", "P", "H" if level == 0 else f"A{level - 1}",
                f"A{level}", 0, shares - level - 1,
                parent_grant_id=parent,
                effective_at="2026-03-02T00:00:00Z"))
            parent = f"G{level}"
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", shares)], [fx.proposal_row("Q")], grants,
            [fx.ballot_row("B1", "P", 0, shares - depth, f"A{depth - 1}")])
        proposal = result["proposals"][0]
        self.assertTrue(result["complete"])
        self.assertEqual(proposal["by_choice"]["X"], shares - depth)
        self.assertEqual(proposal["abstain"], depth)
        self.assertEqual(len(proposal["segments"]), depth + 1)

    def test_every_grant_on_a_cycle_is_reported(self):
        # G2 -> G1 -> G2: both members are on the cycle, not just the entry.
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)], [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "A", "B", 0, 50, parent_grant_id="G2"),
             fx.grant_row("G2", "P", "B", "A", 0, 40, parent_grant_id="G1")],
            [])
        problems = result["positions"][0]["problems"]
        cycles = [p for p in problems if p["code"] == "grant-cycle"]
        self.assertEqual(sorted(p["details"]["grant_id"] for p in cycles),
                         ["G1", "G2"])
        self.assertFalse(result["complete"])

    def test_grant_signed_after_record_date_but_live_at_cutoff_counts(self):
        result, _ = self.reconcile(
            [fx.roster_row("P", "H", 100)],
            [fx.proposal_row("Q")],
            [fx.grant_row("G1", "P", "H", "A", 0, 100,
                          effective_at="2026-03-04T00:00:00Z")],
            [fx.ballot_row("B1", "P", 0, 100, "A")])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["by_choice"]["X"], 100)
        self.assertTrue(result["complete"])


if __name__ == "__main__":
    unittest.main()
