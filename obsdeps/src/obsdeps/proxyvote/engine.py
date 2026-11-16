"""The proxy-vote reconciliation engine.

Given one meeting's parsed roster, proposals, grants and ballots, decide for
each proposal which shares were validly cast for which choice, and emit a
per-segment evidence trail whose non-overlapping units recompute every total.

Semantics, in order:

1. **The roster fixes the share universe.**  Each position is the half-open
   range ``[0, shares)`` from the requested roster version.  Positions are
   never merged by holder: ``position_id`` is the identity.

2. **Authority is judged at the cutoff.**  A grant is *live* when
   ``effective_at <= cutoff < revoked_at`` (the revocation instant itself is
   outside the grant).  ``record_date`` fixes which shares exist; it does not
   gate proxy creation, so a grant signed after the record date but effective
   before the cutoff counts.  A grant whose parent is not live is not live.

3. **Every authority has one chain.**  A root grant must be issued by the
   position holder.  A child grant must be issued by its parent's grantee, lie
   inside the parent range, and list no proposal the parent does not list.
   Cycles, mismatched identities, out-of-parent ranges and illegal times are
   input errors that quarantine the *position*, never the whole meeting.

4. **Ownership is a partition of [0, shares), per proposal.**  Only grants
   listing the proposal convey authority over it.  A grantee owns a unit
   unless a live child grant by that grantee owns it; the holder owns what no
   live grant allocates.

5. **Ambiguity is local.**  Two *different* grantees reached by independent
   live chains over the same units make exactly those units ``contested``.
   Two grants from one authority to the *same* grantee are not ambiguous: the
   shares are owned once, both paths stay in the evidence, and nothing is
   double counted.  Ballots with different choices over the same units are
   likewise contested only on their intersection, and identical repeats count
   once with every source ballot retained.

6. **Ballots are clipped to authority.**  A ballot after the cutoff is void.
   The part of a ballot outside the voter's live authority for that proposal
   is recorded as ``invalid`` and never widens, moves or creates authority.

7. **Sweep, do not enumerate shares.**  Ownership endpoints and ballot
   endpoints form the only cut points; the sweep cost grows with the number of
   input ranges and their endpoints, not with ``shares``.  A billion-share
   position with two grants costs what a hundred-share position with two
   grants costs.

Every position/proposal therefore reports segments that tile ``[0, shares)``
exactly; ``by_choice + abstain + contested == shares`` and the invalid count is
reported separately because those units are not part of the valid tally.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..errors import Problem
from .inputs import Ballot, Grant, ParsedMeeting, Proposal, RosterPosition

PROXYVOTE_ENGINE_VERSION = "1.0.0"

# Children of the roster holder are keyed under ROOT_KEY; every other
# authority is keyed by the id of the grant that transferred shares to it.
ROOT_KEY = ""


# --------------------------------------------------------------------------- #
# Interval helpers
# --------------------------------------------------------------------------- #


def merge_intervals(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[List[int]] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def subtract_intervals(base: Tuple[int, int],
                       cuts: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """``base`` minus a sorted, disjoint union of ``cuts``."""
    start, end = base
    result: List[Tuple[int, int]] = []
    cursor = start
    for cut_start, cut_end in cuts:
        if cut_end <= cursor:
            continue
        if cut_start >= end:
            break
        if cut_start > cursor:
            result.append((cursor, cut_start))
        cursor = max(cursor, cut_end)
        if cursor >= end:
            break
    if cursor < end:
        result.append((cursor, end))
    return result


def intersect_intervals(a: Tuple[int, int],
                        b: Sequence[Tuple[int, int]]
                        ) -> List[Tuple[int, int]]:
    result: List[Tuple[int, int]] = []
    start, end = a
    for left, right in b:
        if right <= start:
            continue
        if left >= end:
            break
        result.append((max(start, left), min(end, right)))
    return result


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #


@dataclass
class GrantState:
    grant: Grant
    live: bool
    basis: str


@dataclass
class OwnedRange:
    """A unit span and every live authority chain that reaches it.

    ``chains`` holds one ``(voter, path)`` per distinct live authority chain.
    More than one chain reaching the same span is an authority overlap and
    makes exactly that span contested; the paths are all kept as evidence.
    """

    unit_start: int
    unit_end: int
    chains: Tuple[Tuple[str, Tuple[str, ...]], ...]

    @property
    def voters(self) -> Tuple[str, ...]:
        return tuple(sorted({voter for voter, _ in self.chains}))

    @property
    def contested(self) -> bool:
        return len(self.chains) > 1


@dataclass
class BallotPiece:
    voter: str
    unit_start: int
    unit_end: int
    choice: str
    ballot_id: str
    source_index: int


@dataclass
class Segment:
    unit_start: int
    unit_end: int
    outcome: str
    voter: str
    path: Tuple[str, ...]
    ballot_ids: List[str] = field(default_factory=list)
    alternatives: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def units(self) -> int:
        return max(0, self.unit_end - self.unit_start)


@dataclass
class PositionPlan:
    position: RosterPosition
    grants: List[Grant] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    grant_states: Dict[str, GrantState] = field(default_factory=dict)
    grant_paths: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    grant_records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    proposal_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    incomplete: bool = False


# --------------------------------------------------------------------------- #
# Meeting driver
# --------------------------------------------------------------------------- #


def reconcile_meeting(parsed: ParsedMeeting) -> Dict[str, Any]:
    positions = sorted(parsed.positions.values(), key=lambda p: p.position_id)
    grants_by_position: Dict[str, List[Grant]] = {}
    for grant in parsed.grants:
        grants_by_position.setdefault(grant.position_id, []).append(grant)
    ballots_by_key: Dict[Tuple[str, str], List[Ballot]] = {}
    for ballot in parsed.ballots:
        ballots_by_key.setdefault(
            (ballot.position_id, ballot.proposal_id), []).append(ballot)

    problems: List[Problem] = list(parsed.problems)
    results: List[Dict[str, Any]] = []
    incomplete = bool(problems)
    if not positions:
        problems.append(Problem(
            "roster-empty",
            "the requested roster version declares no positions",
            "roster", {"record_date": parsed.record_date_text}))
        incomplete = True
    for position in positions:
        plan = _plan_position(
            parsed, position,
            grants_by_position.get(position.position_id, []),
            ballots_by_key,
        )
        incomplete = incomplete or plan.incomplete
        results.append(_position_record(plan))

    for position_id in sorted({g.position_id for g in parsed.grants
                               if g.position_id not in parsed.positions}):
        problems.append(Problem(
            "grant-unknown-position",
            "grant references a position absent from the requested roster",
            f"position:{position_id}", {"position_id": position_id}))
        incomplete = True
    for position_id in sorted({b.position_id for b in parsed.ballots
                               if b.position_id not in parsed.positions}):
        problems.append(Problem(
            "ballot-unknown-position",
            "ballot references a position absent from the requested roster",
            f"position:{position_id}", {"position_id": position_id}))
        incomplete = True

    proposals = [
        _proposal_record(proposal, results)
        for proposal in sorted(parsed.proposals.values(),
                               key=lambda p: p.proposal_id)
    ]

    return {
        "engine_version": PROXYVOTE_ENGINE_VERSION,
        "meeting_id": parsed.meeting_id,
        "roster_version": {
            "record_date": parsed.record_date_text,
            "records_rejected": parsed.skipped_roster_rows,
            "sha256": parsed.sources.get("roster", {}).get("sha256", ""),
        },
        "record_date": parsed.record_date_text,
        "cutoff_at": parsed.cutoff_text,
        "cutoff_utc": format_utc(parsed.cutoff_at),
        "complete": not incomplete,
        "summary": {
            "positions": len(positions),
            "incomplete_positions": sum(1 for r in results
                                        if r["status"] == "incomplete"),
            "proposals": len(parsed.proposals),
            "grants": len(parsed.grants),
            "ballots": len(parsed.ballots),
        },
        "positions": results,
        "proposals": proposals,
        "problems": _problem_dicts(problems),
        "sources": dict(sorted(parsed.sources.items())),
    }


@lru_cache(maxsize=262144)
def format_utc(moment: dt.datetime) -> str:
    """RFC 3339 UTC text for an aware datetime.

    Cached on the (immutable, hashable) datetime *value*, never on its
    identity, so a recycled object address can never return the wrong text.
    """
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Position: validate the grant graph
# --------------------------------------------------------------------------- #


def _plan_position(parsed: ParsedMeeting, position: RosterPosition,
                   grants: List[Grant],
                   ballots_by_key: Dict[Tuple[str, str], List[Ballot]]
                   ) -> PositionPlan:
    plan = PositionPlan(position=position, grants=list(grants))
    position_id = position.position_id

    local = [p for p in parsed.problems
             if p.details.get("position_id") == position_id]
    if local:
        plan.problems.extend(local)
        return _quarantined(plan)

    grants = sorted(grants, key=lambda g: (g.unit_start, g.unit_end, g.index))
    by_id: Dict[str, Grant] = {}
    for grant in grants:
        if grant.grant_id in by_id:
            plan.problems.append(Problem(
                "grant-duplicate-id",
                f"grant_id {grant.grant_id!r} appears more than once for "
                f"this position", grant.locator,
                {"position_id": position_id, "grant_id": grant.grant_id}))
        else:
            by_id[grant.grant_id] = grant

    declared_proposals = set(parsed.proposals)
    for grant in grants:
        if grant.parent_grant_id is not None \
                and grant.parent_grant_id not in by_id:
            _bad(plan, grant, "grant-parent-unknown",
                 f"parent_grant_id {grant.parent_grant_id!r} is not declared "
                 f"for this position",
                 {"parent_grant_id": grant.parent_grant_id})
        if grant.parent_grant_id is None:
            if grant.grantor != position.holder_id:
                _bad(plan, grant, "grant-grantor-mismatch",
                     "a root grant must be issued by the position holder",
                     {"expected": position.holder_id,
                      "actual": grant.grantor})
        else:
            parent = by_id.get(grant.parent_grant_id)
            if parent is not None:
                if grant.grantor != parent.grantee:
                    _bad(plan, grant, "grant-grantor-mismatch",
                         "a child grant must be issued by its parent's grantee",
                         {"parent_grant_id": parent.grant_id,
                          "expected": parent.grantee,
                          "actual": grant.grantor})
                if grant.unit_start < parent.unit_start \
                        or grant.unit_end > parent.unit_end:
                    _bad(plan, grant, "grant-exceeds-parent",
                         "child share range is not inside the parent range",
                         {"parent_grant_id": parent.grant_id,
                          "parent_range": [parent.unit_start, parent.unit_end],
                          "child_range": [grant.unit_start, grant.unit_end]})
                extra = sorted(set(grant.proposal_ids)
                               - set(parent.proposal_ids))
                if extra:
                    _bad(plan, grant, "grant-proposals-exceed-parent",
                         "child proposal set is not a subset of the parent's",
                         {"parent_grant_id": parent.grant_id,
                          "extra_proposals": extra})
        unknown = sorted(set(grant.proposal_ids) - declared_proposals)
        if unknown:
            _bad(plan, grant, "grant-unknown-proposal",
                 "grant lists proposal ids this meeting does not declare",
                 {"proposal_ids": unknown})
        if grant.unit_end > position.shares:
            _bad(plan, grant, "grant-exceeds-position",
                 "grant range is outside the position's [0, shares)",
                 {"shares": position.shares,
                  "unit_range": [grant.unit_start, grant.unit_end]})

    for grant, cycle_at in _cycle_members(by_id):
        _bad(plan, grant, "grant-cycle",
             "parent_grant_id chain revisits a grant (cycle)",
             {"cycle_at": cycle_at})

    if plan.problems:
        return _quarantined(plan)

    # --- liveness at the cutoff ------------------------------------------ #
    for grant in grants:
        plan.grant_states[grant.grant_id] = GrantState(
            grant=grant, live=_grant_live(grant, parsed.cutoff_at),
            basis=_liveness_basis(grant, parsed.cutoff_at))
    for grant_id in _topological_order(by_id):
        grant = by_id[grant_id]
        if grant.parent_grant_id is None:
            continue
        state = plan.grant_states[grant_id]
        parent_state = plan.grant_states[grant.parent_grant_id]
        if state.live and not parent_state.live:
            state.live = False
            state.basis = "parent-not-live"

    # Structural chain evidence, independent of which proposal a grant names.
    for grant_id in by_id:
        chain: List[str] = []
        cursor: Optional[str] = grant_id
        while cursor is not None:
            chain.append(cursor)
            cursor = by_id[cursor].parent_grant_id
        plan.grant_paths[grant_id] = tuple(reversed(chain))

    # --- per-proposal ownership sweep ------------------------------------ #
    for proposal in sorted(parsed.proposals.values(),
                           key=lambda p: p.proposal_id):
        relevant = [g for g in grants
                    if proposal.proposal_id in g.proposal_ids]
        owned, overlaps = _ownership(plan, proposal, relevant)
        _report_overlaps(plan, proposal, overlaps)
        ballots = ballots_by_key.get((position_id, proposal.proposal_id), [])
        plan.proposal_results[proposal.proposal_id] = _adjudicate(
            parsed, plan, proposal, owned, ballots)
    return plan


def _bad(plan: PositionPlan, grant: Grant, code: str, message: str,
         details: Dict[str, Any]) -> None:
    plan.problems.append(Problem(
        code, message, grant.locator,
        {"position_id": plan.position.position_id,
         "grant_id": grant.grant_id, **details}))


def _quarantined(plan: PositionPlan) -> PositionPlan:
    plan.incomplete = True
    return plan


def _cycle_members(by_id: Dict[str, Grant]) -> List[Tuple[Grant, str]]:
    """Find every grant on a parent cycle in O(n), without recursion.

    An explicit colour-marked depth-first walk: white = unvisited, grey = on
    the current path, black = finished.  Seeing a grey parent means the path
    from that parent to the current node is a cycle.  Nothing recurses, so a
    legal chain thousands of levels deep is fine, and each node is entered
    once.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {}
    found: List[Tuple[Grant, str]] = []
    for start in sorted(by_id):
        if colour.get(start, WHITE) != WHITE:
            continue
        stack: List[str] = [start]
        path: List[str] = []
        on_path: Dict[str, int] = {}
        while stack:
            node = stack[-1]
            if colour.get(node, WHITE) == WHITE:
                colour[node] = GREY
                on_path[node] = len(path)
                path.append(node)
            parent = by_id[node].parent_grant_id
            if parent is None or parent not in by_id:
                stack.pop()
                colour[node] = BLACK
                on_path.pop(node, None)
                if path and path[-1] == node:
                    path.pop()
                continue
            parent_colour = colour.get(parent, WHITE)
            if parent_colour == GREY:
                for grant_id in path[on_path[parent]:]:
                    found.append((by_id[grant_id], parent))
                stack.pop()
                colour[node] = BLACK
                on_path.pop(node, None)
                if path and path[-1] == node:
                    path.pop()
                continue
            if parent_colour == BLACK:
                stack.pop()
                colour[node] = BLACK
                on_path.pop(node, None)
                if path and path[-1] == node:
                    path.pop()
                continue
            stack.append(parent)
    return sorted({(grant.grant_id, cycle_at): (grant, cycle_at)
                   for grant, cycle_at in found}.values(),
                  key=lambda item: item[0].grant_id)


