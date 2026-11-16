"""The settlement engine.

The algorithm, per meter/period:

1. Validate everything that could make the bill untrustworthy (missing meter
   or tariff, malformed readings, wrong offsets, duplicate-reading conflicts,
   illegal counter roll-backs, missing boundary readings).  Any failure
   rejects *that period only* and is returned with a stable problem code.
2. Choose the readings covering both period ends: the last reading at or
   before ``start`` and the first at or after ``end`` (exact matches count).
3. Between two adjacent readings the meter is assumed to advance at a
   constant rate: a clip of ``c`` real seconds out of a pair lasting ``d``
   seconds owns ``c/d`` of the pair's energy (including one modulus roll-over
   when configured).  All arithmetic is exact :class:`fractions.Fraction`.
4. Each clipped real-time interval is split at tariff rule boundaries and DST
   transition instants, never at local clock labels, so the repeated hour of
   a fall-back day is billed as two real segments and the missing hour of a
   spring-forward day receives no invented energy.
5. Slices are aggregated per tariff detail, reconciled so the displayed
   decimal usages sum exactly to the usage derived from the boundary readings,
   and money is rounded half-up to the cent *after* aggregation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from ..errors import Problem
from . import timeutil
from .inputs import Meter, ParsedInputs, PeriodRequest, RawReading, Tariff

BILLING_ENGINE_VERSION = "1.0.0"

_USAGE_PLACES = 9
_USAGE_UNIT = Decimal(1).scaleb(-_USAGE_PLACES)
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class WindowRow:
    """A validated reading plus the roll-over count arriving *at* it."""

    reading: RawReading
    rollovers_in: int


# =========================================================================== #
# Public entry point
# =========================================================================== #


def settle_inputs(parsed: ParsedInputs) -> Dict[str, Any]:
    """Settle every period in parsed inputs; never raises for data problems.

    Returns a JSON-serialisable document with separate ``bills`` and
    ``rejected`` buckets, each sorted deterministically so repeated runs over
    identical inputs produce identical output.
    """
    bills: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for period in parsed.periods:
        outcome = _settle_one(period, parsed)
        if outcome["status"] == "settled":
            bills.append(outcome)
        else:
            rejected.append(outcome)
    bills.sort(key=lambda b: (b["meter_id"], b["period"]["start_utc"], b["period_id"]))
    rejected.sort(key=lambda r: (r["meter_id"], r["period_id"]))
    return {
        "engine_version": BILLING_ENGINE_VERSION,
        "sources": dict(sorted(parsed.sources.items())),
        "summary": {"periods": len(parsed.periods),
                    "settled": len(bills), "rejected": len(rejected)},
        "bills": bills,
        "rejected": rejected,
    }


# =========================================================================== #
# Per-period validation
# =========================================================================== #


def _settle_one(period: PeriodRequest, parsed: ParsedInputs) -> Dict[str, Any]:
    problems: List[Problem] = list(parsed.period_problems.get(period.period_id, ()))

    meter = parsed.meters.get(period.meter_id)
    if meter is None:
        problems.append(Problem(
            "meter-unknown",
            f"meter {period.meter_id!r} is not registered or is itself invalid",
            f"period:{period.period_id}",
        ))
    tariff = parsed.tariffs.get(period.tariff_id)
    if tariff is None:
        problems.append(Problem(
            "tariff-unknown",
            f"tariff {period.tariff_id!r} is not registered or is itself invalid",
            f"period:{period.period_id}",
        ))
    if problems:
        return _rejection(period, problems)

    assert meter is not None and tariff is not None
    problems.extend(parsed.meter_problems.get(meter.meter_id, ()))
    problems.extend(parsed.tariff_problems.get(tariff.tariff_id, ()))

    start = timeutil.parse_utc(period.start_text)
    end = timeutil.parse_utc(period.end_text)
    for boundary, text in (("start", period.start_text), ("end", period.end_text)):
        aware = timeutil.parse_rfc3339(text)
        if not timeutil.offset_matches_zone(aware, meter.zone):
            problems.append(Problem(
                "offset-mismatch",
                f"period {boundary} offset {aware.utcoffset()} does not match the "
                f"{meter.timezone} offset at that instant",
                f"period:{period.period_id}",
                {"boundary": boundary, "timestamp": text, "timezone": meter.timezone},
            ))
    if problems:
        return _rejection(period, problems)

    window_problems, window = _prepare_reading_window(meter, start, end, parsed)
    problems.extend(window_problems)
    if problems:
        return _rejection(period, problems)
    assert window is not None

    try:
        return _build_bill(period, meter, tariff, start, end, window)
    except _SettlementError as exc:
        return _rejection(period, [exc.problem])


def _rejection(period: PeriodRequest, problems: List[Problem]) -> Dict[str, Any]:
    unique: List[Problem] = []
    seen = set()
    for problem in problems:
        key = (problem.code, problem.locator, problem.message)
        if key not in seen:
            seen.add(key)
            unique.append(problem)
    return {
        "status": "rejected",
        "period_id": period.period_id,
        "meter_id": period.meter_id,
        "tariff_id": period.tariff_id,
        "problems": [problem.to_dict() for problem in unique],
    }


class _SettlementError(Exception):
    def __init__(self, problem: Problem):
        super().__init__(problem.render())
        self.problem = problem


# --------------------------------------------------------------------------- #
# Reading window: parse problems, offsets, duplicates, monotonicity, coverage
# --------------------------------------------------------------------------- #


def _prepare_reading_window(
    meter: Meter, start: dt.datetime, end: dt.datetime, parsed: ParsedInputs
) -> Tuple[List[Problem], Optional[List[WindowRow]]]:
    problems: List[Problem] = []

    malformed = [
        problem
        for (meter_id, index), bucket in parsed.reading_problems.items()
        if meter_id == meter.meter_id
        for problem in bucket
    ]
    problems.extend(malformed)

    good: List[RawReading] = []
    for row in parsed.readings.get(meter.meter_id, ()):
        if row.instant is None or row.watt_hours is None:
            continue
        aware = timeutil.parse_rfc3339(row.timestamp_text)
        if not timeutil.offset_matches_zone(aware, meter.zone):
            problems.append(Problem(
                "offset-mismatch",
                f"reading offset {aware.utcoffset()} does not match the "
                f"{meter.timezone} offset at that instant",
                f"readings[{row.index}]",
                {"meter_id": meter.meter_id, "timestamp": row.timestamp_text,
                 "timezone": meter.timezone},
            ))
        good.append(row)
    if problems:
        return problems, None

    good.sort(key=lambda r: (r.instant, r.index))

    before = _last_at_or_before(good, start)
    after = _first_at_or_after(good, end)
    if before is None or after is None:
        side = "start" if before is None else "end"
        problems.append(Problem(
            "missing-boundary-reading",
            f"no reading covers the period {side}; a reading on or "
            f"{'before' if side == 'start' else 'after'} the boundary is required",
            f"period-meter:{meter.meter_id}",
            {"meter_id": meter.meter_id, "boundary": side,
             "boundary_utc": timeutil.format_utc(start if side == "start" else end)},
        ))
        return problems, None

    window_rows = [r for r in good if before.instant <= r.instant <= after.instant]

    # Duplicates at the same real instant: equal values merge, differing values
    # invalidate the period -- we must not pick one.
    merged: List[RawReading] = []
    cursor: Optional[RawReading] = None
    for row in window_rows:
        if cursor is not None and row.instant == cursor.instant:
            if row.watt_hours != cursor.watt_hours:
                problems.append(Problem(
                    "duplicate-reading-conflict",
                    f"two readings at {row.timestamp_text} report different cumulative "
                    f"values ({cursor.watt_hours} vs {row.watt_hours})",
                    f"readings[{cursor.index}]",
                    {"meter_id": meter.meter_id, "timestamp": row.timestamp_text,
                     "values": [str(cursor.watt_hours), str(row.watt_hours)],
                     "indexes": [cursor.index, row.index]},
                ))
            continue
        merged.append(row)
        cursor = row
    if problems:
        return problems, None

    # Monotonicity with optional, explicitly configured modulus roll-over.
    validated: List[WindowRow] = []
    for row in merged:
        rollovers = 0
        if validated:
            previous = validated[-1].reading
            if row.watt_hours < previous.watt_hours:
                if not _is_legal_rollover(previous, row, meter):
                    problems.append(Problem(
                        "reading-rollback",
                        "cumulative reading decreased without a legal modulus roll-over",
                        f"readings[{row.index}]",
                        {"meter_id": meter.meter_id,
                         "from_index": previous.index, "to_index": row.index,
                         "from_value": str(previous.watt_hours),
                         "to_value": str(row.watt_hours),
                         "modulus": str(meter.modulus) if meter.modulus else None},
                    ))
                    continue
                rollovers = 1
        validated.append(WindowRow(row, rollovers))
    if problems:
        return problems, None
    return [], validated


def _is_legal_rollover(previous: RawReading, row: RawReading, meter: Meter) -> bool:
    if meter.modulus is None:
        return False
    return (Decimal(0) <= previous.watt_hours < meter.modulus
            and Decimal(0) <= row.watt_hours < meter.modulus)


def _last_at_or_before(rows: List[RawReading],
                       instant: dt.datetime) -> Optional[RawReading]:
    result: Optional[RawReading] = None
    for row in rows:
        if row.instant <= instant:
            result = row
        else:
            break
    return result


def _first_at_or_after(rows: List[RawReading],
                       instant: dt.datetime) -> Optional[RawReading]:
    for row in rows:
        if row.instant >= instant:
            return row
    return None


# =========================================================================== #
# Bill construction
# =========================================================================== #


def _build_bill(period: PeriodRequest, meter: Meter, tariff: Tariff,
                start: dt.datetime, end: dt.datetime,
                window: List[WindowRow]) -> Dict[str, Any]:
    zone = meter.zone
    rate_per_wh = _rate_per_wh(tariff)
    hard_transitions = _offset_transitions(start, end, zone)

    # A detail line is identified by (label, declared rate): several rules can
    # share a label (a night rate on both sides of midnight) and the same label
    # could be priced differently across exceptions -- only identical pricing
    # is ever merged onto one line.
    detail_energy: Dict[Tuple[str, str], Fraction] = {}
    detail_meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
    segments: List[Dict[str, Any]] = []
    total_seconds = 0

    for previous, current in zip(window, window[1:]):
        pair_start, pair_end = previous.reading.instant, current.reading.instant
        clip_start = max(pair_start, start)
        clip_end = min(pair_end, end)
        if clip_start >= clip_end:
            continue
        pair_seconds = int((pair_end - pair_start).total_seconds())
        clip_seconds = int((clip_end - clip_start).total_seconds())
        pair_energy = _pair_energy(previous, current, meter)
        allocated_energy = pair_energy * Fraction(clip_seconds, pair_seconds)

        slices = _rate_slices(clip_start, clip_end, zone, tariff,
                              hard_transitions)
        slice_records: List[Dict[str, Any]] = []
        for slice_start, slice_end, label, rate_value, source in slices:
            seconds = int((slice_end - slice_start).total_seconds())
            energy = pair_energy * Fraction(seconds, pair_seconds)
            key = (label, str(rate_value))
            detail_energy[key] = detail_energy.get(key, Fraction(0)) + energy
            meta = detail_meta.setdefault(
                key, {"rate": rate_value, "sources": []})
            if source not in meta["sources"]:
                meta["sources"].append(source)
            slice_records.append(_slice_record(
                slice_start, slice_end, zone, seconds, label, source))

        total_seconds += clip_seconds
        segments.append({
            "reading_from_index": previous.reading.index,
            "reading_to_index": current.reading.index,
            "interval_start_utc": timeutil.format_utc(clip_start),
            "interval_end_utc": timeutil.format_utc(clip_end),
            "pair_start_utc": timeutil.format_utc(pair_start),
            "pair_end_utc": timeutil.format_utc(pair_end),
            "pair_seconds": pair_seconds,
            "allocated_seconds": clip_seconds,
            "pair_energy_wh": _decimal_text(Fraction(pair_energy)),
            "allocated_energy_wh": _decimal_text(allocated_energy),
            "rollovers": current.rollovers_in,
            "slices": slice_records,
        })

    if not segments:
        raise _SettlementError(Problem(
            "missing-boundary-reading",
            "no reading pair overlaps the period interval",
            f"period:{period.period_id}",
        ))

    start_value = _interpolate(window, start, meter)
    end_value = _interpolate(window, end, meter)
    boundary_usage = end_value - start_value
    sliced_usage = sum(detail_energy.values(), Fraction(0))
    if boundary_usage != sliced_usage:
        raise _SettlementError(Problem(
            "internal-reconciliation",
            "sliced usage does not match boundary-derived usage",
            f"period:{period.period_id}",
            {"boundary_usage": str(boundary_usage),
             "sliced_usage": str(sliced_usage)},
        ))

    details, total_wh = _reconcile_details(detail_energy, detail_meta, rate_per_wh,
                                           boundary_usage, tariff, period.currency)
    total_amount = sum((Decimal(item["amount"]) for item in details), Decimal("0.00"))
    precise_amount = sum((Decimal(item["amount_precise"]) for item in details),
                         Decimal("0"))

    before_row = _last_at_or_before([w.reading for w in window], start)
    after_row = _first_at_or_after([w.reading for w in window], end)
    assert before_row is not None and after_row is not None

    return {
        "status": "settled",
        "period_id": period.period_id,
        "meter_id": meter.meter_id,
        "tariff_id": tariff.tariff_id,
        "currency": period.currency,
        "rate_unit": tariff.rate_unit,
        "timezone": meter.timezone,
        "period": {
            "start": period.start_text,
            "end": period.end_text,
            "start_utc": timeutil.format_utc(start),
            "end_utc": timeutil.format_utc(end),
            "seconds": total_seconds,
        },
        "boundary_readings": {
            "before": _reading_record(before_row),
            "after": _reading_record(after_row),
        },
        "participating_readings": [_reading_record(w.reading) for w in window],
        "boundary_values": {
            "start_wh": _decimal_text(start_value),
            "end_wh": _decimal_text(end_value),
        },
        "total_wh": total_wh,
        "total_amount": str(total_amount),
        "total_amount_precise": str(precise_amount),
        "details": details,
        "segments": segments,
    }


def _pair_energy(previous: WindowRow, current: WindowRow, meter: Meter) -> Fraction:
    diff = Fraction(current.reading.watt_hours - previous.reading.watt_hours)
    if current.rollovers_in:
        assert meter.modulus is not None
        diff += Fraction(meter.modulus) * current.rollovers_in
    return diff


def _interpolate(window: List[WindowRow], instant: dt.datetime,
                 meter: Meter) -> Fraction:
    """Cumulative counter value at an instant inside the covered window."""
    for previous, current in zip(window, window[1:]):
        p, r = previous.reading, current.reading
        if p.instant <= instant <= r.instant:
            pair_seconds = int((r.instant - p.instant).total_seconds())
            energy = _pair_energy(previous, current, meter)
            elapsed = int((instant - p.instant).total_seconds())
            return Fraction(p.watt_hours) + energy * Fraction(elapsed, pair_seconds)
    for wrapped in window:
        if wrapped.reading.instant == instant:
            return Fraction(wrapped.reading.watt_hours)
    raise _SettlementError(Problem(
        "missing-boundary-reading",
        "instant falls outside the covering readings",
        f"meter:{meter.meter_id}",
    ))


# --------------------------------------------------------------------------- #
# Tariff lookup and real-time rate slicing
# --------------------------------------------------------------------------- #


def _rate_per_wh(tariff: Tariff) -> Dict[str, Decimal]:
    """Map every declared rate (as text) to its money-per-Wh equivalent."""
    rates: Dict[str, Decimal] = {}

    def add(value: Decimal) -> None:
        rates[str(value)] = value if tariff.rate_unit == "per_wh" \
            else value / Decimal(1000)

    for rules in tariff.weekday_rules.values():
        for rule in rules:
            add(rule.rate)
    for exception in tariff.day_exceptions.values():
        add(exception.rate)
    return rates


def _lookup_rule(instant: dt.datetime, zone: dt.tzinfo,
                 tariff: Tariff) -> Tuple[str, Decimal, Dict[str, Any]]:
    date_key = timeutil.local_date_key(instant, zone)
    exception = tariff.day_exceptions.get(date_key)
    if exception is not None:
        return exception.label, exception.rate, exception.source
    weekday = timeutil.iso_weekday(instant, zone)
    minute = timeutil.fold_aware_minute_of_day(instant, zone)
    for rule in tariff.weekday_rules[weekday]:
        if rule.start_minute <= minute < rule.end_minute:
            return rule.label, rule.rate, rule.source
    raise _SettlementError(Problem(
        "tariff-gap",
        "no tariff rule covers this instant",
        f"tariff:{tariff.tariff_id}",
        {"instant_utc": timeutil.format_utc(instant)},
    ))


def _offset_transitions(start: dt.datetime, end: dt.datetime,
                        zone: dt.tzinfo) -> List[dt.datetime]:
    """Fall-back transition instants inside ``(start, end)``.

    Only fall-backs need an explicit slice boundary: the repeated wall time
    is two real ranges that must be priced separately even under one rule.
    Spring-forward gaps need no boundary -- the non-existent wall time is
    simply absent from the rule-boundary expansion and the surrounding real
    seconds keep their (pre-gap) rule.
    """
    instants = [i for i in timeutil.transition_instants(start, end, zone)
                if start < i < end]
    hard: List[dt.datetime] = []
    for instant in instants:
        local = timeutil.local_at(instant, zone)
        before_offset = (instant - dt.timedelta(seconds=1)).astimezone(
            zone).utcoffset()
        if local.utcoffset() < before_offset:
            hard.append(instant)
    return sorted(hard)


def _rate_slices(
    clip_start: dt.datetime, clip_end: dt.datetime, zone: dt.tzinfo,
    tariff: Tariff, hard_transitions: List[dt.datetime],
) -> List[Tuple[dt.datetime, dt.datetime, str, Decimal, Dict[str, Any]]]:
    boundaries = _boundary_instants(clip_start, clip_end, zone, tariff,
                                    hard_transitions)
    raw: List[Tuple[dt.datetime, dt.datetime, str, Decimal, Dict[str, Any]]] = []
    cursor = clip_start
    for boundary in boundaries:
        label, rate, source = _lookup_rule(cursor, zone, tariff)
        raw.append((cursor, boundary, label, rate, source))
        cursor = boundary
    label, rate, source = _lookup_rule(cursor, zone, tariff)
    raw.append((cursor, clip_end, label, rate, source))

    # Rule boundaries projected from a non-existent (spring-gap) wall time
    # produce no candidate at all, so adjacent real ranges under one rule may
    # still meet on a harmless intermediate point; merge them, but never cross
    # a fall-back transition, where the repeated wall time stays two ranges.
    hard = set(hard_transitions)
    merged: List[Tuple[dt.datetime, dt.datetime, str, Decimal, Dict[str, Any]]] = []
    for piece in raw:
        piece_start = piece[0]
        if (merged and piece_start not in hard
                and merged[-1][2] == piece[2] and merged[-1][4] == piece[4]):
            previous = merged.pop()
            piece = (previous[0], piece[1], piece[2], piece[3], piece[4])
        merged.append(piece)
    return merged


def _boundary_instants(clip_start: dt.datetime, clip_end: dt.datetime,
                       zone: dt.tzinfo, tariff: Tariff,
                       hard_transitions: List[dt.datetime]) -> List[dt.datetime]:
    points: List[dt.datetime] = list(hard_transitions)

    day = timeutil.local_at(clip_start, zone).date()
    last_day = timeutil.local_at(clip_end, zone).date()
    while day <= last_day:
        wall_midnight = dt.datetime(day.year, day.month, day.day, 0, 0, 0)
        points.extend(timeutil.wall_boundary_instants(wall_midnight, zone))
        for rule in tariff.weekday_rules.get(day.isoweekday(), ()):
            for kind, minute in (("start", rule.start_minute),
                                 ("end", rule.end_minute)):
                if minute == 24 * 60:
                    # 24:00 is the next local midnight, already enumerated.
                    continue
                wall = dt.datetime(day.year, day.month, day.day,
                                   minute // 60, minute % 60)
                points.extend(_rule_boundary_instants(wall, kind, zone))
        day = day + dt.timedelta(days=1)

    return sorted({p for p in points if clip_start < p < clip_end})


def _rule_boundary_instants(wall: dt.datetime, kind: str,
                            zone: dt.tzinfo) -> List[dt.datetime]:
    """Real UTC instants for a rule wall-time boundary.

    * Ordinary and fall-back (ambiguous) walls return every real occurrence.
    * A wall time inside a spring-forward gap never happens.  A rule *start*
      there becomes effective at the post-gap instant (the first real second
      on or after the intended wall clock); a rule *end* stops at the last
      pre-gap second, i.e. the boundary is the post-gap instant as well.  In
      both cases the single real boundary is the post-gap instant; the gap
      itself contains no boundary and keeps the preceding rule.
    """
    valid = timeutil.wall_boundary_instants(wall, zone)
    if valid:
        return valid
    candidates = timeutil.wall_to_utc_candidates(wall, zone)
    if timeutil.classify_wall(wall, zone) != "missing":
        return candidates
    # Post-gap instant: the candidate whose real wall clock is past the gap.
    post_gap = max(candidates)
    return [post_gap]


# --------------------------------------------------------------------------- #
# Decimal reconciliation and money rounding
# =========================================================================== #


def _reconcile_details(
    detail_energy: Dict[Tuple[str, str], Fraction],
    detail_meta: Dict[Tuple[str, str], Dict[str, Any]],
    rate_per_wh: Dict[str, Decimal],
    total_usage: Fraction,
    tariff: Tariff,
    currency: str,
) -> Tuple[List[Dict[str, Any]], str]:
    total_decimal = _fraction_to_decimal(total_usage).quantize(
        _USAGE_UNIT, ROUND_HALF_UP)

    # Largest-remainder allocation so the displayed detail usages sum exactly
    # to the boundary-derived total.
    ordered_keys = sorted(detail_energy)
    floored: Dict[Tuple[str, str], Decimal] = {}
    remainders: Dict[Tuple[str, str], Decimal] = {}
    for key in ordered_keys:
        exact = _fraction_to_decimal(detail_energy[key])
        floor_value = exact.quantize(_USAGE_UNIT, ROUND_FLOOR)
        floored[key] = floor_value
        remainders[key] = exact - floor_value
    allocated = sum(floored.values(), Decimal(0))
    leftover_units = int((total_decimal - allocated) / _USAGE_UNIT)
    ranked = sorted(ordered_keys, key=lambda k: (-remainders[k], k[0], k[1]))
    for key in ranked[:leftover_units]:
        floored[key] += _USAGE_UNIT

    details: List[Dict[str, Any]] = []
    for key in ordered_keys:
        label, _rate_text = key
        meta = detail_meta[key]
        rate_declared = meta["rate"]
        unit_rate = rate_per_wh[str(rate_declared)]
        exact_amount = _fraction_to_decimal(detail_energy[key]) * unit_rate
        amount = exact_amount.quantize(_CENT, ROUND_HALF_UP)
        sources = sorted(meta["sources"],
                         key=lambda src: (src["kind"], src["locator"]))
        details.append({
            "label": label,
            "rate": str(rate_declared),
            "rate_unit": tariff.rate_unit,
            "rate_per_wh": str(unit_rate),
            "watt_hours": str(floored[key]),
            "amount": str(amount),
            "amount_precise": str(exact_amount),
            "currency": currency,
            "rule_sources": sources,
        })
    return details, str(total_decimal)


def _fraction_to_decimal(value: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return Decimal(value.numerator) / Decimal(value.denominator)


def _decimal_text(value: Fraction) -> str:
    return str(_fraction_to_decimal(value))


# --------------------------------------------------------------------------- #
# Evidence records
# =========================================================================== #


def _reading_record(row: RawReading) -> Dict[str, Any]:
    return {
        "source_index": row.index,
        "meter_id": row.meter_id,
        "timestamp": row.timestamp_text,
        "instant_utc": timeutil.format_utc(row.instant),
        "watt_hours": str(row.watt_hours),
    }


def _slice_record(slice_start: dt.datetime, slice_end: dt.datetime,
                  zone: dt.tzinfo, seconds: int, label: str,
                  source: Dict[str, Any]) -> Dict[str, Any]:
    start_local = timeutil.local_at(slice_start, zone)
    end_local = timeutil.local_at(slice_end, zone)
    return {
        "start_utc": timeutil.format_utc(slice_start),
        "end_utc": timeutil.format_utc(slice_end),
        "start_local": _render_local(start_local),
        "start_fold": start_local.fold,
        "end_local": _render_local(end_local),
        "end_fold": end_local.fold,
        "seconds": seconds,
        "label": label,
        "rule_source": source,
    }


def _render_local(local: dt.datetime) -> str:
    offset = local.utcoffset() or dt.timedelta(0)
    total_minutes = abs(int(offset.total_seconds() // 60))
    sign = "+" if offset.total_seconds() >= 0 else "-"
    return (local.strftime("%Y-%m-%dT%H:%M:%S")
            + f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}")
