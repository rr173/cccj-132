"""Fixtures for the proxy-vote tests.

Every expected number in the tests is derived on paper (see the docstrings) and
re-derived independently by ``proxyvote_enumerator``; none is produced by the
production engine.  Ranges are half-open ``[unit_start, unit_end)`` throughout.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Sequence

MEETING = "M-2026"
RECORD_DATE = "2026-03-01"
CUTOFF = "2026-03-05T00:00:00Z"


def roster_row(position_id: str, holder_id: str, shares: int,
               *, record_date: str = RECORD_DATE,
               security_class: str = "COMMON",
               meeting_id: str = MEETING) -> Dict[str, Any]:
    return {"meeting_id": meeting_id, "position_id": position_id,
            "holder_id": holder_id, "security_class": security_class,
            "record_date": record_date, "shares": shares}


def proposal_row(proposal_id: str, options: Sequence[str] = ("X", "Y", "ABSTAIN"),
                 *, title: str = "", meeting_id: str = MEETING) -> Dict[str, Any]:
    return {"meeting_id": meeting_id, "proposal_id": proposal_id,
            "title": title or proposal_id, "options": list(options)}


def grant_row(grant_id: str, position_id: str, grantor: str, grantee: str,
              unit_start: int, unit_end: int, *,
              proposal_ids: Sequence[str] = ("Q",),
              effective_at: str = "2026-03-02T00:00:00Z",
              revoked_at: str = None, parent_grant_id: str = None,
              meeting_id: str = MEETING) -> Dict[str, Any]:
    return {"meeting_id": meeting_id, "grant_id": grant_id,
            "position_id": position_id, "grantor": grantor,
            "grantee": grantee, "unit_start": unit_start,
            "unit_end": unit_end, "proposal_ids": list(proposal_ids),
            "effective_at": effective_at, "revoked_at": revoked_at,
            "parent_grant_id": parent_grant_id}


def ballot_row(ballot_id: str, position_id: str, unit_start: int, unit_end: int,
               voter: str, *, choice: str = "X", proposal_id: str = "Q",
               submitted_at: str = "2026-03-04T00:00:00Z",
               meeting_id: str = MEETING) -> Dict[str, Any]:
    return {"meeting_id": meeting_id, "ballot_id": ballot_id,
            "position_id": position_id, "unit_start": unit_start,
            "unit_end": unit_end, "voter": voter,
            "proposal_id": proposal_id, "choice": choice,
            "submitted_at": submitted_at}


def write_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


class Documents:
    """Four JSONL files on disk, plus the in-memory rows for comparison."""

    def __init__(self, directory: str, roster, proposals, grants, ballots):
        self.directory = directory
        self.rows = {"roster": list(roster), "proposals": list(proposals),
                     "grants": list(grants), "ballots": list(ballots)}
        self.paths = {
            name: write_jsonl(os.path.join(directory, f"{name}.jsonl"), rows)
            for name, rows in self.rows.items()
        }

    def arguments(self) -> Dict[str, Any]:
        return {"meeting_id": MEETING, "record_date": RECORD_DATE,
                "cutoff_at": CUTOFF, **self.paths}

    def documents(self) -> Dict[str, Any]:
        return {"meeting_id": MEETING, "record_date": RECORD_DATE,
                "cutoff_at": CUTOFF, **self.rows}


# --------------------------------------------------------------------------- #
# The hand-worked scenario from the request
# --------------------------------------------------------------------------- #


def worked_roster() -> List[Dict[str, Any]]:
    return [roster_row("P", "H", 100)]


def worked_proposals() -> List[Dict[str, Any]]:
    return [proposal_row("Q")]


def worked_grants(*, with_c: bool = False) -> List[Dict[str, Any]]:
    grants = [
        # H owns [0,100).  H delegates [20,80) to A.
        grant_row("G1", "P", "H", "A", 20, 80),
        # A re-delegates [40,70) to B.  A keeps [20,40) and [70,80).
        grant_row("G2", "P", "A", "B", 40, 70, parent_grant_id="G1",
                  effective_at="2026-03-03T00:00:00Z"),
    ]
    if with_c:
        # H separately delegates [35,50) to C; C overlaps A's [35,40) and B's
        # [40,50) with an independent live chain.
        grants.append(grant_row("G3", "P", "H", "C", 35, 50))
    return grants


def worked_ballots(*, with_c: bool = False) -> List[Dict[str, Any]]:
    ballots = [
        # H votes for the whole position; only [0,20) and [80,100) remain.
        ballot_row("BH", "P", 0, 100, "H", choice="X"),
        # A votes for its whole granted range; B's sub-range is already gone.
        ballot_row("BA", "P", 20, 80, "A", choice="X"),
        # B votes for its sub-delegated range.
        ballot_row("BB", "P", 40, 70, "B", choice="X"),
    ]
    if with_c:
        ballots.append(ballot_row("BC", "P", 35, 50, "C", choice="Y"))
    return ballots


def worked_documents(directory: str, *, with_c: bool = False) -> Documents:
    return Documents(directory, worked_roster(), worked_proposals(),
                     worked_grants(with_c=with_c),
                     worked_ballots(with_c=with_c))
