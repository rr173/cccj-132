#!/usr/bin/env python3
"""Offline settlement worked example with hand-written expectations.

The expected numbers in ``billing_expected.json`` were computed on paper from
the input documents (see the paper trail in each scenario's docstring of
``tests/test_billing_handworked.py``), never produced by the engine, so
agreement is an independent check.

Usage::

    python3 examples/run_billing_example.py [--keep]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from obsdeps.billing import BillingApi  # noqa: E402

# Reuse the fixture tariffs/readings shape but keep this script standalone.
NY = "America/New_York"


def build_documents() -> dict:
    meters = [
        {"meter_id": "M1", "timezone": NY},
        {"meter_id": "M2", "timezone": NY, "modulus": 1000},
    ]
    tariffs = [
        {
            "tariff_id": "TOU",
            "rate_unit": "per_kwh",
            "weekday_rules": [
                {"weekdays": [1, 2, 3, 4, 5], "rules": [
                    {"label": "OFF", "start": "00:00", "end": "07:00",
                     "rate": "0.10"},
                    {"label": "PEAK", "start": "07:00", "end": "24:00",
                     "rate": "0.20"},
                ]},
                {"weekdays": [6, 7], "rules": [
                    {"label": "WEEKEND", "start": "00:00", "end": "24:00",
                     "rate": "0.06"},
                ]},
            ],
            "day_exceptions": [
                {"date": "2024-03-15", "label": "HOLIDAY", "rate": "0.05"},
            ],
        },
        {
            "tariff_id": "FLAT",
            "rate_unit": "per_kwh",
            "weekday_rules": [
                {"weekdays": [1, 2, 3, 4, 5, 6, 7], "rules": [
                    {"label": "FLAT", "start": "00:00", "end": "24:00",
                     "rate": "0.20"},
                ]},
            ],
        },
    ]
    readings = [
        # M1: constant 500 Wh/h over Thu 2024-03-14.
        {"meter_id": "M1", "timestamp": "2024-03-14T00:00:00-04:00",
         "watt_hours": 0},
        {"meter_id": "M1", "timestamp": "2024-03-14T12:00:00-04:00",
         "watt_hours": 6000},
        {"meter_id": "M1", "timestamp": "2024-03-15T00:00:00-04:00",
         "watt_hours": 12000},
        # M2: one modulus roll-over (900 -> 50 = 150 Wh).
        {"meter_id": "M2", "timestamp": "2024-03-14T00:00:00-04:00",
         "watt_hours": 900},
        {"meter_id": "M2", "timestamp": "2024-03-14T03:00:00-04:00",
         "watt_hours": 50},
    ]
    periods = [
        # M1 [Thu 06:00, Fri 00:00): OFF 500 Wh, PEAK 8500 Wh.
        {"period_id": "P-TOU", "meter_id": "M1", "tariff_id": "TOU",
         "start": "2024-03-14T06:00:00-04:00",
         "end": "2024-03-15T00:00:00-04:00"},
        # M2 roll-over period, flat rate.
        {"period_id": "P-ROLL", "meter_id": "M2", "tariff_id": "FLAT",
         "start": "2024-03-14T00:00:00-04:00",
         "end": "2024-03-14T03:00:00-04:00"},
    ]
    return {"meters": meters, "readings": readings,
            "tariffs": tariffs, "periods": periods}


# Hand-written expectations (see module docstring).  Rates per kWh are divided
# by 1000 here just as the engine does, and money is rounded half-up.
EXPECTED = {
    "P-TOU": {
        "total_wh": "9000.000000000",
        "total_amount": "1.75",
        "lines": {
            "OFF": {"watt_hours": "500.000000000", "amount": "0.05"},
            "PEAK": {"watt_hours": "8500.000000000", "amount": "1.70"},
        },
        "paper": {
            "start_wh": 3000, "end_wh": 12000,
            "off_wh": 1 * 500, "peak_wh": 17 * 500,
        },
    },
    "P-ROLL": {
        "total_wh": "150.000000000",
        "total_amount": "0.03",
        "lines": {
            "FLAT": {"watt_hours": "150.000000000", "amount": "0.03"},
        },
        "paper": {"physical_wh": 50 - 900 + 1000},
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", default=None,
                        help="directory to write the four input JSON files into")
    args = parser.parse_args()

    directory = args.bundle or tempfile.mkdtemp(prefix="billing-example-")
    documents = build_documents()
    paths = {}
    for name, document in documents.items():
        path = os.path.join(directory, f"{name}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        paths[name] = path

    digests_before = {name: hashlib.sha256(open(path, "rb").read()).hexdigest()
                      for name, path in paths.items()}

    with BillingApi() as api:
        result = api.settle_files(**paths)

    failures = []
    if result["summary"]["rejected"] != 0:
        failures.append(f"unexpected rejections: {result['rejected']}")

    bills = {bill["period_id"]: bill for bill in result["bills"]}
    for period_id, want in EXPECTED.items():
        bill = bills.get(period_id)
        if bill is None:
            failures.append(f"{period_id}: missing bill")
            continue
        if bill["total_wh"] != want["total_wh"]:
            failures.append(f"{period_id}: total_wh {bill['total_wh']} != "
                            f"{want['total_wh']}")
        if bill["total_amount"] != want["total_amount"]:
            failures.append(f"{period_id}: total_amount {bill['total_amount']} "
                            f"!= {want['total_amount']}")
        lines = {line["label"]: line for line in bill["details"]}
        for label, expected_line in want["lines"].items():
            got = lines.get(label)
            if got is None:
                failures.append(f"{period_id}: missing line {label}")
                continue
            for field, value in expected_line.items():
                if got[field] != value:
                    failures.append(
                        f"{period_id} {label}.{field}: {got[field]} != {value}")
        # The detail lines must reconcile exactly to the boundary usage.
        from decimal import Decimal
        summed = sum((Decimal(line["watt_hours"]) for line in bill["details"]),
                     Decimal(0))
        if summed != Decimal(bill["total_wh"]):
            failures.append(f"{period_id}: detail usages {summed} do not sum "
                            f"to total {bill['total_wh']}")

    # Re-run must be identical and input files untouched.
    with BillingApi() as api:
        again = api.settle_files(**paths)
    if json.dumps(again, sort_keys=True) != json.dumps(result, sort_keys=True):
        failures.append("second run differs from the first run")
    digests_after = {name: hashlib.sha256(open(path, "rb").read()).hexdigest()
                     for name, path in paths.items()}
    if digests_after != digests_before:
        failures.append("input files were modified during settlement")

    if failures:
        print("FAILED")
        for failure in failures:
            print(" -", failure)
        return 1

    print("billing worked example matches hand-written expectations")
    print(json.dumps({
        pid: {"total_wh": bill["total_wh"],
              "total_amount": bill["total_amount"],
              "lines": {d["label"]: {"watt_hours": d["watt_hours"],
                                     "amount": d["amount"]}
                        for d in bill["details"]}}
        for pid, bill in sorted(bills.items())
    }, indent=2))
    print(f"\ninput files written in: {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
