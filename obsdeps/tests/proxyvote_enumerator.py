"""Independent, deliberately naive per-share reference enumerator.

This module exists to cross-check the production engine on *small* samples.
It shares no code with ``obsdeps.proxyvote.engine``: it re-implements the
authority walk, ballot clipping and conflict rules by literally iterating over
every share index ``range(shares)`` for every proposal.  The production engine
never imports this module, and it is documented as unsuitable for large share
counts (its cost is O(shares x ballots)).

Usage from the tests::

    from proxyvote_enumerator import enumerate
    expected = enumerate(meeting_rows, proposal_id="Q")
    # expected["by_choice"], expected["abstain"], expected["contested"],
    # expected["invalid"], expected["segments"] (per-unit ownership)
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _parse(text: str) -> dt.datetime:
    # Accepts ``Z`` and numeric offsets without the library's parser.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return dt.datetime.fromisoformat(text).astimezone(dt.timezone.utc)


def enumerate(roster: Sequence[Dict[str, Any]],
              grants: Sequence[Dict[str, Any]],
              ballots: Sequence[Dict[str, Any]],
              proposals: Sequence[Dict[str, Any]],
              *, cutoff_at: str,
              proposal_id: Optional[str] = None) -> Dict[str, Any]:
    """Enumerate every share, ballot and grant; return totals and per-unit map."""
    cutoff = _parse(cutoff_at)
    positions = {row["position_id"]: row for row in roster}
    proposal_map = {row["proposal_id"]: row for row in proposals}
    selected = [proposal_id] if proposal_id is not None else sorted(proposal_map)

    report: Dict[str, Any] = {}
    for pid in selected:
        option_list = list(proposal_map[pid]["options"])
        by_choice = {option: 0 for option in option_list}
        abstain = contested = invalid = 0
        units: List[List[Any]] = []

        for position_id, position in positions.items():
            shares = int(position["shares"])
            holder = position["holder_id"]
            position_grants = {g["grant_id"]: g for g in grants
                               if g["position_id"] == position_id}
            live = _live_grants(position_grants, cutoff)

            # Owned chains per share, and the set of shares each voter owns.
            owners_by_unit: Dict[int, List[Tuple[str, List[str]]]] = {}
            owned_by_voter: Dict[str, set] = {}
            for unit in range(shares):
                owners = _owners(pid, unit, position_id, holder,
                                 position_grants, live)
                owners_by_unit[unit] = owners
                for voter, _path in owners:
                    owned_by_voter.setdefault(voter, set()).add(unit)

            # Invalid units: every submitted (pre-cutoff) ballot share that the
            # voter does not own for this proposal.  Digits are enumerated one
            # by one, deliberately mirroring the requirement's wording.
            for ballot in ballots:
                if ballot["position_id"] != position_id \
                        or ballot["proposal_id"] != pid:
                    continue
                if _parse(ballot["submitted_at"]) > cutoff:
                    invalid += ballot["unit_end"] - ballot["unit_start"]
                    continue
                owned = owned_by_voter.get(ballot["voter"], set())
                for unit in range(ballot["unit_start"], ballot["unit_end"]):
                    if unit not in owned:
                        invalid += 1

            for unit in range(shares):
                owners = owners_by_unit[unit]
                if len(owners) > 1:
                    units.append([position_id, unit, "contested", "", []])
                    contested += 1
                    continue
                voter, path = owners[0]
                ballot = _resolved_ballot(ballots, position_id, pid, voter,
                                          unit, cutoff)
                if ballot is None:
                    units.append([position_id, unit, "abstain", voter, path])
                    abstain += 1
                elif ballot[0] == "conflict":
                    units.append([position_id, unit, "contested", voter, path])
                    contested += 1
                else:
                    choice, ballot_ids = ballot
                    units.append([position_id, unit, choice, voter, path])
                    by_choice[choice] += 1

        report[pid] = {
            "by_choice": by_choice,
            "abstain": abstain,
            "contested": contested,
            "invalid": invalid,
            "total": sum(by_choice.values()) + abstain + contested,
            "units": units,
        }
    return report


def _live_grants(grants: Dict[str, Dict[str, Any]],
                 cutoff: dt.datetime) -> Dict[str, bool]:
    live = {}
    for gid, grant in grants.items():
        effective = _parse(grant["effective_at"])
        revoked = grant.get("revoked_at")
        live[gid] = effective <= cutoff and (
            revoked is None or cutoff < _parse(revoked))
    changed = True
    while changed:
        changed = False
        for gid, grant in grants.items():
            parent = grant.get("parent_grant_id")
            if live.get(gid) and parent is not None \
                    and not live.get(parent, False):
                live[gid] = False
                changed = True
    return live


def _owners(proposal_id: str, unit: int, position_id: str, holder: str,
            grants: Dict[str, Dict[str, Any]], live: Dict[str, bool]
            ) -> List[Tuple[str, List[str]]]:
    """Every live chain reaching ``unit`` for ``proposal_id``."""
    results: List[Tuple[str, List[str]]] = []

    def walk(owner: str, parent: Optional[str], path: List[str]) -> None:
        covering = []
        for gid, grant in grants.items():
            if not live.get(gid):
                continue
            if grant.get("parent_grant_id") != parent:
                continue
            if proposal_id not in grant["proposal_ids"]:
                continue
            if grant["unit_start"] <= unit < grant["unit_end"]:
                covering.append((gid, grant))
        if not covering:
            results.append((owner, path))
            return
        for gid, grant in sorted(covering):
            walk(grant["grantee"], gid, path + [gid])

    walk(holder, None, [])
    # Deduplicate by (voter, path) to keep identical evidence stable.
    unique = sorted({(voter, tuple(path)) for voter, path in results})
    return [(voter, list(path)) for voter, path in unique]


def _resolved_ballot(ballots, position_id, proposal_id, voter, unit, cutoff):
    """What one voter's ballots say about one share.

    Returns ``None`` (no ballot), ``"invalid"`` (out-of-authority ballot with
    no in-authority part), ``("conflict", [ids])`` or ``(choice, [ids])``.
    Authority clipping is checked against the voter's own live grants.
    """
    candidates = [b for b in ballots
                  if b["position_id"] == position_id
                  and b["proposal_id"] == proposal_id
                  and b["voter"] == voter]
    if not candidates:
        return None
    valid_choices = set()
    ids = []
    for ballot in candidates:
        if _parse(ballot["submitted_at"]) > cutoff:
            continue
        if not ballot["unit_start"] <= unit < ballot["unit_end"]:
            continue
        ids.append(ballot["ballot_id"])
        valid_choices.add(ballot["choice"])
    if not valid_choices:
        return None
    if len(valid_choices) > 1:
        return ("conflict", sorted(set(ids)))
    return (sorted(valid_choices)[0], sorted(set(ids)))
