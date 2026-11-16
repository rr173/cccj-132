"""Validation and rejection tests.

Every failure mode that must never produce a plausible-looking bill is
exercised here.  Rejections name the meter/period and carry a stable problem
code; independent meters/periods still settle in the same run.
"""

from __future__ import annotations

import unittest

from support import make_api  # noqa: F401  (import path bootstrap only)
from obsdeps.billing import BillingApi, BillingInputError

import billing_fixtures as fx


def settle(meters, readings, tariffs, periods):
    with BillingApi() as api:
        return api.settle_documents(
            meters=meters, readings=readings, tariffs=tariffs, periods=periods)


def codes(result, period_id):
    rejection = next(r for r in result["rejected"]
                     if r["period_id"] == period_id)
    return sorted(p["code"] for p in rejection["problems"])


def good_period(period_id="P", tariff_id="FLAT"):
    return fx.period(period_id, "M1", tariff_id,
                     "2024-03-14T00:00:00-04:00",
                     "2024-03-14T03:00:00-04:00")


def good_readings():
    return [
        fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
        fx.reading("M1", "2024-03-14T03:00:00-04:00", 300),
    ]


class MissingBoundaryReadings(unittest.TestCase):
    def test_no_reading_before_start(self):
        result = settle(
            fx.meters(),
            [fx.reading("M1", "2024-03-14T01:00:00-04:00", 0),
             fx.reading("M1", "2024-03-14T03:00:00-04:00", 300)],
            [fx.TARIFF_FLAT], [good_period()])
        self.assertEqual(result["summary"],
                         {"periods": 1, "settled": 0, "rejected": 1})
        self.assertIn("missing-boundary-reading", codes(result, "P"))

    def test_no_reading_after_end(self):
        result = settle(
            fx.meters(),
            [fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
             fx.reading("M1", "2024-03-14T02:00:00-04:00", 200)],
            [fx.TARIFF_FLAT], [good_period()])
        self.assertIn("missing-boundary-reading", codes(result, "P"))

    def test_reading_exactly_on_boundary_counts(self):
        # Readings at exactly start and end are valid coverage.
        result = settle(fx.meters(), good_readings(),
                        [fx.TARIFF_FLAT], [good_period()])
        self.assertEqual(result["summary"]["rejected"], 0)
        self.assertEqual(result["bills"][0]["total_wh"], "300.000000000")


class RuleGapsAndOverlaps(unittest.TestCase):
    def test_gap_in_day_coverage_rejects(self):
        gappy = [{
            "tariff_id": "GAP", "rate_unit": "per_kwh",
            "weekday_rules": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                {"label": "A", "start": "00:00", "end": "07:00", "rate": "0.1"},
                {"label": "B", "start": "08:00", "end": "24:00", "rate": "0.2"},
            ]}],
        }]
        result = settle(fx.meters(), good_readings(), gappy, [good_period(tariff_id="GAP")])
        self.assertIn("tariff-gap", codes(result, "P"))
        self.assertEqual(result["bills"], [])

    def test_overlapping_rules_reject(self):
        overlap = [{
            "tariff_id": "OV", "rate_unit": "per_kwh",
            "weekday_rules": [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                {"label": "A", "start": "00:00", "end": "08:00", "rate": "0.1"},
                {"label": "B", "start": "07:00", "end": "24:00", "rate": "0.2"},
            ]}],
        }]
        result = settle(fx.meters(), good_readings(), overlap,
                        [good_period(tariff_id="OV")])
        self.assertIn("tariff-overlap", codes(result, "P"))

    def test_missing_weekday_rejects(self):
        partial = [{
            "tariff_id": "PART", "rate_unit": "per_kwh",
            # Weekday 2024-03-14 is Thursday (4); only weekends defined.
            "weekday_rules": [{"weekdays": [6, 7], "rules": [
                {"label": "A", "start": "00:00", "end": "24:00", "rate": "0.1"},
            ]}],
        }]
        result = settle(fx.meters(), good_readings(), partial,
                        [good_period(tariff_id="PART")])
        self.assertIn("tariff-day-uncovered", codes(result, "P"))