def _topological_order(by_id: Dict[str, Grant]) -> List[str]:
    """Parents before children, iterative (no recursion depth limit).

    Only called on an acyclic graph; the ``local`` set still guards against an
    unexpected loop so this can never hang.  A node's base depth is the depth
    of the parent it stopped at, which is already known whenever the stop was
    a previously resolved node or an absent parent.
    """
    depth: Dict[str, int] = {}
    for start in sorted(by_id):
        chain: List[str] = []
        local: set = set()
        node: Optional[str] = start
        while node is not None and node in by_id and node not in depth \
                and node not in local:
            chain.append(node)
            local.add(node)
            node = by_id[node].parent_grant_id
        if node is not None and node in depth:
            base = depth[node]
        else:
            base = -1
        for grant_id in reversed(chain):
            base += 1
            depth[grant_id] = base
    return sorted(by_id, key=lambda gid: (depth[gid], gid))


def _grant_live(grant: Grant, cutoff: dt.datetime) -> bool:
    if grant.effective_at > cutoff:
        return False
    if grant.revoked_at is not None and cutoff >= grant.revoked_at:
        return False
    return True


def _liveness_basis(grant: Grant, cutoff: dt.datetime) -> str:
    if grant.effective_at > cutoff:
        return "not-yet-effective"
    if grant.revoked_at is not None and cutoff >= grant.revoked_at:
        return "revoked"
    return "live"


