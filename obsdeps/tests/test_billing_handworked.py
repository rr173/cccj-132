"""Hand-worked settlement tests.

Each test states its paper calculation explicitly.  Expected energies and
amounts are recomputed here with :class:`fractions.Fraction` / :class:`Decimal`
from the fixtures' raw numbers -- never copied from engine output -- so
agreement is meaningful.
"""

from __future__ import annotations

import unittest
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

from support import make_api  # noqa: F401  (import path bootstrap only)
from obsdeps.billing import BillingApi

import billing_fixtures as fx


def settle(meters, readings, tariffs, periods):
    with BillingApi() as api:
        return api.settle_documents(
            meters=meters, readings=readings, tariffs=tariffs, periods=periods)


def bill_by_id(result, period_id):
    return next(b for b in result["bills"] if b["period_id"] == period_id)


def details_map(bill):
    return {d["label"]: d for d in bill["details"]}


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP)


class CrossTariffBoundary(unittest.TestCase):
    """Scenario A: boundary proration across an 07:00 local rate change.

    Readings (M1, no modulus), cumulative Wh::

        2024-03-14 00:00 -04:00        0
        2024-03-14 12:00 -04:00    6 000      (500 Wh/h constant)
        2024-03-15 00:00 -04:00   12 000      (500 Wh/h constant)

    Period [Thu 06:00, Fri 00:00) local = 18 h = 64 800 s.

    Paper allocation:
        start value        0 + 500*6        = 3 000 Wh
        end value          12 000 Wh
        total usage        9 000 Wh
        OFF  06:00-07:00   1 h * 500        =   500 Wh @ 0.10/kWh
        PEAK 07:00-24:00  17 h * 500        = 8 500 Wh @ 0.20/kWh
        amounts            0.05 and 1.70    -> total 1.75
    """

    def setUp(self):
        self.readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-14T12:00:00-04:00", 6000),
            fx.reading("M1", "2024-03-15T00:00:00-04:00", 12000),
        ]
        self.period = fx.period(
            "A", "M1", "TOU",
            "2024-03-14T06:00:00-04:00", "2024-03-15T00:00:00-04:00")
        self.result = settle(fx.meters(), self.readings, [fx.TARIFF_TOU],
                             [self.period])

    def test_hand_computed_totals_and_lines(self):
        self.assertEqual(self.result["summary"],
                         {"periods": 1, "settled": 1, "rejected": 0})
        bill = bill_by_id(self.result, "A")
        self.assertEqual(bill["total_wh"], "9000.000000000")
        self.assertEqual(bill["total_amount"], "1.75")
        details = details_map(bill)
        self.assertEqual(details["OFF"]["watt_hours"], "500.000000000")
        self.assertEqual(details["OFF"]["amount"],
                         str(money(Decimal("500") * Decimal("0.0001"))))
        self.assertEqual(details["PEAK"]["watt_hours"], "8500.000000000")
        self.assertEqual(details["PEAK"]["amount"],
                         str(money(Decimal("8500") * Decimal("0.0002"))))

    def test_boundary_values_are_independently_interpolated(self):
        bill = bill_by_id(self.result, "A")
        self.assertEqual(bill["boundary_values"],
                         {"start_wh": "3000", "end_wh": "12000"})
        # Independent paper check: 6 h into a 12 h pair at 6000 Wh.
        self.assertEqual(
            Fraction(0) + Fraction(6000) * Fraction(6, 12), Fraction(3000))

    def test_evidence_segments_carry_seconds_and_sources(self):
        bill = bill_by_id(self.result, "A")
        seconds = sum(s["allocated_seconds"] for s in bill["segments"])
        self.assertEqual(seconds, 18 * 3600)
        # First pair clip 06:00-12:00 is cut at the 07:00 rule boundary.
        first = bill["segments"][0]
        self.assertEqual(len(first["slices"]), 2)
        self.assertEqual([sl["label"] for sl in first["slices"]],
                         ["OFF", "PEAK"])
        self.assertEqual(sum(sl["seconds"] for sl in first["slices"]),
                         first["allocated_seconds"])
        for segment in bill["segments"]:
            for sl in segment["slices"]:
                self.assertEqual(sl["rule_source"]["kind"], "weekday")

    def test_detail_usage_sums_to_boundary_usage_exactly(self):
        bill = bill_by_id(self.result, "A")
        summed = sum((Decimal(d["watt_hours"]) for d in bill["details"]),
                     Decimal(0))
        self.assertEqual(summed, Decimal(bill["total_wh"]))
        self.assertEqual(summed, Decimal(bill["boundary_values"]["end_wh"])
                         - Decimal(bill["boundary_values"]["start_wh"]))


