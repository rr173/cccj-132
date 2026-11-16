"""Command line for the charging-station directory feature.

Sub-commands under ``stations``:

* ``stations collect``  -- page one or more provider feeds into SQLite
  (``--mock-fixture`` swaps the HTTP transport for a fully local interface);
* ``stations list``     -- current state of every station, with filters;
* ``stations show``     -- one station's current state and its observations;
* ``stations canonical``-- the merged cross-network group for one canonical id;
* ``stations runs``     -- collection runs and their completeness;
* ``stations run``      -- full detail, pages and problems of one run;
* ``stations problems`` -- rejected records, page failures, canonical conflicts;
* ``stations sources``  -- the providers known to the database.

Exit codes: ``0`` success (including runs where every source completed);
``2`` the command ran but at least one source was incomplete or any record was
rejected; ``1`` usage/configuration error before collection.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..errors import Problematic
from .api import StationApi
from .errors import StationConfigError


def add_stations_parser(sub: argparse._SubParsersAction) -> None:
    stations = sub.add_parser(
        "stations",
        help="repeatable charging-station directory collection",
        description="Collect cursor-paged station feeds and keep an evidenced "
                    "current-state view in SQLite.",
    )
    station_sub = stations.add_subparsers(
        dest="stations_command", required=True)

    collect = station_sub.add_parser(
        "collect", help="collect one or more provider feeds")
    collect.add_argument("--db", required=True, help="SQLite database path")
    collect.add_argument("--sources", required=True,
                         help="source configuration JSON file")
    collect.add_argument("--mock-fixture", default=None,
                         help="local mock interface fixture (replaces HTTP)")
    collect.add_argument("--run-id", default=None,
                         help="override the content-derived run id")
    collect.add_argument("--started-at", default=None,
                         help="override the run timestamp (deterministic runs)")
    collect.add_argument("--max-pages", type=int, default=None,
                         help="safety cap on pages per source")

    list_cmd = station_sub.add_parser(
        "list", help="list current station states")
    list_cmd.add_argument("--db", required=True)
    list_cmd.add_argument("--provider", default=None)
    list_cmd.add_argument("--status", default=None)

    show = station_sub.add_parser(
        "show", help="show one station with its observation evidence")
    show.add_argument("--db", required=True)
    show.add_argument("--provider", required=True)
    show.add_argument("--station", required=True)

    canonical = station_sub.add_parser(
        "canonical", help="show the merged group for one canonical id")
    canonical.add_argument("--db", required=True)
    canonical.add_argument("--canonical-id", required=True)

    runs = station_sub.add_parser("runs", help="list collection runs")
    runs.add_argument("--db", required=True)

    run = station_sub.add_parser("run", help="detail of one collection run")
    run.add_argument("--db", required=True)
    run.add_argument("--run-id", required=True)

    problems = station_sub.add_parser(
        "problems", help="list record, page and canonical-id problems")
    problems.add_argument("--db", required=True)
    problems.add_argument("--run-id", default=None)

    sources = station_sub.add_parser(
        "sources", help="list providers known to the database")
    sources.add_argument("--db", required=True)


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
    sys.stdout.write("\n")


def run_stations(args: argparse.Namespace) -> int:
    command = args.stations_command
    try:
        if command == "collect":
            with StationApi(args.db) as api:
                summary = api.collect_from_file(
                    args.sources, mock_fixture=args.mock_fixture,
                    run_id=args.run_id, started_at=args.started_at,
                    max_pages=args.max_pages)
            _emit(summary)
            return (2 if summary["incomplete"]
                    or summary["records_rejected"] else 0)

        with StationApi(args.db) as api:
            if command == "list":
                _emit(api.list_stations(provider_id=args.provider,
                                        status=args.status))
                return 0
            if command == "show":
                document = api.get_station(args.provider, args.station)
                if document is None:
                    _emit({"error": "not-found",
                           "provider_id": args.provider,
                           "station_id": args.station})
                    return 1
                _emit(document)
                return 0
            if command == "canonical":
                document = api.get_canonical_group(args.canonical_id)
                if document is None:
                    _emit({"error": "not-found",
                           "canonical_id": args.canonical_id})
                    return 1
                _emit(document)
                return 0
            if command == "runs":
                _emit(api.list_runs())
                return 0
            if command == "run":
                document = api.get_run_detail(args.run_id)
                if document is None:
                    _emit({"error": "not-found", "run_id": args.run_id})
                    return 1
                _emit(document)
                return (2 if document["incomplete_count"]
                        or any(s["records_rejected"]
                               for s in document["sources"]) else 0)
            if command == "problems":
                _emit(api.list_problems(run_id=args.run_id))
                return 0
            if command == "sources":
                _emit(api.list_sources())
                return 0
    except StationConfigError as exc:
        _emit({"error": "station-config", "message": exc.message,
               "source": exc.source})
        return 1
    except Problematic as exc:
        _emit({"error": "problem", "problem": exc.problem.to_dict()})
        return 1
    raise AssertionError("unreachable stations command")