# --------------------------------------------------------------------------- #
# Ownership: partition [0, shares) for one proposal, by interval sweep
# --------------------------------------------------------------------------- #


@dataclass
class _Overlap:
    unit_start: int
    unit_end: int
    voters: Tuple[str, ...]
    paths: Tuple[Tuple[str, ...], ...]


def _ownership(plan: PositionPlan, proposal: Proposal, grants: List[Grant]
               ) -> Tuple[List[OwnedRange], List[_Overlap]]:
    """Assign every unit of ``[0, shares)`` to its live authority chain(s).

    Each grant endpoint turns up in exactly one event; the sweep touches only
    the events at the current boundary.  Work is therefore proportional to
    the number of input ranges and their endpoints, independent of which
    positions the boundaries sit at and completely independent of ``shares``.
    """
    position = plan.position
    live = [g for g in grants if plan.grant_states[g.grant_id].live]

    children: Dict[str, List[Grant]] = {}
    for grant in live:
        key = grant.parent_grant_id if grant.parent_grant_id is not None \
            else ROOT_KEY
        children.setdefault(key, []).append(grant)

    boundaries = {0, position.shares}
    starts: Dict[int, List[Grant]] = {}
    ends: Dict[int, List[Grant]] = {}
    for grant in live:
        boundaries.add(grant.unit_start)
        boundaries.add(grant.unit_end)
        starts.setdefault(grant.unit_start, []).append(grant)
        ends.setdefault(grant.unit_end, []).append(grant)
    for point in starts:
        starts[point].sort(key=lambda g: (g.unit_end, g.grant_id))
    for point in ends:
        ends[point].sort(key=lambda g: (g.unit_start, g.grant_id))
    ordered = sorted(boundaries)

    active: Dict[str, List[Grant]] = {}

    def adjust(point: int) -> None:
        # A node is keyed by parent grant id (or ROOT_KEY); grants of one node
        # are kept ordered by (unit_end, grant_id).
        closing = ends.get(point, ())
        if closing:
            closing_nodes = {_node_of(grant) for grant in closing}
            for node in closing_nodes:
                removed = {id(g) for g in closing
                           if _node_of(g) == node}
                bucket = active.get(node)
                if bucket:
                    active[node] = [g for g in bucket if id(g) not in removed]
        opening = starts.get(point, ())
        for grant in opening:
            bucket = active.setdefault(_node_of(grant), [])
            bucket.append(grant)
            bucket.sort(key=lambda g: (g.unit_end, g.grant_id))

    owned: List[OwnedRange] = []
    overlaps: List[_Overlap] = []
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        adjust(left)
        chains = _chains_at(plan, active, left)
        record = OwnedRange(left, right, chains)
        if record.contested:
            overlaps.append(_Overlap(
                left, right, record.voters,
                tuple(sorted({path for _, path in record.chains}))))
        owned.append(record)
    return _merge_owned(owned), _merge_overlaps(overlaps)