class CrossMidnightRule(unittest.TestCase):
    """Scenario B: NIGHT/DAY tariff whose night window wraps midnight.

    Readings::

        2024-03-13 20:00 -04:00       0
        2024-03-14 08:00 -04:00   6 000     (500 Wh/h over 12 h)

    Period [Wed 22:00, Thu 08:00) = 10 h.  Boundary values 1 000 -> 6 000,
    total 5 000 Wh.  Local label timeline:
        22:00-24:00 NIGHT 2 h = 1 000 Wh @ 0.03
        00:00-06:00 NIGHT 6 h = 3 000 Wh @ 0.03   (same label, aggregated)
        06:00-08:00 DAY   2 h = 1 000 Wh @ 0.06
    NIGHT total 4 000 Wh -> 0.12; DAY 1 000 Wh -> 0.06; bill 0.18.
    """

    def test_cross_midnight_aggregates_same_label(self):
        readings = [
            fx.reading("M1", "2024-03-13T20:00:00-04:00", 0),
            fx.reading("M1", "2024-03-14T08:00:00-04:00", 6000),
        ]
        request = fx.period("B", "M1", "NIGHT",
                            "2024-03-13T22:00:00-04:00",
                            "2024-03-14T08:00:00-04:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_NIGHT], [request])
        bill = bill_by_id(result, "B")
        self.assertEqual(bill["total_wh"], "5000.000000000")
        details = details_map(bill)
        self.assertEqual(details["NIGHT"]["watt_hours"], "4000.000000000")
        self.assertEqual(details["NIGHT"]["amount"], "0.12")
        self.assertEqual(details["DAY"]["watt_hours"], "1000.000000000")
        self.assertEqual(details["DAY"]["amount"], "0.06")
        self.assertEqual(bill["total_amount"], "0.18")
        # A midnight boundary really appears in the evidence.
        utc_starts = [sl["start_utc"] for seg in bill["segments"]
                      for sl in seg["slices"]]
        self.assertIn("2024-03-14T04:00:00Z", utc_starts)  # local midnight -04:00


class FallBackRepeatedHour(unittest.TestCase):
    """Scenario C: the repeated 01:00 local hour is billed as two real hours.

    NY fall-back 2024-11-03: 01:00 local exists at 05:00Z (EDT, fold 0) and
    again at 06:00Z (EST, fold 1).

    Readings::

        2024-11-03 00:00 -04:00       0
        2024-11-03 03:00 -05:00   4 000     (1 000 Wh/h over 4 REAL hours)

    Period exactly spans them: 04:00Z-08:00Z = 14 400 s, 4 000 Wh.
        CHEAP 04:00Z-05:00Z (local 00-01, fold 0) 1 h = 1 000 Wh @ 0.10
        STD   05:00Z-08:00Z (includes the repeated local 01:00 hour)
              3 h = 3 000 Wh @ 0.20
        amounts 0.10 and 0.60 -> 0.70
    """

    def setUp(self):
        self.readings = [
            fx.reading("M1", "2024-11-03T00:00:00-04:00", 0),
            fx.reading("M1", "2024-11-03T03:00:00-05:00", 4000),
        ]
        self.request = fx.period(
            "C", "M1", "HOUR",
            "2024-11-03T00:00:00-04:00", "2024-11-03T03:00:00-05:00")
        self.result = settle(fx.meters(), self.readings, [fx.TARIFF_HOUR],
                             [self.request])

    def test_repeated_hour_two_real_segments(self):
        bill = bill_by_id(self.result, "C")
        self.assertEqual(bill["period"]["seconds"], 4 * 3600)
        details = details_map(bill)
        self.assertEqual(details["CHEAP"]["watt_hours"], "1000.000000000")
        self.assertEqual(details["STD"]["watt_hours"], "3000.000000000")
        self.assertEqual(bill["total_amount"], "0.70")
        slices = [sl for seg in bill["segments"] for sl in seg["slices"]]
        # The STD slice starts in the first occurrence of local 01:00.
        std = next(sl for sl in slices if sl["label"] == "STD")
        self.assertEqual(std["start_utc"], "2024-11-03T05:00:00Z")
        self.assertEqual(std["start_local"], "2024-11-03T01:00:00-04:00")
        self.assertEqual(std["start_fold"], 0)
        # Some slice boundary sits at 06:00Z = the second occurrence fold 1.
        self.assertIn("2024-11-03T06:00:00Z", [sl["end_utc"] for sl in slices])
        fold_one = next(sl for sl in slices
                        if sl["start_utc"] == "2024-11-03T06:00:00Z")
        self.assertEqual(fold_one["start_local"], "2024-11-03T01:00:00-05:00")
        self.assertEqual(fold_one["start_fold"], 1)
        # Same local label, two distinct real ranges are both priced.
        self.assertEqual(sum(sl["seconds"] for sl in slices), 4 * 3600)

    def test_energy_uses_real_seconds_not_clock_hours(self):
        # A naive 3-local-hour clock reading would give 3 h; the real elapsed
        # time is 4 h.  The bill must charge all 4 000 Wh, nothing duplicated
        # or dropped.
        bill = bill_by_id(self.result, "C")
        self.assertEqual(
            sum(Decimal(d["watt_hours"]) for d in bill["details"]),
            Decimal("4000.000000000"))


class SpringForwardMissingHour(unittest.TestCase):
    """Scenario D: the missing 02:00-03:00 local hour gets no invented energy.

    NY spring forward 2024-03-10.  Readings::

        2024-03-10 00:00 -05:00       0
        2024-03-10 04:00 -04:00   3 000     (1 000 Wh per REAL hour, 3 real h)

    The GAP tariff changes at local 02:00 -- the wall time that never
    happens.  Real timeline (1 000 Wh per real hour):
        05:00Z-06:00Z wall 00:xx            CHEAP 1 h = 1 000 Wh
        06:00Z-07:00Z wall frozen at 01:xx  CHEAP 1 h = 1 000 Wh
        07:00Z-08:00Z wall 03:xx            STD   1 h = 1 000 Wh
    Nothing is ever priced at the non-existent 02:xx wall clock.
        amounts 0.20 and 0.20 -> 0.40; total 3 000 Wh over 10 800 real s.
    """

    def test_missing_hour_has_no_slice_and_no_energy(self):
        readings = [
            fx.reading("M1", "2024-03-10T00:00:00-05:00", 0),
            fx.reading("M1", "2024-03-10T04:00:00-04:00", 3000),
        ]
        request = fx.period("D", "M1", "GAP",
                            "2024-03-10T00:00:00-05:00",
                            "2024-03-10T04:00:00-04:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_GAP], [request])
        bill = bill_by_id(result, "D")
        self.assertEqual(bill["period"]["seconds"], 3 * 3600)
        details = details_map(bill)
        self.assertEqual(details["CHEAP"]["watt_hours"], "2000.000000000")
        self.assertEqual(details["STD"]["watt_hours"], "1000.000000000")
        self.assertEqual(bill["total_amount"], "0.40")
        slices = [sl for seg in bill["segments"] for sl in seg["slices"]]
        # No slice ever claims the non-existent 02:00-03:00 local range.
        for sl in slices:
            self.assertFalse(
                sl["start_local"].startswith("2024-03-10T02:"))
        # The STD slice starts on the post-gap instant 07:00Z (local 03:00).
        std = next(sl for sl in slices if sl["label"] == "STD")
        self.assertEqual(std["start_utc"], "2024-03-10T07:00:00Z")
        self.assertEqual(std["start_local"], "2024-03-10T03:00:00-04:00")
        # Independent paper recomputation of the real-second split.
        cheap = Fraction(3000) * Fraction(2, 3)
        std_energy = Fraction(3000) * Fraction(1, 3)
        self.assertEqual(cheap, Fraction(2000))
        self.assertEqual(std_energy, Fraction(1000))


class DateExceptionOverridesWeekday(unittest.TestCase):
    """Scenario E: an all-day exception on Friday 2024-03-15 beats weekday rules.

    Readings::

        2024-03-15 00:00 -04:00       0
        2024-03-16 00:00 -04:00   2 000     (Friday, 24 h)

    Whole day inside the period.  Weekday TOU would price OFF/PEAK, but the
    exception applies HOLIDAY 0.05/kWh to every second:
        2 000 Wh * 0.00005 = 0.10.
    """

    def test_exception_rates_whole_day(self):
        readings = [
            fx.reading("M1", "2024-03-15T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-16T00:00:00-04:00", 2000),
        ]
        request = fx.period("E", "M1", "TOU",
                            "2024-03-15T00:00:00-04:00",
                            "2024-03-16T00:00:00-04:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_TOU], [request])
        bill = bill_by_id(result, "E")
        self.assertEqual(len(bill["details"]), 1)
        detail = bill["details"][0]
        self.assertEqual(detail["label"], "HOLIDAY")
        self.assertEqual(detail["watt_hours"], "2000.000000000")
        self.assertEqual(detail["amount"], "0.10")
        self.assertEqual(len(detail["rule_sources"]), 1)
        source = detail["rule_sources"][0]
        self.assertEqual(source["kind"], "day_exception")
        self.assertEqual(source["date"], "2024-03-15")

    def test_exception_priority_only_inside_its_day(self):
        # Period crossing Thursday weekday rules into the Friday exception.
        # Constant 100 Wh/h in both reading pairs.
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-15T00:00:00-04:00", 2400),
            fx.reading("M1", "2024-03-16T00:00:00-04:00", 4800),
        ]
        request = fx.period("E2", "M1", "TOU",
                            "2024-03-14T23:00:00-04:00",
                            "2024-03-15T01:00:00-04:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_TOU], [request])
        bill = bill_by_id(result, "E2")
        # 100 Wh/h.  Thu 23-24 PEAK 100 Wh; Fri 00-01 HOLIDAY 100 Wh.
        details = details_map(bill)
        self.assertEqual(details["PEAK"]["watt_hours"], "100.000000000")
        self.assertEqual(details["HOLIDAY"]["watt_hours"], "100.000000000")
        self.assertEqual(bill["total_wh"], "200.000000000")


class BoundaryProration(unittest.TestCase):
    """Scenario F: neither period end lands on a reading; seconds proration.

    Readings at 00:00 = 0 and 06:00 = 600 (100 Wh/h).  Period 01:00-05:00.
        start value 100, end value 500, total 400 Wh over 4 h.
    TOU: 01:00-07:00 is OFF, so all 400 Wh are OFF @ 0.10 -> 0.04.
    A second period 06:30-07:30 using readings 06:00=600 and 12:00=1200:
        start 650, end 750, total 100 Wh; OFF 30 min = 50 Wh,
        PEAK 30 min = 50 Wh -> 0.005 + 0.01 = 0.015 -> 0.02 half-up total
        (line amounts 0.01 + 0.01 = 0.02).
    """

    def test_interior_boundaries_prorate_by_seconds(self):
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-14T06:00:00-04:00", 600),
            fx.reading("M1", "2024-03-14T12:00:00-04:00", 1200),
        ]
        request = fx.period("F", "M1", "TOU",
                            "2024-03-14T01:00:00-04:00",
                            "2024-03-14T05:00:00-04:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_TOU], [request])
        bill = bill_by_id(result, "F")
        self.assertEqual(bill["boundary_values"],
                         {"start_wh": "100", "end_wh": "500"})
        self.assertEqual(bill["total_wh"], "400.000000000")
        self.assertEqual(details_map(bill)["OFF"]["amount"], "0.04")

    def test_clip_split_across_rate_change(self):
        readings = [
            fx.reading("M1", "2024-03-14T06:00:00-04:00", 600),
            fx.reading("M1", "2024-03-14T12:00:00-04:00", 1200),
        ]
        request = fx.period("F2", "M1", "TOU",
                            "2024-03-14T06:30:00-04:00",
                            "2024-03-14T07:30:00-04:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_TOU], [request])
        bill = bill_by_id(result, "F2")
        details = details_map(bill)
        self.assertEqual(details["OFF"]["watt_hours"], "50.000000000")
        self.assertEqual(details["PEAK"]["watt_hours"], "50.000000000")
        # Paper: 50 Wh * 0.0001 = 0.005 -> 0.01; 50 * 0.0002 = 0.01.
        self.assertEqual(details["OFF"]["amount"], "0.01")
        self.assertEqual(details["PEAK"]["amount"], "0.01")
        self.assertEqual(bill["total_amount"], "0.02")
        # Precise pre-rounding figures stay available for review.
        self.assertEqual(Decimal(details["OFF"]["amount_precise"]),
                         Decimal("0.005"))


class LegalRollover(unittest.TestCase):
    """Scenario G: explicit modulus, one wrap, values inside [0, modulus).

    modulus = 1000.  Readings 900 -> 50 over 3 h.  Physical energy
    50 - 900 + 1000 = 150 Wh.  Flat 0.20/kWh -> 0.03.
    """

    def test_single_wrap_counts_modulus_once(self):
        meter = fx.meters([("M1", 1000)])
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 900),
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 50),
        ]
        request = fx.period("G", "M1", "FLAT",
                            "2024-03-14T00:00:00-04:00",
                            "2024-03-14T03:00:00-04:00")
        result = settle(meter, readings, [fx.TARIFF_FLAT], [request])
        bill = bill_by_id(result, "G")
        self.assertEqual(bill["total_wh"], "150.000000000")
        self.assertEqual(bill["total_amount"], "0.03")
        self.assertEqual(bill["segments"][0]["rollovers"], 1)
        # Independent fraction check of the wrap arithmetic.
        self.assertEqual(Fraction(50) - Fraction(900) + Fraction(1000),
                         Fraction(150))

    def test_wrap_inside_prorated_clip(self):
        # Period starts midway; the wrap still belongs to the reading pair.
        meter = fx.meters([("M1", 1000)])
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 900),
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 50),
        ]
        request = fx.period("G2", "M1", "FLAT",
                            "2024-03-14T01:30:00-04:00",
                            "2024-03-14T03:00:00-04:00")
        result = settle(meter, readings, [fx.TARIFF_FLAT], [request])
        bill = bill_by_id(result, "G2")
        # Half of the pair's 150 Wh.
        self.assertEqual(bill["total_wh"], "75.000000000")
        self.assertEqual(bill["boundary_values"]["start_wh"], "975")


