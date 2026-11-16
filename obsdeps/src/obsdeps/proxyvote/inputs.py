"""Parsing and structural validation for one shareholder-meeting ballot run.

Four JSONL documents, all read-only, all belonging to one ``meeting_id``:

``roster``   -- one position per line::

    {"meeting_id": "M", "position_id": "P", "holder_id": "H",
     "security_class": "COMMON", "record_date": "2026-03-01",
     "shares": 100}

``proposals`` -- one proposal per line::

    {"meeting_id": "M", "proposal_id": "Q", "title": "...",
     "options": ["X", "Y", "ABSTAIN"]}

``grants``   -- one delegation per line::

    {"meeting_id": "M", "grant_id": "G1", "position_id": "P",
     "grantor": "H", "grantee": "A", "unit_start": 20, "unit_end": 80,
     "proposal_ids": ["Q"], "effective_at": "2026-03-02T00:00:00Z",
     "revoked_at": null, "parent_grant_id": null}

``ballots``  -- one ballot per line::

    {"meeting_id": "M", "ballot_id": "B1", "position_id": "P",
     "unit_start": 20, "unit_end": 80, "voter": "A", "proposal_id": "Q",
     "choice": "X", "submitted_at": "2026-03-03T00:00:00Z"}

Every share range is **half-open** ``[unit_start, unit_end)`` inside the
position's ``[0, shares)``.  Timestamps are RFC 3339 with an explicit offset
and are normalised to UTC.

The parser never merges positions: ``position_id`` is the identity, a holder
may own several positions.  Row problems are collected (each carrying the
``position_id`` it touches when known) so the engine can quarantine exactly
that position; only a document that cannot be read as JSONL at all raises
:class:`~obsdeps.proxyvote.errors.ProxyVoteInputError`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..billing.timeutil import parse_utc
from ..errors import Problem
from .errors import ProxyVoteInputError

# Identity fields of the *request*; a row may not redefine them.
DECLARATION_FIELDS = ("meeting_id", "record_date", "security_class",
                      "position_id", "holder_id", "shares")
GRANT_FIELDS = ("meeting_id", "grant_id", "position_id", "grantor", "grantee",
                "unit_start", "unit_end", "proposal_ids", "effective_at",
                "revoked_at", "parent_grant_id")
BALLOT_FIELDS = ("meeting_id", "ballot_id", "position_id", "unit_start",
                 "unit_end", "voter", "proposal_id", "choice", "submitted_at")
PROPOSAL_FIELDS = ("meeting_id", "proposal_id", "options")

DOCUMENT_NAMES = ("roster", "proposals", "grants", "ballots")


@dataclass(frozen=True)
class RosterPosition:
    position_id: str
    holder_id: str
    security_class: str
    shares: int
    locator: str


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    title: str
    options: Tuple[str, ...]
    locator: str


@dataclass(frozen=True)
class Grant:
    grant_id: str
    position_id: str
    grantor: str
    grantee: str
    unit_start: int
    unit_end: int
    proposal_ids: Tuple[str, ...]
    effective_at: dt.datetime
    revoked_at: Optional[dt.datetime]
    parent_grant_id: Optional[str]
    index: int
    locator: str


@dataclass(frozen=True)
class Ballot:
    ballot_id: str
    position_id: str
    unit_start: int
    unit_end: int
    voter: str
    proposal_id: str
    choice: str
    submitted_at: dt.datetime
    index: int
    locator: str


@dataclass
class ParsedMeeting:
    meeting_id: str
    record_date: dt.datetime
    record_date_text: str
    cutoff_at: dt.datetime
    cutoff_text: str
    positions: Dict[str, RosterPosition] = field(default_factory=dict)
    proposals: Dict[str, Proposal] = field(default_factory=dict)
    grants: List[Grant] = field(default_factory=list)
    ballots: List[Ballot] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    skipped_roster_rows: int = 0


# --------------------------------------------------------------------------- #
# File loading
# --------------------------------------------------------------------------- #


def read_jsonl(path: str) -> Tuple[List[Tuple[int, Any]], bytes]:
    """Read a JSONL file into ``(line_number, row)`` pairs plus the raw bytes.

    Blank lines are ignored for parsing but included in the digest.  Malformed
    JSON is a whole-document failure; a syntactically valid but non-object row
    is a row problem reported by the caller with its line number.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ProxyVoteInputError(f"cannot read input file: {exc}",
                                  source=path) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProxyVoteInputError(
            f"input file is not valid UTF-8: {exc}", source=path) from exc
    rows: List[Tuple[int, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append((number, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ProxyVoteInputError(
                f"invalid JSON on line {number} column {exc.colno}: {exc.msg}",
                source=path) from exc
    return rows, raw


def document_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _as_rows(value: Any, name: str) -> Tuple[List[Tuple[int, Any]], bytes]:
    """Accept a file path, a JSONL string, or an already-parsed row list.

    In-memory rows get a synthetic, stable digest so the same documents key
    the same stored result as their serialized file form would.
    """
    if isinstance(value, str) and "\n" not in value and os.path.isfile(value):
        return read_jsonl(value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        rows: List[Tuple[int, Any]] = []
        for number, line in enumerate(value.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append((number, json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ProxyVoteInputError(
                    f"invalid JSON on line {number} column {exc.colno}: "
                    f"{exc.msg}", source=f"<{name}>") from exc
        return rows, raw
    if isinstance(value, (list, tuple)):
        rows = [(index, row) for index, row in enumerate(value, start=1)]
        raw = "".join(json.dumps(row) + "\n" for row in value).encode("utf-8")
        return rows, raw
    raise ProxyVoteInputError(
        f"{name} must be a JSONL path, a JSONL string or a row list",
        source=f"<{name}>")


def load_documents(*, meeting_id: str, record_date: str, cutoff_at: str,
                   roster: str, proposals: str, grants: str,
                   ballots: str) -> ParsedMeeting:
    """Parse the four files for the meeting explicitly selected by the request.

    ``meeting_id``, ``record_date`` and ``cutoff_at`` come from the *request*;
    they are never inferred from, nor replaced by, later database state.
    """
    if not isinstance(meeting_id, str) or not meeting_id.strip():
        raise ProxyVoteInputError("meeting_id must be a non-empty string",
                                  source="<request>")
    meeting_id = meeting_id.strip()
    record_moment = _parse_request_date(record_date)
    cutoff_moment = _parse_request_cutoff(cutoff_at)
    if cutoff_moment < record_moment:
        raise ProxyVoteInputError(
            "voting cutoff must not precede the record date", source="<request>")

    parsed = ParsedMeeting(
        meeting_id=meeting_id, record_date=record_moment,
        record_date_text=record_date.strip(), cutoff_at=cutoff_moment,
        cutoff_text=cutoff_at.strip(),
    )
    documents = {"roster": roster, "proposals": proposals,
                 "grants": grants, "ballots": ballots}
    parsers = {"roster": _parse_roster_row, "proposals": _parse_proposal_row,
               "grants": _parse_grant_row, "ballots": _parse_ballot_row}
    for name in DOCUMENT_NAMES:
        rows, raw = _as_rows(documents[name], name)
        source_path = (os.path.abspath(documents[name])
                       if isinstance(documents[name], str)
                       and os.path.isfile(documents[name])
                       else f"<{name}>")
        parsed.sources[name] = {
            "path": source_path,
            "sha256": document_digest(raw),
            "bytes": len(raw),
            "records": len(rows),
        }
        for number, row in rows:
            locator = f"{name}[{number}]"
            if not isinstance(row, dict):
                _add(parsed, "row-not-object", "JSONL row must be an object",
                     locator, None)
                continue
            before = len(parsed.positions) if name == "roster" else 0
            if not _meeting_matches(parsed, row, locator):
                parsed.skipped_roster_rows += (name == "roster")
                continue
            parsers[name](parsed, row, locator)
            if name == "roster" and len(parsed.positions) == before:
                parsed.skipped_roster_rows += 1
    return parsed


def _parse_request_date(text: Any) -> dt.datetime:
    if not isinstance(text, str):
        raise ProxyVoteInputError("record_date must be YYYY-MM-DD",
                                  source="<request>")
    try:
        day = dt.date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ProxyVoteInputError(f"record_date is not YYYY-MM-DD: {exc}",
                                  source="<request>") from exc
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


def _parse_request_cutoff(text: Any) -> dt.datetime:
    if not isinstance(text, str):
        raise ProxyVoteInputError(
            "cutoff_at must be an RFC 3339 timestamp with an offset",
            source="<request>")
    try:
        return parse_utc(text)
    except ValueError as exc:
        raise ProxyVoteInputError(f"cutoff_at is not RFC 3339: {exc}",
                                  source="<request>") from exc


# --------------------------------------------------------------------------- #
# Shared field helpers
# --------------------------------------------------------------------------- #


def _add(parsed: ParsedMeeting, code: str, message: str, locator: str,
         position_id: Optional[str],
         details: Optional[Dict[str, Any]] = None) -> None:
    payload = dict(details or {})
    if position_id is not None:
        payload.setdefault("position_id", position_id)
    parsed.problems.append(Problem(code, message, locator, payload))


def _meeting_matches(parsed: ParsedMeeting, row: Dict[str, Any],
                     locator: str) -> bool:
    value = row.get("meeting_id")
    if value is None:
        _add(parsed, "row-missing-field",
             "row is missing required field(s): meeting_id", locator,
             _position_of(row), {"fields": ["meeting_id"]})
        return False
    if not isinstance(value, str) or value.strip() != parsed.meeting_id:
        _add(parsed, "row-unknown-meeting",
             f"row belongs to meeting {value!r}, not the requested "
             f"{parsed.meeting_id!r}", locator, _position_of(row),
             {"meeting_id": value})
        return False
    return True


def _position_of(row: Dict[str, Any]) -> Optional[str]:
    value = row.get("position_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_fields(parsed: ParsedMeeting, row: Dict[str, Any], locator: str,
                    fields: Iterable[str]) -> bool:
    missing = [f for f in fields if f not in row]
    if missing:
        _add(parsed, "row-missing-field",
             "row is missing required field(s): " + ", ".join(missing),
             locator, _position_of(row), {"fields": missing})
        return False
    return True


def _text(parsed: ParsedMeeting, row: Dict[str, Any], field_name: str,
          locator: str) -> Optional[str]:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        _add(parsed, "row-wrong-type",
             f"{field_name} must be a non-empty string", locator,
             _position_of(row), {"field": field_name, "value": value})
        return None
    return value.strip()


def _integer(parsed: ParsedMeeting, row: Dict[str, Any], field_name: str,
             locator: str, code: str) -> Optional[int]:
    value = row.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        _add(parsed, code, f"{field_name} must be an integer", locator,
             _position_of(row), {"field": field_name, "value": value})
        return None
    return value


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #


def _parse_roster_row(parsed: ParsedMeeting, row: Dict[str, Any],
                      locator: str) -> None:
    if not _require_fields(parsed, row, locator, DECLARATION_FIELDS):
        return
    position_id = _text(parsed, row, "position_id", locator)
    holder_id = _text(parsed, row, "holder_id", locator)
    security_class = _text(parsed, row, "security_class", locator)
    declared_record = _text(parsed, row, "record_date", locator)
    shares = _integer(parsed, row, "shares", locator,
                      code="position-shares-invalid")
    if position_id is None or holder_id is None or security_class is None \
            or shares is None:
        return
    if shares <= 0:
        _add(parsed, "position-shares-invalid",
             "shares must be a positive integer", locator, position_id,
             {"shares": shares})
        return
    if declared_record is not None:
        try:
            declared = dt.date.fromisoformat(declared_record)
        except ValueError:
            declared = None
        if declared is None or declared != parsed.record_date.date():
            _add(parsed, "row-wrong-type",
                 f"roster record_date {declared_record!r} does not match the "
                 f"requested {parsed.record_date.date().isoformat()}",
                 locator, position_id,
                 {"field": "record_date", "value": declared_record})
            return
    if position_id in parsed.positions:
        _add(parsed, "row-duplicate-position",
             "position_id appears more than once in this roster version",
             locator, position_id,
             {"position_id": position_id,
              "first_locator": parsed.positions[position_id].locator})
        return
    parsed.positions[position_id] = RosterPosition(
        position_id, holder_id, security_class, shares, locator)


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #


def _parse_proposal_row(parsed: ParsedMeeting, row: Dict[str, Any],
                        locator: str) -> None:
    if not _require_fields(parsed, row, locator, PROPOSAL_FIELDS):
        return
    proposal_id = _text(parsed, row, "proposal_id", locator)
    if proposal_id is None:
        return
    title = row.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        _add(parsed, "row-wrong-type", "title must be a non-empty string",
             locator, None, {"field": "title"})
        return
    raw_options = row.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        _add(parsed, "proposal-options-invalid",
             "options must be a non-empty array of strings", locator, None,
             {"field": "options"})
        return
    options: List[str] = []
    for index, option in enumerate(raw_options):
        if not isinstance(option, str) or not option.strip():
            _add(parsed, "proposal-options-invalid",
                 "every option must be a non-empty string", locator, None,
                 {"index": index, "value": option})
            return
        text = option.strip()
        if text in options:
            _add(parsed, "proposal-options-duplicate",
                 f"option {text!r} appears more than once", locator, None,
                 {"option": text})
            return
        options.append(text)
    if proposal_id in parsed.proposals:
        _add(parsed, "row-duplicate-proposal",
             "proposal_id appears more than once", locator, None,
             {"proposal_id": proposal_id})
        return
    parsed.proposals[proposal_id] = Proposal(
        proposal_id, title.strip() if isinstance(title, str) else "",
        tuple(options), locator)


# --------------------------------------------------------------------------- #
# Grants
# --------------------------------------------------------------------------- #


def _parse_grant_row(parsed: ParsedMeeting, row: Dict[str, Any],
                     locator: str) -> None:
    if not _require_fields(parsed, row, locator, GRANT_FIELDS):
        return
    grant_id = _text(parsed, row, "grant_id", locator)
    position_id = _text(parsed, row, "position_id", locator)
    grantor = _text(parsed, row, "grantor", locator)
    grantee = _text(parsed, row, "grantee", locator)
    if grant_id is None or position_id is None or grantor is None \
            or grantee is None:
        return
    start = _integer(parsed, row, "unit_start", locator, "grant-unit-invalid")
    end = _integer(parsed, row, "unit_end", locator, "grant-unit-invalid")
    if start is None or end is None:
        return
    proposal_ids, proposals_ok = _proposal_set(parsed, row, locator,
                                               position_id)
    effective = _instant_field(parsed, row, "effective_at", locator,
                               "grant-time-invalid", position_id)
    if effective is None:
        return
    revoked_text = row.get("revoked_at")
    revoked: Optional[dt.datetime] = None
    if revoked_text is not None:
        revoked = _instant_field(parsed, row, "revoked_at", locator,
                                 "grant-time-invalid", position_id)
        if revoked is None:
            return
    parent = row.get("parent_grant_id")
    if parent is not None:
        if not isinstance(parent, str) or not parent.strip():
            _add(parsed, "row-wrong-type",
                 "parent_grant_id must be null or a non-empty string", locator,
                 position_id, {"field": "parent_grant_id", "value": parent})
            return
        parent = parent.strip()
    if not proposals_ok:
        return
    if start < 0 or end <= start:
        _add(parsed, "grant-unit-invalid",
             "grant range must be a non-empty half-open interval "
             "with 0 <= unit_start < unit_end", locator, position_id,
             {"unit_start": start, "unit_end": end})
        return
    if revoked is not None and revoked <= effective:
        _add(parsed, "grant-time-invalid",
             "revoked_at must be strictly later than effective_at "
             "(the revocation instant is not part of the grant)", locator,
             position_id,
             {"effective_at": row.get("effective_at"),
              "revoked_at": revoked_text})
        return
    parsed.grants.append(Grant(
        grant_id, position_id, grantor, grantee, start, end, proposal_ids,
        effective, revoked, parent, len(parsed.grants), locator))


def _instant_field(parsed: ParsedMeeting, row: Dict[str, Any],
                   field_name: str, locator: str, code: str,
                   position_id: Optional[str]) -> Optional[dt.datetime]:
    value = row.get(field_name)
    if not isinstance(value, str):
        _add(parsed, code, f"{field_name} must be an RFC 3339 string", locator,
             position_id, {"field": field_name, "value": value})
        return None
    try:
        return parse_utc(value)
    except ValueError as exc:
        _add(parsed, code, str(exc), locator, position_id,
             {"field": field_name, "value": value})
        return None


def _proposal_set(parsed: ParsedMeeting, row: Dict[str, Any], locator: str,
                  position_id: Optional[str]
                  ) -> Tuple[Tuple[str, ...], bool]:
    raw = row.get("proposal_ids")
    if not isinstance(raw, list) or not raw:
        _add(parsed, "grant-empty-proposals",
             "proposal_ids must be a non-empty array of proposal ids", locator,
             position_id, {"field": "proposal_ids", "value": raw})
        return (), False
    result: List[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            _add(parsed, "row-wrong-type",
                 "every proposal id must be a non-empty string", locator,
                 position_id, {"field": "proposal_ids", "index": index,
                               "value": value})
            return (), False
        text = value.strip()
        if text in result:
            _add(parsed, "grant-duplicate-proposal",
                 f"proposal {text!r} listed more than once", locator,
                 position_id, {"proposal_id": text})
            return (), False
        result.append(text)
    return tuple(result), True


# --------------------------------------------------------------------------- #
# Ballots
# --------------------------------------------------------------------------- #


def _parse_ballot_row(parsed: ParsedMeeting, row: Dict[str, Any],
                      locator: str) -> None:
    if not _require_fields(parsed, row, locator, BALLOT_FIELDS):
        return
    ballot_id = _text(parsed, row, "ballot_id", locator)
    position_id = _text(parsed, row, "position_id", locator)
    voter = _text(parsed, row, "voter", locator)
    proposal_id = _text(parsed, row, "proposal_id", locator)
    choice = _text(parsed, row, "choice", locator)
    if None in (ballot_id, position_id, voter, proposal_id, choice):
        return
    start = _integer(parsed, row, "unit_start", locator, "ballot-unit-invalid")
    end = _integer(parsed, row, "unit_end", locator, "ballot-unit-invalid")
    if start is None or end is None:
        return
    if start < 0 or end <= start:
        _add(parsed, "ballot-unit-invalid",
             "ballot range must be a non-empty half-open interval "
             "with 0 <= unit_start < unit_end", locator, position_id,
             {"unit_start": start, "unit_end": end})
        return
    submitted = _instant_field(parsed, row, "submitted_at", locator,
                               "ballot-time-invalid", position_id)
    if submitted is None:
        return
    proposal = parsed.proposals.get(proposal_id)
    if proposal is None:
        _add(parsed, "ballot-unknown-proposal",
             f"proposal {proposal_id!r} is not declared for this meeting",
             locator, position_id, {"proposal_id": proposal_id})
        return
    if choice not in proposal.options:
        _add(parsed, "ballot-choice-invalid",
             f"choice {choice!r} is not an option of proposal {proposal_id!r}",
             locator, position_id,
             {"proposal_id": proposal_id, "choice": choice,
              "options": list(proposal.options)})
        return
    parsed.ballots.append(Ballot(
        ballot_id, position_id, start, end, voter, proposal_id, choice,
        submitted, len(parsed.ballots), locator))