def _node_of(grant: Grant) -> str:
    return grant.parent_grant_id if grant.parent_grant_id is not None \
        else ROOT_KEY


def _merge_owned(owned: List[OwnedRange]) -> List[OwnedRange]:
    merged: List[OwnedRange] = []
    for item in owned:
        if merged and merged[-1].chains == item.chains \
                and merged[-1].unit_end == item.unit_start:
            merged[-1].unit_end = item.unit_end
        else:
            merged.append(item)
    return merged


def _merge_overlaps(overlaps: List[_Overlap]) -> List[_Overlap]:
    merged: List[_Overlap] = []
    for item in overlaps:
        if merged and merged[-1].voters == item.voters \
                and merged[-1].paths == item.paths \
                and merged[-1].unit_end == item.unit_start:
            merged[-1].unit_end = item.unit_end
        else:
            merged.append(item)
    return merged


def _chains_at(plan: PositionPlan, active: Dict[str, List[Grant]], unit: int
               ) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """Every live authority chain reaching ``unit`` (usually exactly one).

    Iterative, so a legal chain thousands of levels deep cannot hit Python's
    recursion limit.  Because each grant has exactly one parent, the walk from
    the holder is a tree; a unit may still be reached by several branches.
    """
    holder = plan.position.holder_id
    results: List[Tuple[str, Tuple[str, ...]]] = []
    stack: List[Tuple[str, Tuple[str, ...], str]] = [(ROOT_KEY, (), holder)]
    while stack:
        key, path, owner = stack.pop()
        covering = [g for g in active.get(key, ())
                    if g.unit_start <= unit < g.unit_end]
        if not covering:
            results.append((owner, path))
            continue
        for grant in covering:
            stack.append((grant.grant_id, path + (grant.grant_id,),
                          grant.grantee))
    if len(results) == 1:
        return tuple(results)
    return tuple(sorted(set(results)))


