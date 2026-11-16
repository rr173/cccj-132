"""Time parsing, timezone checks and DST-aware wall-clock helpers.

All instants are carried internally as timezone-aware datetimes in UTC.
Input timestamps are RFC 3339 with an explicit offset (``Z`` or ``±hh:mm``);
the offset must equal the meter's IANA timezone offset *at that instant*.

The two DST transitions need explicit, testable behaviour:

* **Fall-back (ambiguous wall time).** One local wall-clock hour exists twice.
  The two occurrences map to two different real UTC instants and are priced
  separately -- energy is never allocated to a "local hour", only to real
  elapsed seconds.
* **Spring-forward (missing wall time).** One local wall-clock hour does not
  exist. No reading or rule boundary can fall on a real instant inside it, so
  no usage is ever invented for it.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import List, Tuple

# RFC 3339 with mandatory offset; fractional seconds optional.
_RFC3339 = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt ]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<frac>\.\d+)?"
    r"(?P<offset>Z|z|[+-]\d{2}:\d{2})$"
)


def parse_rfc3339(text: str) -> dt.datetime:
    """Parse an RFC 3339 timestamp with a mandatory UTC offset.

    Returns a timezone-aware datetime (normalised to the stated offset, not
    UTC; call :func:`to_utc` to normalise).  Raises ``ValueError`` for anything
    that is not RFC 3339 with an explicit offset.
    """
    if not isinstance(text, str):
        raise ValueError(f"timestamp must be a string, got {type(text).__name__}")
    match = _RFC3339.match(text.strip())
    if match is None:
        raise ValueError(
            f"{text!r} is not an RFC 3339 timestamp with an explicit offset "
            "(expected e.g. 2024-03-10T06:30:00-05:00 or 2024-03-10T11:30:00Z)"
        )
    offset_text = match.group("offset")
    if offset_text in ("Z", "z"):
        offset = dt.timezone.utc
    else:
        sign = 1 if offset_text[0] == "+" else -1
        hours = int(offset_text[1:3])
        minutes = int(offset_text[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError(f"{text!r} has an out-of-range UTC offset")
        offset = dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    microsecond = 0
    if match.group("frac"):
        microsecond = int(round(float(match.group("frac")) * 1_000_000))
    try:
        parsed = dt.datetime(
            int(match.group("year")), int(match.group("month")),
            int(match.group("day")), int(match.group("hour")),
            int(match.group("minute")), int(match.group("second")),
            microsecond, tzinfo=offset,
        )
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a valid calendar timestamp: {exc}") from None
    return parsed


def to_utc(moment: dt.datetime) -> dt.datetime:
    """Normalise an aware datetime to UTC (still aware)."""
    if moment.tzinfo is None:
        raise ValueError("to_utc() requires a timezone-aware datetime")
    return moment.astimezone(dt.timezone.utc)


def parse_utc(text: str) -> dt.datetime:
    return to_utc(parse_rfc3339(text))


def offset_matches_zone(moment: dt.datetime, zone: dt.tzinfo) -> bool:
    """True when the timestamp's own UTC offset equals the zone offset then.

    ``moment`` may carry any offset; its *instant* is converted into the meter
    timezone and the offset declared on the input is compared with the offset
    the IANA rules produce for that instant.
    """
    as_local = moment.astimezone(zone)
    return as_local.utcoffset() == moment.utcoffset()


def local_at(instant_utc: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    """The wall-clock datetime of a UTC instant inside ``zone``."""
    return instant_utc.astimezone(zone)


def format_utc(instant_utc: dt.datetime) -> str:
    """Canonical RFC 3339 rendering of a UTC instant (``...Z``)."""
    return to_utc(instant_utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wall_to_utc_candidates(wall: dt.datetime, zone: dt.tzinfo) -> List[dt.datetime]:
    """Map a *naive* local wall-clock datetime to the real UTC instant(s).

    Returns one element for an ordinary instant and two distinct elements for a
    fall-back ambiguous wall time, ordered with ``fold=0`` first (the earlier
    occurrence) and ``fold=1`` second (the repeated hour).  A spring-forward
    gap wall time yields the two surrounding regime instants; use
    :func:`wall_boundary_instants` to filter out the non-existent one.
    """
    if wall.tzinfo is not None:
        raise ValueError("wall_to_utc_candidates() expects a naive wall-clock datetime")
    candidates: List[dt.datetime] = []
    for fold in (0, 1):
        aware = wall.replace(fold=fold, tzinfo=zone)
        instant = to_utc(aware)
        if instant not in candidates:
            candidates.append(instant)
    return candidates


def wall_boundary_instants(wall: dt.datetime, zone: dt.tzinfo) -> List[dt.datetime]:
    """Real UTC instants on which the wall clock *equals* ``wall``.

    Unlike :func:`wall_to_utc_candidates`, instants that only exist because
    PEP 495 projects a non-existent (spring-forward gap) wall time onto a
    neighbouring instant are dropped: that wall time never happens, so it
    cannot be a tariff boundary.  Ambiguous fall-back walls return both real
    occurrences.
    """
    if wall.tzinfo is not None:
        raise ValueError("wall_boundary_instants() expects a naive wall-clock datetime")
    result: List[dt.datetime] = []
    for instant in wall_to_utc_candidates(wall, zone):
        if local_at(instant, zone).replace(tzinfo=None) == wall:
            result.append(instant)
    return result


def classify_wall(wall: dt.datetime, zone: dt.tzinfo) -> str:
    """Classify a naive wall-clock datetime in ``zone``.

    * ``"ordinary"``  -- exactly one real instant and the wall time exists;
    * ``"ambiguous"`` -- fall-back: two distinct real instants share it;
    * ``"missing"``   -- spring-forward: this wall time never happens.
    """
    first = wall.replace(fold=0, tzinfo=zone)
    second = wall.replace(fold=1, tzinfo=zone)
    if first.utcoffset() != second.utcoffset():
        # Gap or ambiguous: distinguish by whether fold=0 round-trips.
        if local_at(to_utc(first), zone).replace(tzinfo=None) == wall:
            return "ambiguous"
        return "missing"
    return "ordinary"


def iso_weekday(instant_utc: dt.datetime, zone: dt.tzinfo) -> int:
    """ISO weekday (1=Monday ... 7=Sunday) of the local calendar day."""
    return local_at(instant_utc, zone).isoweekday()


def local_date_key(instant_utc: dt.datetime,
                   zone: dt.tzinfo) -> Tuple[int, int, int]:
    local = local_at(instant_utc, zone)
    return (local.year, local.month, local.day)


def fold_aware_minute_of_day(instant_utc: dt.datetime,
                             zone: dt.tzinfo) -> int:
    """Minute-of-day used for tariff lookup at a real instant.

    For the real seconds inside a spring-forward gap the IANA projection
    already lands on the last existing pre-gap wall clock (e.g. New York
    06:00Z during the 02:00->03:00 jump reads as local 01:00), which is
    exactly the rule timeline that must apply.  Fall-back repeats keep their
    PEP 495 ``fold`` flag.
    """
    local = local_at(instant_utc, zone)
    return local.hour * 60 + local.minute


def transition_instants(start: dt.datetime, end: dt.datetime,
                        zone: dt.tzinfo) -> List[dt.datetime]:
    """Real UTC instants of DST transitions in the half-open ``[start, end)``.

    A transition is the first second of a new offset regime, discovered by
    binary searching for adjacent seconds whose offsets differ.  A
    spring-forward gap therefore reports its post-gap instant while the
    (non-existent) wall hour is never represented as real time.
    """
    result: List[dt.datetime] = []

    def search(lo: dt.datetime, hi: dt.datetime) -> None:
        if lo.astimezone(zone).utcoffset() == hi.astimezone(zone).utcoffset():
            return
        span = int((hi - lo).total_seconds())
        if span == 1:
            result.append(hi)
            return
        mid = lo + dt.timedelta(seconds=span // 2)
        search(lo, mid)
        search(mid, hi)

    search(start, end)
    return sorted(set(result))


def parse_hhmm(text: str) -> int:
    """Parse ``"HH:MM"`` (``24:00`` allowed as end-of-day) to minutes."""
    if not isinstance(text, str) or not re.match(r"^\d{2}:\d{2}$", text):
        raise ValueError(f"{text!r} must look like HH:MM")
    hour, minute = (int(part) for part in text.split(":"))
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError(f"{text!r} is not a valid half-open time of day")
    return hour * 60 + minute
