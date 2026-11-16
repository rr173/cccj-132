#!/usr/bin/env python3
"""Proxy-vote reconciliation worked example with paper-stated expectations.

The meetings below are derived by hand (see the comments and
``docs/PROXYVOTE.md``), never produced by the engine, so agreement is an
independent check of the implementation.

Scenario A -- the H -> A -> B chain from the requirement::

    position P, holder H, 100 shares, range [0, 100)
    H grants [20, 80) to A
    A grants [40, 70) to B
    H, A and B all vote "X" for proposal Q

    H owns [0, 20) and [80, 100) = 40 shares
    A owns [20, 40) and [70, 80) = 20 shares
    B owns [40, 70)             = 30 shares
    every share is X exactly once  ->  X = 100, no abstain, no contest

Scenario B -- add a second H grant [35, 50) to C, C votes "Y"::

    [35, 40) is claimed by A (via G1) and C (via G3)
    [40, 50) is claimed by B (via G1,G2) and C (via G3)
    only [35, 50) = 15 shares become contested; X stays 85,
    contested = 15, and every other segment keeps its owner.

Scenario C -- widen A's ballot to [10, 90) without touching the grants::

    A's authority is [20, 40) and [70, 80).  The widened part [10, 20),
    [40, 70) and [80, 90) is outside it and rejected (50 units); A's valid
    [20, 40) and [70, 80) still counts in full, so X stays 100 overall.  A
    widened ballot never changes the grants and never widens authority.

Scenario D -- move G1's revocation before the cutoff::

    the H -> A path is no longer live, so A and B lose their authority;
    H's ballot covers only its own 100 shares, but A's and B's ballots are
    now wholly outside authority.  X = 0 is *not* the answer: H's ballot is
    still there.  The example asserts H's ownership path and that G1 is
    reported not live.

Usage::

    python3 examples/run_proxyvote_example.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from obsdeps.proxyvote import ProxyVoteApi  # noqa: E402

MEETING = "M-EXAMPLE"
RECORD_DATE = "2026-03-01"
CUTOFF = "2026-03-05T00:00:00Z"


def roster(shares: int = 100):
    return [{"meeting_id": MEETING, "position_id": "P", "holder_id": "H",
             "security_class": "COMMON", "record_date": RECORD_DATE,
             "shares": shares}]


def proposals():
    return [{"meeting_id": MEETING, "proposal_id": "Q", "title": "Q",
             "options": ["X", "Y", "ABSTAIN"]}]


def grants(*, with_c: bool = False, revoked: bool = False):
    rows = [
        {"meeting_id": MEETING, "grant_id": "G1", "position_id": "P",
         "grantor": "H", "grantee": "A", "unit_start": 20, "unit_end": 80,
         "proposal_ids": ["Q"], "effective_at": "2026-03-02T00:00:00Z",
         "revoked_at": "2026-03-04T00:00:00Z" if revoked else None,
         "parent_grant_id": None},
        {"meeting_id": MEETING, "grant_id": "G2", "position_id": "P",
         "grantor": "A", "grantee": "B", "unit_start": 40, "unit_end": 70,
         "proposal_ids": ["Q"], "effective_at": "2026-03-03T00:00:00Z",
         "revoked_at": None, "parent_grant_id": "G1"},
    ]
    if with_c:
        rows.append({"meeting_id": MEETING, "grant_id": "G3",
                     "position_id": "P", "grantor": "H", "grantee": "C",
                     "unit_start": 35, "unit_end": 50,
                     "proposal_ids": ["Q"],
                     "effective_at": "2026-03-02T00:00:00Z",
                     "revoked_at": None, "parent_grant_id": None})
    return rows


def ballots(*, with_c: bool = False, widened: bool = False):
    rows = [
        {"meeting_id": MEETING, "ballot_id": "BH", "position_id": "P",
         "unit_start": 0, "unit_end": 100, "voter": "H",
         "proposal_id": "Q", "choice": "X",
         "submitted_at": "2026-03-04T00:00:00Z"},
        {"meeting_id": MEETING, "ballot_id": "BA", "position_id": "P",
         "unit_start": 10 if widened else 20,
         "unit_end": 90 if widened else 80, "voter": "A",
         "proposal_id": "Q", "choice": "X",
         "submitted_at": "2026-03-04T00:00:00Z"},
        {"meeting_id": MEETING, "ballot_id": "BB", "position_id": "P",
         "unit_start": 40, "unit_end": 70, "voter": "B",
         "proposal_id": "Q", "choice": "X",
         "submitted_at": "2026-03-04T00:00:00Z"},
    ]
    if with_c:
        rows.append({"meeting_id": MEETING, "ballot_id": "BC",
                     "position_id": "P", "unit_start": 35, "unit_end": 50,
                     "voter": "C", "proposal_id": "Q", "choice": "Y",
                     "submitted_at": "2026-03-04T00:00:00Z"})
    return rows


def reconcile(directory, *, with_c=False, widened=False, revoked=False,
              cutoff=CUTOFF):
    paths = {}
    for name, rows in (("roster", roster()), ("proposals", proposals()),
                       ("grants", grants(with_c=with_c, revoked=revoked)),
                       ("ballots", ballots(with_c=with_c, widened=widened))):
        path = os.path.join(directory, f"{name}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        paths[name] = path
    with ProxyVoteApi() as api:
        return api.reconcile_files(
            meeting_id=MEETING, record_date=RECORD_DATE, cutoff_at=cutoff,
            **paths)


def table(proposal):
    return [(segment["unit_start"], segment["unit_end"],
             segment["outcome"], segment["voter"],
             tuple(segment["authority_path"])) for segment in
            proposal["segments"]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="print and keep the temporary input directory")
    parser.add_argument("--db", default=None,
                        help="also store the first reconciliation in this SQLite file")
    arguments = parser.parse_args(argv)
    directory = tempfile.mkdtemp(prefix="proxyvote-example-")

    scenario_a = reconcile(directory)
    proposal_a = scenario_a["proposals"][0]
    assert proposal_a["by_choice"]["X"] == 100, proposal_a
    assert proposal_a["abstain"] == 0
    assert proposal_a["contested"] == 0
    assert table(proposal_a) == [
        (0, 20, "X", "H", ()),
        (20, 40, "X", "A", ("G1",)),
        (40, 70, "X", "B", ("G1", "G2")),
        (70, 80, "X", "A", ("G1",)),
        (80, 100, "X", "H", ()),
    ]
    print("Scenario A: X =", proposal_a["by_choice"]["X"],
          "abstain =", proposal_a["abstain"],
          "contested =", proposal_a["contested"])

    scenario_b = reconcile(directory, with_c=True)
    proposal_b = scenario_b["proposals"][0]
    assert proposal_b["by_choice"]["X"] == 85, proposal_b
    assert proposal_b["by_choice"]["Y"] == 0
    assert proposal_b["contested"] == 15
    assert table(proposal_b) == [
        (0, 20, "X", "H", ()),
        (20, 35, "X", "A", ("G1",)),
        (35, 40, "contested", "", ()),
        (40, 50, "contested", "", ()),
        (50, 70, "X", "B", ("G1", "G2")),
        (70, 80, "X", "A", ("G1",)),
        (80, 100, "X", "H", ()),
    ]
    print("Scenario B: X =", proposal_b["by_choice"]["X"],
          "contested =", proposal_b["contested"],
          "(only [35, 50) is affected)")

    scenario_c = reconcile(directory, widened=True)
    proposal_c = scenario_c["proposals"][0]
    invalid = {record["ballot_id"]: record
               for record in proposal_c["invalid_ballots"]}
    assert proposal_c["by_choice"]["X"] == 100, proposal_c
    assert invalid["BA"]["outside"] == [[10, 20], [40, 70], [80, 90]], invalid
    assert invalid["BA"]["units"] == 50
    print("Scenario C: widened A ballot rejected", invalid["BA"]["units"],
          "units outside authority; valid parts stay counted; X =",
          proposal_c["by_choice"]["X"])

    scenario_d = reconcile(directory, revoked=True)
    proposal_d = scenario_d["proposals"][0]
    grant_d = {grant["grant_id"]: grant
               for grant in scenario_d["positions"][0]["grants"]}
    assert grant_d["G1"]["live_at_cutoff"] is False
    assert grant_d["G1"]["live_basis"] == "revoked"
    assert grant_d["G2"]["live_at_cutoff"] is False
    assert grant_d["G2"]["live_basis"] == "parent-not-live"
    assert table(proposal_d) == [(0, 100, "X", "H", ())]
    assert proposal_d["invalid"] == 90
    print("Scenario D: revoked G1 -> A and B lose authority; X =",
          proposal_d["by_choice"]["X"], "invalid units =",
          proposal_d["invalid"])

    if arguments.db:
        stored = ProxyVoteApi(arguments.db)
        try:
            outcome = stored.register(
                meeting_id=MEETING, record_date=RECORD_DATE, cutoff_at=CUTOFF,
                roster=os.path.join(directory, "roster.jsonl"),
                proposals=os.path.join(directory, "proposals.jsonl"),
                grants=os.path.join(directory, "grants.jsonl"),
                ballots=os.path.join(directory, "ballots.jsonl"),
                created_at="2026-03-06T00:00:00Z")
            print("Stored:", outcome["reconciliation_id"])
        finally:
            stored.close()

    if arguments.keep:
        print("inputs kept in", directory)
    else:
        for name in os.listdir(directory):
            os.unlink(os.path.join(directory, name))
        os.rmdir(directory)
    print("proxyvote example matches hand-written expectations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