class FallBackBoundaryInsideRepeatedHour(unittest.TestCase):
    """Scenario C2: a rule boundary inside the repeated hour occurs twice.

    NY fall-back 2024-11-03. Rule: EARLY [00:00,01:30), LATE [01:30,24:00).
    Local 01:30 exists at 05:30Z (EDT, fold 0) and 06:30Z (EST, fold 1).

    Readings 04:00Z = 0 and 08:00Z = 5 000, i.e. 1 250 Wh per real hour over
    four real hours.  Real timeline:
        04:00Z-05:30Z EARLY 1.5 h
        05:30Z-06:00Z LATE  0.5 h
        06:00Z-06:30Z EARLY 0.5 h (second occurrence of the 01:00 hour)
        06:30Z-08:00Z LATE  1.5 h
    EARLY totals 2.0 h = 2 500 Wh; LATE totals 2.0 h = 2 500 Wh.
    """

    def setUp(self):
        tariff = [{
            "tariff_id": "REPEAT", "rate_unit": "per_kwh",
            "weekday_rules": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                {"label": "EARLY", "start": "00:00", "end": "01:30",
                 "rate": "0.10"},
                {"label": "LATE", "start": "01:30", "end": "24:00",
                 "rate": "0.20"},
            ]}],
        }]
        readings = [
            fx.reading("M1", "2024-11-03T00:00:00-04:00", 0),
            fx.reading("M1", "2024-11-03T03:00:00-05:00", 5000),
        ]
        request = fx.period("C2", "M1", "REPEAT",
                            "2024-11-03T00:00:00-04:00",
                            "2024-11-03T03:00:00-05:00")
        self.result = settle(fx.meters(), readings, tariff, [request])

    def test_two_real_boundaries_and_even_split(self):
        bill = bill_by_id(self.result, "C2")
        details = details_map(bill)
        self.assertEqual(details["EARLY"]["watt_hours"], "2500.000000000")
        self.assertEqual(details["LATE"]["watt_hours"], "2500.000000000")
        self.assertEqual(bill["total_wh"], "5000.000000000")
        slices = [sl for seg in bill["segments"] for sl in seg["slices"]]
        starts = {sl["start_utc"] for sl in slices}
        self.assertIn("2024-11-03T05:30:00Z", starts)
        self.assertIn("2024-11-03T06:30:00Z", starts)
        # The second occurrence is explicitly tagged with fold=1.
        second = next(sl for sl in slices
                      if sl["start_utc"] == "2024-11-03T06:30:00Z")
        self.assertEqual(second["start_local"], "2024-11-03T01:30:00-05:00")
        self.assertEqual(second["start_fold"], 1)
        # Every real second is accounted for exactly once.
        self.assertEqual(sum(sl["seconds"] for sl in slices), 4 * 3600)