class OffsetMismatch(unittest.TestCase):
    def test_reading_with_wrong_offset_rejects(self):
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 0),
            # -03:00 is never the NY offset.
            fx.reading("M1", "2024-03-14T03:00:00-03:00", 300),
        ]
        result = settle(fx.meters(), readings, [fx.TARIFF_FLAT], [good_period()])
        self.assertIn("offset-mismatch", codes(result, "P"))

    def test_period_boundary_wrong_offset_rejects(self):
        request = fx.period("P", "M1", "FLAT",
                            "2024-03-14T00:00:00-04:00",
                            "2024-03-14T03:00:00-03:00")
        result = settle(fx.meters(), good_readings(), [fx.TARIFF_FLAT], [request])
        self.assertIn("offset-mismatch", codes(result, "P"))

    def test_offset_that_is_right_at_that_instant_accepted(self):
        # In January NY is EST (-05:00); the same string would fail in summer.
        readings = [
            fx.reading("M1", "2024-01-14T00:00:00-05:00", 0),
            fx.reading("M1", "2024-01-14T03:00:00-05:00", 300),
        ]
        request = fx.period("P", "M1", "FLAT",
                            "2024-01-14T00:00:00-05:00",
                            "2024-01-14T03:00:00-05:00")
        result = settle(fx.meters(), readings, [fx.TARIFF_FLAT], [request])
        self.assertEqual(result["summary"]["rejected"], 0)


class DuplicateReadings(unittest.TestCase):
    def test_equal_duplicates_merge(self):
        readings = good_readings() + [
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 300)]
        result = settle(fx.meters(), readings, [fx.TARIFF_FLAT], [good_period()])
        self.assertEqual(result["summary"]["rejected"], 0)
        bill = result["bills"][0]
        self.assertEqual(len(bill["participating_readings"]), 2)

    def test_conflicting_duplicates_reject_without_picking(self):
        readings = good_readings() + [
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 999)]
        result = settle(fx.meters(), readings, [fx.TARIFF_FLAT], [good_period()])
        self.assertIn("duplicate-reading-conflict", codes(result, "P"))
        rejection = next(r for r in result["rejected"] if r["period_id"] == "P")
        detail = next(p for p in rejection["problems"]
                      if p["code"] == "duplicate-reading-conflict")
        # Both conflicting values are reported, not one silently chosen.
        self.assertEqual(set(detail["details"]["values"]), {"300", "999"})


class MonotonicityAndRollover(unittest.TestCase):
    def test_unexplained_decrease_rejects(self):
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 300),
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 100),
        ]
        result = settle(fx.meters(), readings, [fx.TARIFF_FLAT], [good_period()])
        self.assertIn("reading-rollback", codes(result, "P"))

    def test_decrease_with_modulus_but_value_out_of_range_rejects(self):
        # modulus 250: the earlier value 300 is not < 250, so no legal wrap.
        meter = fx.meters([("M1", 250)])
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 300),
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 100),
        ]
        result = settle(meter, readings, [fx.TARIFF_FLAT], [good_period()])
        self.assertIn("reading-rollback", codes(result, "P"))

    def test_legal_wrap_is_accepted_and_second_period_still_works(self):
        meter = fx.meters([("M1", 1000)])
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00-04:00", 900),
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 50),
            fx.reading("M1", "2024-03-14T06:00:00-04:00", 200),
        ]
        p1 = fx.period("P1", "M1", "FLAT",
                       "2024-03-14T00:00:00-04:00",
                       "2024-03-14T03:00:00-04:00")
        p2 = fx.period("P2", "M1", "FLAT",
                       "2024-03-14T03:00:00-04:00",
                       "2024-03-14T06:00:00-04:00")
        result = settle(meter, readings, [fx.TARIFF_FLAT], [p1, p2])
        self.assertEqual(result["summary"],
                         {"periods": 2, "settled": 2, "rejected": 0})
        totals = {b["period_id"]: b["total_wh"] for b in result["bills"]}
        # Wrap pair 900->50 = 150 Wh; normal pair 50->200 = 150 Wh.
        self.assertEqual(totals, {"P1": "150.000000000",
                                  "P2": "150.000000000"})


