"""Deterministic billing fixtures with hand-worked expected values.

Every expected number in the billing tests is derived on paper below (and
re-derived independently inside each test); none is produced by the engine.
Timezone: ``America/New_York``, whose 2024 DST transitions are

* spring forward 2024-03-10 02:00 local -> 03:00 local (07:00Z), the hour
  02:00-03:00 does not exist;
* fall back 2024-11-03 02:00 local -> 01:00 local (06:00Z), the hour
  01:00-02:00 local exists twice (05:00Z-06:00Z EDT, then 06:00Z-07:00Z EST).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Tuple

# --------------------------------------------------------------------------- #
# Tariffs
# --------------------------------------------------------------------------- #

# Weekday/weekend TOU with one all-day exception (Fri 2024-03-15).
TARIFF_TOU = {
    "tariff_id": "TOU",
    "rate_unit": "per_kwh",
    "weekday_rules": [
        {"weekdays": [1, 2, 3, 4, 5], "rules": [
            {"label": "OFF", "start": "00:00", "end": "07:00", "rate": "0.10"},
            {"label": "PEAK", "start": "07:00", "end": "24:00", "rate": "0.20"},
        ]},
        {"weekdays": [6, 7], "rules": [
            {"label": "WEEKEND", "start": "00:00", "end": "24:00", "rate": "0.06"},
        ]},
    ],
    "day_exceptions": [
        {"date": "2024-03-15", "label": "HOLIDAY", "rate": "0.05"},
    ],
}

# Cheap first local hour, standard afterwards -- makes the fall-back repeat
# visible as two differently priced real hours.
TARIFF_HOUR = {
    "tariff_id": "HOUR",
    "rate_unit": "per_kwh",
    "weekday_rules": [
        {"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
            {"label": "CHEAP", "start": "00:00", "end": "01:00", "rate": "0.10"},
            {"label": "STD", "start": "01:00", "end": "24:00", "rate": "0.20"},
        ]},
    ],
}

# Flat tariff, handy for roll-over and rounding tests.
TARIFF_FLAT = {
    "tariff_id": "FLAT",
    "rate_unit": "per_kwh",
    "weekday_rules": [
        {"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
            {"label": "FLAT", "start": "00:00", "end": "24:00", "rate": "0.20"},
        ]},
    ],
}

# Rate change at 02:00 local, the hour removed by the 2024 spring jump.
# During the gap the real seconds keep reading the last existing wall clock
# (01:xx), so they stay on the pre-gap rate -- nothing is priced at 02:xx.
TARIFF_GAP = {
    "tariff_id": "GAP",
    "rate_unit": "per_kwh",
    "weekday_rules": [
        {"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
            {"label": "CHEAP", "start": "00:00", "end": "02:00", "rate": "0.10"},
            {"label": "STD", "start": "02:00", "end": "24:00", "rate": "0.20"},
        ]},
    ],
}

# Night 22:00-06:00 crossing midnight, then day.
TARIFF_NIGHT = {
    "tariff_id": "NIGHT",
    "rate_unit": "per_kwh",
    "weekday_rules": [
        {"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
            {"label": "NIGHT", "start": "00:00", "end": "06:00", "rate": "0.03"},
            {"label": "DAY", "start": "06:00", "end": "22:00", "rate": "0.06"},
            {"label": "NIGHT", "start": "22:00", "end": "24:00", "rate": "0.03"},
        ]},
    ],
}


def meters(specs: List[Tuple[str, Any]] = None) -> List[Dict[str, Any]]:
    specs = specs or [("M1", None)]
    out = []
    for meter_id, modulus in specs:
        row: Dict[str, Any] = {"meter_id": meter_id,
                               "timezone": "America/New_York"}
        if modulus is not None:
            row["modulus"] = modulus
        out.append(row)
    return out


def reading(meter_id: str, timestamp: str, value: Any) -> Dict[str, Any]:
    return {"meter_id": meter_id, "timestamp": timestamp, "watt_hours": value}


def period(period_id: str, meter_id: str, tariff_id: str, start: str,
           end: str) -> Dict[str, Any]:
    return {"period_id": period_id, "meter_id": meter_id,
            "tariff_id": tariff_id, "start": start, "end": end}


# --------------------------------------------------------------------------- #
# Independent reference implementation of the paper calculation.
#
# Given reading pairs and period boundaries (all given as UTC epoch seconds in
# the test's own arithmetic), energy is allocated by elapsed real seconds and
# the tester labels each real second-range itself.  This deliberately shares no
# code with the engine.
# --------------------------------------------------------------------------- #


def prorate(pair_energy_wh: Decimal, pair_seconds: int,
            clip_seconds: int) -> Fraction:
    """Energy owned by a clip inside one reading pair (exact fraction)."""
    return Fraction(pair_energy_wh) * Fraction(clip_seconds, pair_seconds)


def half_up_money(exact_amount: Decimal, places: int = 2) -> Decimal:
    return exact_amount.quantize(Decimal(1).scaleb(-places), rounding="ROUND_HALF_UP")