class ReconciliationInvariants(unittest.TestCase):
    """Cross-checks the output stays internally consistent on every bill."""

    def test_non_terminating_split_reconciles_exactly(self):
        # 1 000 Wh split evenly across three hourly rules -> repeating thirds.
        tariff = [{
            "tariff_id": "THIRDS", "rate_unit": "per_kwh",
            "weekday_rules": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                {"label": "A", "start": "00:00", "end": "01:00", "rate": "0.10"},
                {"label": "B", "start": "01:00", "end": "02:00", "rate": "0.20"},
                {"label": "C", "start": "02:00", "end": "24:00", "rate": "0.30"},
            ]}],
        }]
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 1000),
        ]
        request = fx.period("R", "M1", "THIRDS",
                            "2024-03-14T00:00:00-04:00",
                            "2024-03-14T03:00:00-04:00")
        result = settle(fx.meters(), readings, tariff, [request])
        bill = bill_by_id(result, "R")
        values = sorted(d["watt_hours"] for d in bill["details"])
        # Largest-remainder: two lines at 333.333333333, one carries the unit.
        self.assertEqual(values, ["333.333333333", "333.333333333",
                                  "333.333333334"])
        summed = sum((Decimal(d["watt_hours"]) for d in bill["details"]),
                     Decimal(0))
        self.assertEqual(summed, Decimal(bill["total_wh"]))
        self.assertEqual(summed,
                         Decimal(bill["boundary_values"]["end_wh"])
                         - Decimal(bill["boundary_values"]["start_wh"]))
        # Rounded line amounts add up to the printed total.
        self.assertEqual(
            sum((Decimal(d["amount"]) for d in bill["details"]),
                Decimal("0.00")),
            Decimal(bill["total_amount"]))


