"""Command line for the offline time-of-use settlement feature.

Sub-commands under ``billing``:

* ``billing settle``     -- read four JSON files, print the bill/rejection
  document, never write anything;
* ``billing register``   -- store four JSON files immutably in SQLite;
* ``billing registered`` -- settle everything stored in that database.

Exit codes mirror the main CLI: ``0`` every period settled, ``2`` at least one
period rejected (valid bills are still printed), ``1`` usage/file error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..errors import Problematic
from .api import BillingApi
from .errors import BillingInputError


def add_billing_parser(sub: argparse._SubParsersAction) -> None:
    billing = sub.add_parser(
        "billing",
        help="offline time-of-use electricity settlement",
        description="Read local meter readings, meter config and tariff rules "
                    "and produce reviewable settlement bills.",
    )
    billing_sub = billing.add_subparsers(dest="billing_command", required=True)

    settle = billing_sub.add_parser(
        "settle", help="settle from four local JSON files without a database")
    _four_file_arguments(settle)

    register = billing_sub.add_parser(
        "register", help="store four JSON files immutably in SQLite")
    register.add_argument("--db", required=True, help="SQLite database path")
    _four_file_arguments(register)
    register.add_argument("--batch-id", default=None)
    register.add_argument("--note", default=None)
    register.add_argument("--registered-at", default=None,
                          help="override the UTC registration timestamp (deterministic runs)")

    registered = billing_sub.add_parser(
        "registered", help="settle all periods stored in a database")
    registered.add_argument("--db", required=True, help="SQLite database path")


def _four_file_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--meters", required=True, help="meter configuration JSON")
    parser.add_argument("--readings", required=True, help="meter readings JSON")
    parser.add_argument("--tariffs", required=True, help="tariff rules JSON")
    parser.add_argument("--periods", required=True, help="settlement periods JSON")


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
    sys.stdout.write("\n")


def run_billing(args: argparse.Namespace) -> int:
    command = args.billing_command
    try:
        if command == "settle":
            with BillingApi() as api:
                result = api.settle_files(
                    meters=args.meters, readings=args.readings,
                    tariffs=args.tariffs, periods=args.periods,
                )
            _emit(result)
            return 0 if result["summary"]["rejected"] == 0 else 2
        if command == "register":
            with BillingApi(args.db) as api:
                summary = api.register_inputs(
                    meters=args.meters, readings=args.readings,
                    tariffs=args.tariffs, periods=args.periods,
                    batch_id=args.batch_id, note=args.note,
                    registered_at=args.registered_at,
                )
            _emit({"status": "registered", **summary})
            return 0
        if command == "registered":
            with BillingApi(args.db) as api:
                result = api.settle_registered()
            _emit(result)
            return 0 if result["summary"]["rejected"] == 0 else 2
    except BillingInputError as exc:
        _emit({"error": "billing-input", "message": exc.message, "source": exc.source})
        return 1
    except Problematic as exc:
        _emit({"error": "problem", "problem": exc.problem.to_dict()})
        return 1
    raise AssertionError("unreachable billing command")
