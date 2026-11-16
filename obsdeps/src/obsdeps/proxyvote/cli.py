"""Command line for offline proxy-vote reconciliation.

Sub-commands under ``proxyvote``:

* ``reconcile`` -- read four JSONL files for one meeting version, print the
  full result, write nothing;
* ``store``     -- reconcile and store the result immutably in SQLite;
* ``list``      -- list stored reconciliations (newest last);
* ``summary``   -- totals per proposal from the stored document only;
* ``position``  -- one position's totals and segmented evidence;
* ``proposal``  -- one proposal's totals and segmented evidence;
* ``evidence``  -- segments, optionally narrowed by proposal/position.

Exit codes: ``0`` the reconciliation is complete; ``2`` it ran and produced
locatable results but the input was knowingly incomplete (an input problem
quarantined at least one position); ``1`` usage or unreadable-file error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..errors import Problematic
from .api import ProxyVoteApi
from .errors import ProxyVoteInputError, ReconciliationError


def add_proxyvote_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "proxyvote",
        help="offline proxy-vote reconciliation for one shareholder meeting",
        description="Reconcile record-date holdings, proxy authorizations and "
                    "ballots into per-proposal totals and segmented evidence.",
    )
    commands = parser.add_subparsers(dest="proxyvote_command", required=True)

    reconcile = commands.add_parser(
        "reconcile", help="reconcile four local JSONL files without a database")
    _request_arguments(reconcile)
    _document_arguments(reconcile)

    store = commands.add_parser(
        "store", help="reconcile and store the result immutably in SQLite")
    store.add_argument("--db", required=True, help="SQLite database path")
    _request_arguments(store)
    _document_arguments(store)
    store.add_argument("--created-at", default=None,
                       help="override the registration timestamp")

    listing = commands.add_parser(
        "list", help="list stored reconciliations")
    listing.add_argument("--db", required=True)
    listing.add_argument("--meeting-id", default=None)

    summary = commands.add_parser(
        "summary", help="per-proposal totals from the stored result")
    _lookup_arguments(summary)

    position = commands.add_parser(
        "position", help="one position's totals and evidence")
    _lookup_arguments(position)
    position.add_argument("--position-id", required=True)

    proposal = commands.add_parser(
        "proposal", help="one proposal's totals and evidence")
    _lookup_arguments(proposal)
    proposal.add_argument("--proposal-id", required=True)

    evidence = commands.add_parser(
        "evidence", help="segmented evidence, optionally narrowed")
    _lookup_arguments(evidence)
    evidence.add_argument("--proposal-id", default=None)
    evidence.add_argument("--position-id", default=None)


def _request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--meeting-id", required=True,
                        help="the meeting whose version is reconciled")
    parser.add_argument("--record-date", required=True,
                        help="the selected roster version's record date (YYYY-MM-DD)")
    parser.add_argument("--cutoff-at", required=True,
                        help="voting cutoff, RFC 3339 with an offset")


def _document_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--roster", required=True, help="shareholder roster JSONL")
    parser.add_argument("--proposals", required=True,
                        help="proposals and their options JSONL")
    parser.add_argument("--grants", required=True,
                        help="proxy authorizations JSONL")
    parser.add_argument("--ballots", required=True, help="ballots JSONL")


def _lookup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--reconciliation-id", default=None,
                        help="stored result id (default: latest for the meeting)")
    parser.add_argument("--meeting-id", default=None)
    parser.add_argument("--record-date", default=None)
    parser.add_argument("--cutoff-at", default=None)


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
    sys.stdout.write("\n")


def run_proxyvote(args: argparse.Namespace) -> int:
    command = args.proxyvote_command
    try:
        if command == "reconcile":
            with ProxyVoteApi() as api:
                result = api.reconcile_files(
                    meeting_id=args.meeting_id,
                    record_date=args.record_date,
                    cutoff_at=args.cutoff_at,
                    roster=args.roster, proposals=args.proposals,
                    grants=args.grants, ballots=args.ballots)
            _emit(result)
            return 0 if result["complete"] else 2
        if command == "store":
            with ProxyVoteApi(args.db) as api:
                outcome = api.register(
                    meeting_id=args.meeting_id,
                    record_date=args.record_date,
                    cutoff_at=args.cutoff_at,
                    roster=args.roster, proposals=args.proposals,
                    grants=args.grants, ballots=args.ballots,
                    created_at=args.created_at)
            _emit(outcome)
            return 0
        if command == "list":
            with ProxyVoteApi(args.db) as api:
                _emit(api.list_reconciliations(args.meeting_id))
            return 0
        if command == "summary":
            with ProxyVoteApi(args.db) as api:
                result = api.summary(**_lookup(args))
            _emit(result)
            return 0 if result["complete"] else 2
        if command == "position":
            with ProxyVoteApi(args.db) as api:
                _emit(api.position(position_id=args.position_id,
                                   **_lookup(args)))
            return 0
        if command == "proposal":
            with ProxyVoteApi(args.db) as api:
                _emit(api.proposal(proposal_id=args.proposal_id,
                                   **_lookup(args)))
            return 0
        if command == "evidence":
            with ProxyVoteApi(args.db) as api:
                _emit(api.evidence(proposal_id=args.proposal_id,
                                   position_id=args.position_id,
                                   **_lookup(args)))
            return 0
    except ProxyVoteInputError as exc:
        _emit({"error": "proxyvote-input", "message": exc.message,
               "source": exc.source})
        return 1
    except ReconciliationError as exc:
        _emit({"error": "proxyvote-request", **exc.to_dict()})
        return 1
    except Problematic as exc:
        _emit({"error": "problem", "problem": exc.problem.to_dict()})
        return 1
    raise AssertionError("unreachable proxyvote command")


def _lookup(args: argparse.Namespace) -> dict:
    return {
        "reconciliation_id": args.reconciliation_id,
        "meeting_id": args.meeting_id,
        "record_date": args.record_date,
        "cutoff_at": args.cutoff_at,
    }