def _report_overlaps(plan: PositionPlan, proposal: Proposal,
                     overlaps: List[_Overlap]) -> None:
    if not overlaps:
        return
    plan.problems.append(Problem(
        "authority-overlap",
        "independent live grant chains reach the same share units for this "
        "proposal; only those units are contested",
        plan.position.locator,
        {"position_id": plan.position.position_id,
         "proposal_id": proposal.proposal_id,
         "voters": sorted({v for item in overlaps for v in item.voters}),
         "ranges": [[item.unit_start, item.unit_end] for item in overlaps]}))


# --------------------------------------------------------------------------- #
# Adjudication for one position + proposal
# --------------------------------------------------------------------------- #


@dataclass
class _VoterSpan:
    unit_start: int
    unit_end: int
    outcome: str          # choice or "contested"
    ballot_ids: List[str]


def _adjudicate(parsed: ParsedMeeting, plan: PositionPlan, proposal: Proposal,
                owned: List[OwnedRange], relevant: List[Ballot]
                ) -> Dict[str, Any]:
    position = plan.position
    cutoff = parsed.cutoff_at

    authority: Dict[str, List[Tuple[int, int]]] = {}
    for item in owned:
        for voter, _path in item.chains:
            authority.setdefault(voter, []).append(
                (item.unit_start, item.unit_end))
    for voter in authority:
        authority[voter] = merge_intervals(authority[voter])

    pieces: Dict[str, List[BallotPiece]] = {}
    invalid_ballots: List[Dict[str, Any]] = []
    invalid_units = 0
    for ballot in sorted(relevant, key=lambda b: (b.submitted_at, b.index)):
        if ballot.submitted_at > cutoff:
            record = _invalid_record(
                ballot, "submitted-after-cutoff",
                "ballot was submitted after the voting cutoff", [])
            record["units"] = ballot.unit_end - ballot.unit_start
            invalid_units += record["units"]
            invalid_ballots.append(record)
            plan.problems.append(Problem(
                "ballot-after-cutoff",
                "ballot submitted after the voting cutoff was ignored",
                ballot.locator,
                {"position_id": position.position_id,
                 "proposal_id": proposal.proposal_id,
                 "ballot_id": ballot.ballot_id}))
            continue
        clipped = intersect_intervals((ballot.unit_start, ballot.unit_end),
                                      authority.get(ballot.voter, []))
        outside = _outside_parts(ballot, clipped)
        if outside:
            record = _invalid_record(
                ballot, "outside-authority",
                "part of the ballot range is outside the voter's live "
                "authority for this proposal at the cutoff", outside)
            invalid_ballots.append(record)
            invalid_units += record["units"]
            plan.problems.append(Problem(
                "ballot-outside-authority",
                "part of a ballot was rejected because it lies outside the "
                "voter's live authority",
                ballot.locator,
                {"position_id": position.position_id,
                 "proposal_id": proposal.proposal_id,
                 "ballot_id": ballot.ballot_id,
                 "accepted_ranges": clipped,
                 "rejected_units": record["units"],
                 "rejected_ranges": outside}))
        for start, end in clipped:
            pieces.setdefault(ballot.voter, []).append(BallotPiece(
                voter=ballot.voter, unit_start=start, unit_end=end,
                choice=ballot.choice, ballot_id=ballot.ballot_id,
                source_index=ballot.index))

    voter_spans = {voter: _voter_spans(items)
                   for voter, items in pieces.items()}
    segments = _compose(position, owned, voter_spans)
    _assert_tiling(position, segments)

    by_choice: Dict[str, int] = {option: 0 for option in proposal.options}
    abstain = contested = 0
    for segment in segments:
        if segment.outcome == "contested":
            contested += segment.units
        elif segment.outcome == "abstain":
            abstain += segment.units
        else:
            by_choice[segment.outcome] = by_choice.get(segment.outcome, 0) \
                + segment.units
    return {
        "proposal_id": proposal.proposal_id,
        "by_choice": dict(sorted(by_choice.items())),
        "abstain": abstain,
        "contested": contested,
        "invalid": invalid_units,
        "invalid_ballot_count": len({record["ballot_id"]
                                     for record in invalid_ballots}),
        "invalid_ballots": sorted(invalid_ballots,
                                  key=lambda r: (r["ballot_id"], r["code"])),
        "total": sum(by_choice.values()) + abstain + contested,
        "segments": segments,
    }