class HalfUpRounding(unittest.TestCase):
    """Scenario H: cent rounding is Decimal ROUND_HALF_UP after aggregation."""

    def test_half_cent_goes_up(self):
        # 1 Wh at 0.05/kWh = 0.0005 -> 0.00 (below half); choose values that
        # land exactly on half a cent: 5 Wh at 0.01/kWh = 0.0005 still below.
        # Use per_wh pricing to hit 0.005 exactly.
        tariff = {
            "tariff_id": "PW", "rate_unit": "per_wh",
            "weekday_rules": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                {"label": "X", "start": "00:00", "end": "24:00",
                 "rate": "0.005"}]}],
        }
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-14T01:00:00-04:00", 1),
        ]
        request = fx.period("H", "M1", "PW",
                            "2024-03-14T00:00:00-04:00",
                            "2024-03-14T01:00:00-04:00")
        result = settle(fx.meters(), readings, [tariff], [request])
        bill = bill_by_id(result, "H")
        # Exact 0.005 half-up -> 0.01.
        self.assertEqual(bill["details"][0]["amount_precise"], "0.005")
        self.assertEqual(bill["details"][0]["amount"], "0.01")
        self.assertEqual(bill["total_amount"], "0.01")
        # And the independent Decimal computation agrees.
        self.assertEqual(Decimal("0.005").quantize(Decimal("0.01"),
                                                   ROUND_HALF_UP),
                         Decimal("0.01"))

    def test_quarter_cent_goes_down(self):
        tariff = {
            "tariff_id": "PW2", "rate_unit": "per_wh",
            "weekday_rules": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                {"label": "X", "start": "00:00", "end": "24:00",
                 "rate": "0.0025"}]}],
        }
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M1", "2024-03-14T01:00:00-04:00", 1),
        ]
        request = fx.period("H2", "M1", "PW2",
                            "2024-03-14T00:00:00-04:00",
                            "2024-03-14T01:00:00-04:00")
        result = settle(fx.meters(), readings, [tariff], [request])
        bill = bill_by_id(result, "H2")
        self.assertEqual(bill["details"][0]["amount_precise"], "0.0025")
        self.assertEqual(bill["details"][0]["amount"], "0.00")


if __name__ == "__main__":
    unittest.main()
