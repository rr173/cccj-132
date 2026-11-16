"""Parsing and structural validation of the four local input files.

Inputs are plain JSON documents:

* ``meters``   -- ``[{"meter_id", "timezone", "modulus"?}, ...]``
* ``readings`` -- ``[{"meter_id", "timestamp", "watt_hours"}, ...]`` where the
  timestamp is RFC 3339 with an explicit offset;
* ``tariffs``  -- ``[{"tariff_id", "weekday_rules", "day_exceptions"?}, ...]``
  with half-open ``[start, end)`` local times of day (``"24:00"`` allowed);
* ``periods``  -- ``[{"period_id", "meter_id", "tariff_id", "start", "end",
  "currency"?}, ...]`` with RFC 3339 ``[start, end)`` boundaries.

Structural problems are collected as :class:`~obsdeps.errors.Problem` objects
keyed by entity; the settlement engine turns them into per-period rejections.
Only a file that cannot be parsed at all raises
:class:`BillingInputError`.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import Problem
from .errors import BillingInputError
from . import timeutil

# --------------------------------------------------------------------------- #
# Parsed entities
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Meter:
    meter_id: str
    timezone: str
    zone: dt.tzinfo
    modulus: Optional[Decimal]  # None => the counter never wraps


@dataclass(frozen=True)
class RawReading:
    meter_id: str
    timestamp_text: str
    instant: Optional[dt.datetime]  # UTC, None when unparseable
    watt_hours: Optional[Decimal]   # None when unparseable/non-numeric
    index: int                      # position inside the source file


@dataclass(frozen=True)
class Rule:
    label: str
    start_minute: int
    end_minute: int
    rate: Decimal
    source: Dict[str, Any]


@dataclass(frozen=True)
class DayException:
    date: Tuple[int, int, int]
    label: str
    rate: Decimal
    source: Dict[str, Any]


@dataclass(frozen=True)
class Tariff:
    tariff_id: str
    rate_unit: str                 # "per_kwh" or "per_wh"
    weekday_rules: Dict[int, List[Rule]]   # ISO weekday 1..7
    day_exceptions: Dict[Tuple[int, int, int], DayException]


@dataclass(frozen=True)
class PeriodRequest:
    period_id: str
    meter_id: str
    tariff_id: str
    start_text: str
    end_text: str
    currency: str
    index: int


@dataclass
class ParsedInputs:
    meters: Dict[str, Meter] = field(default_factory=dict)
    meter_problems: Dict[str, List[Problem]] = field(default_factory=dict)
    readings: Dict[str, List[RawReading]] = field(default_factory=dict)
    reading_problems: Dict[Tuple[str, int], List[Problem]] = field(default_factory=dict)
    tariffs: Dict[str, Tariff] = field(default_factory=dict)
    tariff_problems: Dict[str, List[Problem]] = field(default_factory=dict)
    periods: List[PeriodRequest] = field(default_factory=list)
    period_problems: Dict[str, List[Problem]] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# File loading
# --------------------------------------------------------------------------- #


def load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise BillingInputError(f"cannot read input file: {exc}", source=path) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BillingInputError(
            f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
            source=path,
        ) from exc


def load_inputs(*, meters: Any, readings: Any, tariffs: Any, periods: Any,
                sources: Optional[Dict[str, str]] = None) -> ParsedInputs:
    """Parse and structurally validate the four input documents.

    Each argument may be a file path (string ending in ``.json`` only when it
    names an existing file), a JSON string, or an already-parsed Python
    structure.
    """
    documents = {
        "meters": _as_document(meters, "meters"),
        "readings": _as_document(readings, "readings"),
        "tariffs": _as_document(tariffs, "tariffs"),
        "periods": _as_document(periods, "periods"),
    }
    parsed = ParsedInputs(sources=dict(sources or {}))
    _parse_meters(documents["meters"], parsed)
    _parse_readings(documents["readings"], parsed)
    _parse_tariffs(documents["tariffs"], parsed)
    _parse_periods(documents["periods"], parsed)
    return parsed


def _as_document(value: Any, name: str) -> Any:
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("[", "{")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise BillingInputError(
                    f"{name} JSON is invalid at line {exc.lineno}: {exc.msg}",
                    source=f"<{name}>",
                ) from exc
        return load_json_file(value)
    return value


def _require_list(document: Any, name: str) -> List[Any]:
    if not isinstance(document, list):
        raise BillingInputError(f"{name} document must be a JSON array", source=f"<{name}>")
    return document


# --------------------------------------------------------------------------- #
# Meters
# --------------------------------------------------------------------------- #


def _parse_meters(document: Any, parsed: ParsedInputs) -> None:
    for index, item in enumerate(_require_list(document, "meters")):
        locator = f"meters[{index}]"
        if not isinstance(item, dict):
            _add(parsed.meter_problems, _id_of(item, f"meter#{index}"),
                 Problem("meter-malformed", "meter entry must be an object", locator))
            continue
        meter_id = item.get("meter_id")
        if not isinstance(meter_id, str) or not meter_id.strip():
            _add(parsed.meter_problems, f"meter#{index}",
                 Problem("meter-malformed", "meter_id must be a non-empty string", locator))
            continue
        meter_id = meter_id.strip()
        problems: List[Problem] = []
        timezone_text = item.get("timezone")
        zone: Optional[dt.tzinfo] = None
        if not isinstance(timezone_text, str) or not timezone_text.strip():
            problems.append(Problem("meter-timezone-missing",
                                    "timezone must be an IANA name", locator))
        else:
            try:
                zone = ZoneInfo(timezone_text.strip())
            except ZoneInfoNotFoundError:
                problems.append(Problem("meter-timezone-unknown",
                                        f"unknown IANA timezone {timezone_text!r}",
                                        locator, {"timezone": timezone_text}))
        modulus: Optional[Decimal] = None
        if "modulus" in item and item["modulus"] is not None:
            try:
                modulus = Decimal(str(item["modulus"]))
            except (InvalidOperation, ValueError):
                modulus = None
            if modulus is None or not modulus.is_finite() or modulus <= 0:
                problems.append(Problem("meter-modulus-invalid",
                                        "modulus must be a positive finite number",
                                        locator, {"modulus": item.get("modulus")}))
                modulus = None
        if meter_id in parsed.meters:
            problems.append(Problem("meter-duplicate",
                                    "meter_id appears more than once", locator))
            _add_all(parsed.meter_problems, meter_id, problems)
            continue
        if problems:
            _add_all(parsed.meter_problems, meter_id, problems)
            # Keep an unusable meter out of the usable map.
            continue
        parsed.meters[meter_id] = Meter(meter_id, timezone_text.strip(), zone, modulus)


# --------------------------------------------------------------------------- #
# Readings
# --------------------------------------------------------------------------- #


def _parse_readings(document: Any, parsed: ParsedInputs) -> None:
    rows: Dict[str, List[RawReading]] = {}
    for index, item in enumerate(_require_list(document, "readings")):
        locator = f"readings[{index}]"
        if not isinstance(item, dict):
            raise BillingInputError(f"{locator} must be an object", source="<readings>")
        meter_id = item.get("meter_id")
        if not isinstance(meter_id, str) or not meter_id.strip():
            raise BillingInputError(f"{locator}.meter_id must be a non-empty string",
                                    source="<readings>")
        meter_id = meter_id.strip()
        timestamp_text = item.get("timestamp")
        instant: Optional[dt.datetime] = None
        if not isinstance(timestamp_text, str):
            _add(parsed.reading_problems, (meter_id, index),
                 Problem("reading-timestamp-malformed",
                         "timestamp must be an RFC 3339 string", locator))
        else:
            try:
                instant = timeutil.parse_utc(timestamp_text)
            except ValueError as exc:
                _add(parsed.reading_problems, (meter_id, index),
                     Problem("reading-timestamp-malformed", str(exc), locator,
                             {"timestamp": timestamp_text}))
        watt_hours: Optional[Decimal] = None
        raw_value = item.get("watt_hours")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
            _add(parsed.reading_problems, (meter_id, index),
                 Problem("reading-value-malformed",
                         "watt_hours must be a number", locator,
                         {"watt_hours": raw_value}))
        else:
            try:
                watt_hours = Decimal(str(raw_value))
            except InvalidOperation:
                watt_hours = None
            if watt_hours is None or not watt_hours.is_finite() or watt_hours < 0:
                _add(parsed.reading_problems, (meter_id, index),
                     Problem("reading-value-malformed",
                             "watt_hours must be a finite non-negative number",
                             locator, {"watt_hours": raw_value}))
                watt_hours = None
        rows.setdefault(meter_id, []).append(
            RawReading(meter_id, timestamp_text if isinstance(timestamp_text, str) else "",
                       instant, watt_hours, index))
    parsed.readings = rows


# --------------------------------------------------------------------------- #
# Tariffs
# --------------------------------------------------------------------------- #


def _parse_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0:
        return None
    return result


def _parse_tariffs(document: Any, parsed: ParsedInputs) -> None:
    for index, item in enumerate(_require_list(document, "tariffs")):
        locator = f"tariffs[{index}]"
        if not isinstance(item, dict):
            _add(parsed.tariff_problems, f"tariff#{index}",
                 Problem("tariff-malformed", "tariff entry must be an object", locator))
            continue
        tariff_id = item.get("tariff_id")
        if not isinstance(tariff_id, str) or not tariff_id.strip():
            _add(parsed.tariff_problems, f"tariff#{index}",
                 Problem("tariff-malformed", "tariff_id must be a non-empty string", locator))
            continue
        tariff_id = tariff_id.strip()
        problems: List[Problem] = []
        rate_unit = item.get("rate_unit", "per_kwh")
        if rate_unit not in ("per_kwh", "per_wh"):
            problems.append(Problem("tariff-rate-unit-invalid",
                                    "rate_unit must be 'per_kwh' or 'per_wh'",
                                    locator, {"rate_unit": rate_unit}))
        weekday_rules, rule_problems = _parse_weekday_rules(
            item.get("weekday_rules"), locator)
        problems.extend(rule_problems)
        exceptions, exception_problems = _parse_exceptions(
            item.get("day_exceptions", []), locator)
        problems.extend(exception_problems)
        if tariff_id in parsed.tariffs:
            problems.append(Problem("tariff-duplicate",
                                    "tariff_id appears more than once", locator))
        if problems:
            _add_all(parsed.tariff_problems, tariff_id, problems)
            # Keep the tariff available with whatever rules parsed so a period
            # using it reports the precise structural problem, not just
            # "unknown tariff".
            if tariff_id not in parsed.tariffs:
                parsed.tariffs[tariff_id] = Tariff(
                    tariff_id, rate_unit, weekday_rules, exceptions)
            continue
        parsed.tariffs[tariff_id] = Tariff(tariff_id, rate_unit, weekday_rules, exceptions)


def _parse_weekday_rules(value: Any, locator: str
                         ) -> Tuple[Dict[int, List[Rule]], List[Problem]]:
    problems: List[Problem] = []
    result: Dict[int, List[Rule]] = {}
    if not isinstance(value, list) or not value:
        return result, [Problem("tariff-rules-missing",
                                "weekday_rules must be a non-empty array", locator)]
    for entry_index, entry in enumerate(value):
        entry_loc = f"{locator}.weekday_rules[{entry_index}]"
        if not isinstance(entry, dict):
            problems.append(Problem("tariff-rule-malformed",
                                    "weekday rule must be an object", entry_loc))
            continue
        weekdays = entry.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            problems.append(Problem("tariff-rule-malformed",
                                    "weekdays must be a non-empty array of 1..7",
                                    entry_loc))
            continue
        rules: List[Rule] = []
        for rule_index, raw_rule in enumerate(entry.get("rules", [])):
            rule_loc = f"{entry_loc}.rules[{rule_index}]"
            rule = _parse_rule(raw_rule, rule_loc)
            if isinstance(rule, Problem):
                problems.append(rule)
            else:
                rules.append(rule)
        for weekday in weekdays:
            if not isinstance(weekday, int) or isinstance(weekday, bool) \
                    or weekday < 1 or weekday > 7:
                problems.append(Problem("tariff-rule-malformed",
                                        "weekday must be an integer 1 (Mon) .. 7 (Sun)",
                                        entry_loc, {"weekday": weekday}))
                continue
            for rule in rules:
                result.setdefault(weekday, []).append(rule)
    coverage_problems = _check_day_coverage(result, locator)
    problems.extend(coverage_problems)
    return result, problems


def _parse_rule(raw: Any, locator: str) -> Any:
    if not isinstance(raw, dict):
        return Problem("tariff-rule-malformed", "rule must be an object", locator)
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        return Problem("tariff-rule-malformed", "rule label must be a non-empty string",
                       locator)
    try:
        start = timeutil.parse_hhmm(raw.get("start", ""))
        end = timeutil.parse_hhmm(raw.get("end", ""))
    except ValueError as exc:
        return Problem("tariff-rule-malformed", str(exc), locator)
    if start >= end:
        return Problem("tariff-rule-malformed",
                       "rule interval must be half-open with start < end", locator,
                       {"start": raw.get("start"), "end": raw.get("end")})
    rate = _parse_decimal(raw.get("rate"))
    if rate is None:
        return Problem("tariff-rate-invalid",
                       "rate must be a finite non-negative decimal", locator,
                       {"rate": raw.get("rate")})
    return Rule(label.strip(), start, end, rate,
                {"kind": "weekday", "locator": locator,
                 "start": raw.get("start"), "end": raw.get("end")})


def _check_day_coverage(rules_by_day: Dict[int, List[Rule]],
                        locator: str) -> List[Problem]:
    """The one invariant for a day is full, non-overlapping coverage.

    Two rules may share a label (e.g. one night rate on both sides of
    midnight); the engine aggregates such rules as long as their intervals do
    not overlap and the day has no gap.
    """
    problems: List[Problem] = []
    for weekday in range(1, 8):
        rules = sorted(rules_by_day.get(weekday, []), key=lambda r: r.start_minute)
        if not rules:
            problems.append(Problem("tariff-day-uncovered",
                                    "no rules cover this weekday", locator,
                                    {"weekday": weekday}))
            continue
        cursor = 0
        for rule in rules:
            if rule.start_minute > cursor:
                problems.append(Problem("tariff-gap",
                                        "rules leave a half-open gap on this weekday",
                                        rule.source["locator"],
                                        {"weekday": weekday,
                                         "gap": [_minute_hhmm(cursor),
                                                 _minute_hhmm(rule.start_minute)]}))
            elif rule.start_minute < cursor:
                problems.append(Problem("tariff-overlap",
                                        "rule intervals overlap on this weekday",
                                        rule.source["locator"],
                                        {"weekday": weekday,
                                         "overlap_at":
                                             _minute_hhmm(rule.start_minute)}))
            cursor = max(cursor, rule.end_minute)
        if cursor < 24 * 60:
            problems.append(Problem("tariff-gap",
                                    "rules do not reach 24:00 on this weekday", locator,
                                    {"weekday": weekday,
                                     "gap": [_minute_hhmm(cursor), "24:00"]}))
    return problems


def _minute_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _parse_exceptions(value: Any, locator: str
                      ) -> Tuple[Dict[Tuple[int, int, int], DayException], List[Problem]]:
    problems: List[Problem] = []
    result: Dict[Tuple[int, int, int], DayException] = {}
    if value is None:
        return result, problems
    if not isinstance(value, list):
        return result, [Problem("tariff-exception-malformed",
                                "day_exceptions must be an array", locator)]
    for index, entry in enumerate(value):
        entry_loc = f"{locator}.day_exceptions[{index}]"
        if not isinstance(entry, dict):
            problems.append(Problem("tariff-exception-malformed",
                                    "exception must be an object", entry_loc))
            continue
        date_text = entry.get("date")
        date_key = _parse_date(date_text)
        if date_key is None:
            problems.append(Problem("tariff-exception-malformed",
                                    "date must be YYYY-MM-DD", entry_loc,
                                    {"date": date_text}))
            continue
        label = entry.get("label")
        rate = _parse_decimal(entry.get("rate"))
        if not isinstance(label, str) or not label.strip():
            problems.append(Problem("tariff-exception-malformed",
                                    "exception label must be a non-empty string",
                                    entry_loc))
            continue
        if rate is None:
            problems.append(Problem("tariff-rate-invalid",
                                    "rate must be a finite non-negative decimal",
                                    entry_loc, {"rate": entry.get("rate")}))
            continue
        if date_key in result:
            problems.append(Problem("tariff-exception-duplicate",
                                    "two exceptions target the same local date",
                                    entry_loc, {"date": date_text}))
            continue
        result[date_key] = DayException(
            date_key, label.strip(), rate,
            {"kind": "day_exception", "locator": entry_loc, "date": date_text})
    return result, problems


def _parse_date(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    parts = value.split("-")
    if len(parts) != 3 or any(len(part) == 0 for part in parts):
        return None
    try:
        year, month, day = (int(part) for part in parts)
        dt.date(year, month, day)
    except (ValueError, TypeError):
        return None
    return (year, month, day)


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #


def _parse_periods(document: Any, parsed: ParsedInputs) -> None:
    for index, item in enumerate(_require_list(document, "periods")):
        locator = f"periods[{index}]"
        if not isinstance(item, dict):
            raise BillingInputError(f"{locator} must be an object", source="<periods>")
        period_id = item.get("period_id")
        if not isinstance(period_id, str) or not period_id.strip():
            raise BillingInputError(f"{locator}.period_id must be a non-empty string",
                                    source="<periods>")
        period_id = period_id.strip()
        meter_id = item.get("meter_id")
        tariff_id = item.get("tariff_id")
        if not isinstance(meter_id, str) or not meter_id.strip():
            raise BillingInputError(f"{locator}.meter_id must be a non-empty string",
                                    source="<periods>")
        if not isinstance(tariff_id, str) or not tariff_id.strip():
            raise BillingInputError(f"{locator}.tariff_id must be a non-empty string",
                                    source="<periods>")
        start_text = item.get("start")
        end_text = item.get("end")
        start_instant = end_instant = None
        problems: List[Problem] = []
        if not isinstance(start_text, str):
            problems.append(Problem("period-boundary-malformed",
                                    "start must be an RFC 3339 string", locator))
        else:
            try:
                start_instant = timeutil.parse_utc(start_text)
            except ValueError as exc:
                problems.append(Problem("period-boundary-malformed", str(exc),
                                        locator, {"boundary": "start",
                                                  "timestamp": start_text}))
        if not isinstance(end_text, str):
            problems.append(Problem("period-boundary-malformed",
                                    "end must be an RFC 3339 string", locator))
        else:
            try:
                end_instant = timeutil.parse_utc(end_text)
            except ValueError as exc:
                problems.append(Problem("period-boundary-malformed", str(exc),
                                        locator, {"boundary": "end",
                                                  "timestamp": end_text}))
        if start_instant is not None and end_instant is not None \
                and not start_instant < end_instant:
            problems.append(Problem("period-empty",
                                    "period must be half-open with start < end",
                                    locator))
        currency = item.get("currency", "USD")
        if not isinstance(currency, str) or not currency.strip():
            problems.append(Problem("period-currency-invalid",
                                    "currency must be a non-empty string", locator))
            currency = "USD"
        if period_id in parsed.period_problems or any(
                p.period_id == period_id for p in parsed.periods):
            problems.append(Problem("period-duplicate",
                                    "period_id appears more than once", locator))
        if problems:
            _add_all(parsed.period_problems, period_id, problems)
        parsed.periods.append(PeriodRequest(
            period_id, meter_id.strip(), tariff_id.strip(),
            start_text if isinstance(start_text, str) else "",
            end_text if isinstance(end_text, str) else "",
            currency.strip(), index))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _add(bucket: Dict[Any, List[Problem]], key: Any, problem: Problem) -> None:
    bucket.setdefault(key, []).append(problem)


def _add_all(bucket: Dict[Any, List[Problem]], key: Any,
             problems: List[Problem]) -> None:
    bucket.setdefault(key, []).extend(problems)


def _id_of(item: Any, fallback: str) -> str:
    if isinstance(item, dict) and isinstance(item.get("meter_id"), str):
        return item["meter_id"].strip() or fallback
    return fallback