def _outside_parts(ballot: Ballot, clipped: List[Tuple[int, int]]
                   ) -> List[List[int]]:
    covered = merge_intervals(clipped)
    return [[start, end] for start, end in
            subtract_intervals((ballot.unit_start, ballot.unit_end), covered)]


def _voter_spans(pieces: List[BallotPiece]) -> List[_VoterSpan]:
    """Fold one voter's clipped ballot pieces into choice/contested spans.

    Identical repeat submissions covering the same units produce one choice
    and keep every source ballot id.  Two different choices on the same units
    produce ``contested`` for exactly that intersection.
    """
    if not pieces:
        return []
    boundaries = sorted({p.unit_start for p in pieces}
                        | {p.unit_end for p in pieces})
    active: Dict[str, List[BallotPiece]] = {}
    starts: Dict[int, List[BallotPiece]] = {}
    ends: Dict[int, List[BallotPiece]] = {}
    for piece in pieces:
        starts.setdefault(piece.unit_start, []).append(piece)
        ends.setdefault(piece.unit_end, []).append(piece)

    spans: List[_VoterSpan] = []
    for left, right in zip(boundaries, boundaries[1:]):
        for piece in starts.get(left, ()):
            active.setdefault(piece.ballot_id, []).append(piece)
        for piece in ends.get(left, ()):
            bucket = active.get(piece.ballot_id)
            if bucket is not None:
                active[piece.ballot_id] = [p for p in bucket if p is not piece]
                if not active[piece.ballot_id]:
                    active.pop(piece.ballot_id, None)
        covering = [piece for bucket in active.values() for piece in bucket]
        if not covering:
            continue
        choices = sorted({piece.choice for piece in covering})
        outcome = choices[0] if len(choices) == 1 else "contested"
        ballot_ids = sorted({piece.ballot_id for piece in covering})
        spans.append(_VoterSpan(left, right, outcome, ballot_ids))
    return spans