class UnknownEntitiesAndMalformedInput(unittest.TestCase):
    def test_unknown_meter_rejects_only_that_period(self):
        request = fx.period("P", "GHOST", "FLAT",
                            "2024-03-14T00:00:00-04:00",
                            "2024-03-14T03:00:00-04:00")
        result = settle(fx.meters(), good_readings(),
                        [fx.TARIFF_FLAT], [request])
        self.assertIn("meter-unknown", codes(result, "P"))

    def test_unknown_tariff_rejects(self):
        result = settle(fx.meters(), good_readings(), [fx.TARIFF_FLAT],
                        [good_period(tariff_id="MISSING")])
        self.assertIn("tariff-unknown", codes(result, "P"))

    def test_unknown_timezone_rejects_meter(self):
        result = settle(
            [{"meter_id": "M1", "timezone": "Mars/Olympus"}],
            good_readings(), [fx.TARIFF_FLAT], [good_period()])
        self.assertIn("meter-unknown", codes(result, "P"))

    def test_unparseable_document_raises_input_error(self):
        with BillingApi() as api:
            with self.assertRaises(BillingInputError):
                api.settle_documents(
                    meters=[{"meter_id": "M1", "timezone": "America/New_York"}],
                    readings="not-json{",
                    tariffs=[fx.TARIFF_FLAT], periods=[good_period()])

    def test_malformed_reading_timestamp_rejects_meter_period(self):
        readings = [
            fx.reading("M1", "2024-03-14T00:00:00", 0),  # no offset
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 300),
        ]
        result = settle(fx.meters(), readings, [fx.TARIFF_FLAT], [good_period()])
        self.assertTrue(any(c in {"reading-timestamp-malformed",
                                  "missing-boundary-reading"}
                            for c in codes(result, "P")))


class IndependenceAndDeterminism(unittest.TestCase):
    def test_bad_meter_does_not_erase_good_meter_bill(self):
        meters = fx.meters([("M1", None), ("M2", None)])
        readings = good_readings() + [
            # Conflicting duplicate makes M1 invalid.
            fx.reading("M1", "2024-03-14T03:00:00-04:00", 7),
            fx.reading("M2", "2024-03-14T00:00:00-04:00", 0),
            fx.reading("M2", "2024-03-14T03:00:00-04:00", 999),
        ]
        periods = [
            fx.period("P1", "M1", "FLAT",
                      "2024-03-14T00:00:00-04:00",
                      "2024-03-14T03:00:00-04:00"),
            fx.period("P2", "M2", "FLAT",
                      "2024-03-14T00:00:00-04:00",
                      "2024-03-14T03:00:00-04:00"),
        ]
        result = settle(meters, readings, [fx.TARIFF_FLAT], periods)
        self.assertEqual(result["summary"],
                         {"periods": 2, "settled": 1, "rejected": 1})
        self.assertEqual([b["period_id"] for b in result["bills"]], ["P2"])
        self.assertEqual([r["period_id"] for r in result["rejected"]], ["P1"])
        good = result["bills"][0]
        self.assertEqual(good["total_wh"], "999.000000000")

    def test_repeated_runs_are_byte_identical(self):
        import json
        first = settle(fx.meters(), good_readings(),
                       [fx.TARIFF_FLAT], [good_period()])
        second = settle(fx.meters(), good_readings(),
                        [fx.TARIFF_FLAT], [good_period()])
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_input_order_does_not_change_output(self):
        base = settle(fx.meters(), good_readings(),
                      [fx.TARIFF_FLAT], [good_period()])
        shuffled = settle(
            fx.meters(), list(reversed(good_readings())),
            [fx.TARIFF_FLAT], [good_period()])
        self.assertEqual(base["bills"][0]["details"],
                         shuffled["bills"][0]["details"])
        self.assertEqual(base["bills"][0]["total_wh"],
                         shuffled["bills"][0]["total_wh"])


if __name__ == "__main__":
    unittest.main()
