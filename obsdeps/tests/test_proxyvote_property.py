"""Randomized cross-check of the engine against the per-share enumerator.

Small deterministic pseudo-random meetings (fixed seed, hand-written generator,
no external dependency) are reconciled by both implementations.  Any mismatch
is reported with the exact case so it can be reproduced by hand.

The enumerator is intentionally naive; it is only ever used on small share
counts and only here.  The production engine never imports it.
"""

from __future__ import annotations

import random
import unittest

from support import SRC  # noqa: F401

from obsdeps.proxyvote import ProxyVoteApi
from proxyvote_enumerator import enumerate

import proxyvote_fixtures as fx

PROPOSALS = ("Q1", "Q2")


def random_meeting(rng: random.Random) -> dict:
    positions = []
    grants = []
    ballots = []
    for p_index in range(rng.randint(1, 2)):
        position_id = f"P{p_index}"
        shares = rng.randint(3, 9)
        position = fx.roster_row(position_id, f"H{p_index}", shares)
        positions.append(position)
        holders = {position["holder_id"]}
        for g_index in range(rng.randint(0, 3)):
            start = rng.randint(0, shares - 1)
            end = rng.randint(start + 1, shares)
            proposal_ids = tuple(p for p in PROPOSALS
                                 if rng.random() < 0.8) or ("Q1",)
            parent = None
            grantor = position["holder_id"]
            if grants and rng.random() < 0.5:
                parent = rng.choice(grants)
                if parent["position_id"] != position_id:
                    parent = None
            if parent is not None:
                grantor = parent["grantee"]
                start = max(start, parent["unit_start"])
                end = min(end, parent["unit_end"])
                if start >= end:
                    continue
                proposal_ids = tuple(p for p in proposal_ids
                                     if p in parent["proposal_ids"])
                if not proposal_ids:
                    continue
            effective = rng.choice(["2026-03-02T00:00:00Z",
                                    "2026-03-05T00:00:00Z"])
            revoked = rng.choice([None, None, "2026-03-04T00:00:00Z",
                                  "2026-03-06T00:00:00Z"])
            if revoked is not None and revoked <= effective:
                revoked = None
            grant = fx.grant_row(
                f"G{p_index}-{g_index}", position_id, grantor,
                f"V{rng.randint(0, 4)}", start, end,
                proposal_ids=proposal_ids,
                effective_at=effective, revoked_at=revoked,
                parent_grant_id=parent["grant_id"] if parent else None)
            # Keep the generated graph legal: a dead parent must not be
            # silently resurrected by a live child, and grantor identity must
            # match the parent's grantee (already enforced above).
            if parent is not None and parent["revoked_at"] is not None \
                    and parent["revoked_at"] <= effective:
                continue
            grants.append(grant)
        voters = sorted({g["grantee"] for g in grants
                         if g["position_id"] == position_id}
                        | {position["holder_id"]})
        for b_index in range(rng.randint(0, 5)):
            ballot = fx.ballot_row(
                f"B{p_index}-{b_index}", position_id,
                start := rng.randint(0, shares - 1),
                rng.randint(start + 1, shares),
                rng.choice(voters),
                choice=rng.choice(["X", "Y", "ABSTAIN"]),
                proposal_id=rng.choice(PROPOSALS),
                submitted_at=rng.choice(["2026-03-04T00:00:00Z",
                                         "2026-03-06T00:00:00Z"]))
            ballots.append(ballot)
    proposals = [fx.proposal_row("Q1"), fx.proposal_row("Q2")]
    return {"meeting_id": fx.MEETING, "record_date": fx.RECORD_DATE,
            "cutoff_at": fx.CUTOFF, "roster": positions,
            "proposals": proposals, "grants": grants, "ballots": ballots}


class RandomizedAgreement(unittest.TestCase):
    def test_engine_matches_per_share_enumerator(self):
        rng = random.Random(20260305)
        with ProxyVoteApi() as api:
            for case in range(60):
                documents = random_meeting(rng)
                result = api.reconcile_documents(**documents)
                reference = enumerate(
                    documents["roster"], documents["grants"],
                    documents["ballots"], documents["proposals"],
                    cutoff_at=fx.CUTOFF)
                self.assertFalse(result["complete"] is None)
                for proposal in result["proposals"]:
                    pid = proposal["proposal_id"]
                    expected = reference[pid]
                    with self.subTest(case=case, proposal=pid,
                                      documents=documents):
                        self.assertEqual(proposal["by_choice"],
                                         expected["by_choice"])
                        self.assertEqual(proposal["abstain"],
                                         expected["abstain"])
                        self.assertEqual(proposal["contested"],
                                         expected["contested"])
                        self.assertEqual(proposal["invalid"],
                                         expected["invalid"])
                        # Independent totals must be reconstructible by
                        # summing non-overlapping segments.
                        covered = sum(
                            segment["unit_end"] - segment["unit_start"]
                            for segment in proposal["segments"])
                        self.assertEqual(
                            covered,
                            sum(proposal["by_choice"].values())
                            + proposal["abstain"] + proposal["contested"])
                        self.assertEqual(
                            covered, sum(p["shares"] for p in
                                         documents["roster"]))


if __name__ == "__main__":
    unittest.main()