def _compose(position: RosterPosition, owned: List[OwnedRange],
             voter_spans: Dict[str, List[_VoterSpan]]) -> List[Segment]:
    """Sweep every ownership and ballot boundary once and emit segments.

    Cuts exist only where ownership chains or ballot outcomes change, so the
    number of segments is bounded by the number of input ranges -- never by
    ``shares``.
    """
    boundaries = {0, position.shares}
    for item in owned:
        boundaries.add(item.unit_start)
        boundaries.add(item.unit_end)
    for spans in voter_spans.values():
        for span in spans:
            boundaries.add(span.unit_start)
            boundaries.add(span.unit_end)
    ordered = sorted(boundaries)

    spans_by_voter: Dict[str, List[_VoterSpan]] = {
        voter: sorted(items, key=lambda s: (s.unit_start, s.unit_end))
        for voter, items in voter_spans.items()
    }

    def span_at(voter: str, unit: int) -> Optional[_VoterSpan]:
        for span in spans_by_voter.get(voter, ()):
            if span.unit_start <= unit < span.unit_end:
                return span
        return None

    segments: List[Segment] = []
    owner_cursor = 0
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        while owner_cursor + 1 < len(owned) \
                and owned[owner_cursor].unit_end <= left:
            owner_cursor += 1
        item = owned[owner_cursor]
        if not item.unit_start <= left < item.unit_end:
            raise AssertionError(
                f"ownership does not tile share {left} of "
                f"{position.position_id}")
        if item.contested:
            ballot_ids: List[str] = []
            alternatives: List[Dict[str, Any]] = []
            for voter, path in item.chains:
                alternatives.append({"voter": voter, "path": list(path)})
                span = span_at(voter, left)
                if span is not None:
                    ballot_ids.extend(span.ballot_ids)
            _append(segments, Segment(left, right, "contested", "", (),
                                      sorted(set(ballot_ids)), alternatives))
            continue
        voter, path = item.chains[0]
        span = span_at(voter, left)
        if span is None:
            _append(segments, Segment(left, right, "abstain", voter, path, []))
        elif span.outcome == "contested":
            _append(segments, Segment(
                left, right, "contested", voter, path,
                list(span.ballot_ids),
                [{"voter": voter, "path": list(path)}]))
        else:
            _append(segments, Segment(left, right, span.outcome, voter, path,
                                      list(span.ballot_ids)))
    return segments


def _append(segments: List[Segment], segment: Segment) -> None:
    if segment.unit_end <= segment.unit_start:
        return
    if segments and segments[-1].unit_end == segment.unit_start \
            and segments[-1].outcome == segment.outcome \
            and segments[-1].voter == segment.voter \
            and segments[-1].path == segment.path \
            and segments[-1].alternatives == segment.alternatives:
        segments[-1].unit_end = segment.unit_end
        segments[-1].ballot_ids = sorted(
            set(segments[-1].ballot_ids) | set(segment.ballot_ids))
    else:
        segments.append(segment)


def _assert_tiling(position: RosterPosition, segments: List[Segment]) -> None:
    cursor = 0
    for segment in sorted(segments, key=lambda s: s.unit_start):
        if segment.unit_start != cursor:
            raise AssertionError(
                f"internal segment gap/overlap for {position.position_id}: "
                f"expected {cursor}, got {segment.unit_start}")
        cursor = segment.unit_end
    if cursor != position.shares:
        raise AssertionError(
            f"internal segment tiling for {position.position_id}: "
            f"covered {cursor} of {position.shares}")


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #


def _position_record(plan: PositionPlan) -> Dict[str, Any]:
    position = plan.position
    proposals: List[Dict[str, Any]] = []
    for proposal_id in sorted(plan.proposal_results):
        result = plan.proposal_results[proposal_id]
        proposals.append({
            "proposal_id": proposal_id,
            "by_choice": result["by_choice"],
            "abstain": result["abstain"],
            "contested": result["contested"],
            "invalid": result["invalid"],
            "invalid_ballot_count": result["invalid_ballot_count"],
            "total": result["total"],
            "invalid_ballots": result["invalid_ballots"],
            "segments": [_segment_record(position, proposal_id, s)
                         for s in result["segments"]],
        })
    grants = [_grant_record(plan, grant) for grant in plan.grants]
    return {
        "status": "incomplete" if plan.incomplete else "reconciled",
        "position_id": position.position_id,
        "holder_id": position.holder_id,
        "security_class": position.security_class,
        "shares": position.shares,
        "proposals": proposals,
        "grants": grants,
        "problems": _problem_dicts(plan.problems),
    }


def _grant_record(plan: PositionPlan, grant: Grant) -> Dict[str, Any]:
    cached = plan.grant_records.get(grant.grant_id)
    if cached is not None:
        return cached
    state = plan.grant_states.get(grant.grant_id)
    record = {
        "grant_id": grant.grant_id,
        "grantor": grant.grantor,
        "grantee": grant.grantee,
        "unit_start": grant.unit_start,
        "unit_end": grant.unit_end,
        "proposal_ids": list(grant.proposal_ids),
        "effective_at": format_utc(grant.effective_at),
        "revoked_at": format_utc(grant.revoked_at)
        if grant.revoked_at is not None else None,
        "parent_grant_id": grant.parent_grant_id,
        "live_at_cutoff": state.live if state is not None else None,
        "live_basis": state.basis if state is not None else "unvalidated",
        "path": list(plan.grant_paths.get(grant.grant_id, ())),
    }
    plan.grant_records[grant.grant_id] = record
    return record


def _segment_record(position: RosterPosition, proposal_id: str,
                    segment: Segment) -> Dict[str, Any]:
    return {
        "position_id": position.position_id,
        "proposal_id": proposal_id,
        "unit_start": segment.unit_start,
        "unit_end": segment.unit_end,
        "units": segment.units,
        "outcome": segment.outcome,
        "voter": segment.voter,
        "authority_path": list(segment.path),
        "alternatives": segment.alternatives,
        "ballot_ids": sorted(segment.ballot_ids),
    }


def _proposal_record(proposal: Proposal,
                     positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_choice: Dict[str, int] = {option: 0 for option in proposal.options}
    abstain = contested = invalid = invalid_ballots = 0
    invalid_records: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    for position in positions:
        for entry in position["proposals"]:
            if entry["proposal_id"] != proposal.proposal_id:
                continue
            for option, count in entry["by_choice"].items():
                by_choice[option] = by_choice.get(option, 0) + count
            abstain += entry["abstain"]
            contested += entry["contested"]
            invalid += entry["invalid"]
            invalid_ballots += entry["invalid_ballot_count"]
            invalid_records.extend(
                dict(record, position_id=position["position_id"])
                for record in entry["invalid_ballots"])
            segments.extend(entry["segments"])
    segments.sort(key=lambda s: (s["position_id"], s["unit_start"],
                                 s["unit_end"]))
    invalid_records.sort(key=lambda r: (r["position_id"], r["ballot_id"],
                                        r["code"]))
    return {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "options": list(proposal.options),
        "by_choice": dict(sorted(by_choice.items())),
        "abstain": abstain,
        "contested": contested,
        "invalid": invalid,
        "invalid_ballot_count": invalid_ballots,
        "invalid_ballots": invalid_records,
        "total": sum(by_choice.values()) + abstain + contested,
        "segments": segments,
    }


def _invalid_record(ballot: Ballot, code: str, message: str,
                    outside: List[List[int]]) -> Dict[str, Any]:
    return {
        "ballot_id": ballot.ballot_id,
        "voter": ballot.voter,
        "choice": ballot.choice,
        "code": code,
        "message": message,
        "submitted_at": format_utc(ballot.submitted_at),
        "range": [ballot.unit_start, ballot.unit_end],
        "outside": outside,
        "units": sum(max(0, end - start) for start, end in outside),
    }


def _problem_dicts(problems: Iterable[Problem]) -> List[Dict[str, Any]]:
    unique: List[Dict[str, Any]] = []
    seen = set()
    for problem in problems:
        payload = problem.to_dict()
        key = (payload["code"], payload["locator"], payload["message"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    unique.sort(key=lambda p: (p["code"], p["locator"], p["message"]))
    return unique
