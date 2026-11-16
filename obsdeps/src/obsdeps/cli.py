"""Command line interface: ``register``, ``resolve``, ``explain``, ``compare``.

Results are JSON on stdout.  Exit codes:

* 0 -- command succeeded and the resolution status is ``resolved``/``partial``
* 2 -- the command ran but the resolution is ``blocked`` or ``invalid``
* 1 -- usage / input-manifest error (nothing was registered)

``--json`` is accepted for symmetry; output is always JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from .api import Api
from .billing.cli import add_billing_parser, run_billing
from .errors import Problem, Problematic, UsageError
from .manifest import ManifestError
from .proxyvote.cli import add_proxyvote_parser, run_proxyvote
from .stations.cli import add_stations_parser, run_stations


def _chain_argument(value: str) -> List[Dict[str, str]]:
    """Parse ``repo=obs,repo2=obs2`` or a JSON array."""
    text = value.strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"chain JSON is invalid: {exc}")
        return data
    chain = []
    for index, part in enumerate(text.split(",")):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise argparse.ArgumentTypeError(
                f"chain entry {index} must look like repo_id=observation_id (got {part!r})"
            )
        repo_id, observation_id = part.split("=", 1)
        if not repo_id.strip() or not observation_id.strip():
            raise argparse.ArgumentTypeError(f"chain entry {index} has an empty side")
        chain.append({"repo_id": repo_id.strip(), "observation_id": observation_id.strip()})
    if not chain:
        raise argparse.ArgumentTypeError("chain must contain at least one repository")
    return chain


def _coordinate_argument(value: str) -> Dict[str, str]:
    if value.strip().startswith("{"):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"coordinate JSON is invalid: {exc}")
        return data
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "coordinate must be groupId:artifactId:version or a JSON object"
        )
    return {"groupId": parts[0], "artifactId": parts[1], "version": parts[2]}


def _dependency_argument(value: str) -> Dict[str, str]:
    if value.strip().startswith("{"):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"dependency JSON is invalid: {exc}")
        return data
    parts = value.split(":")
    if len(parts) not in (2, 4):
        raise argparse.ArgumentTypeError(
            "dependency must be groupId:artifactId or groupId:artifactId:type:classifier"
        )
    if len(parts) == 2:
        return {"groupId": parts[0], "artifactId": parts[1]}
    return {"groupId": parts[0], "artifactId": parts[1], "type": parts[2], "classifier": parts[3]}


def _observation_argument(value: str) -> Dict[str, str]:
    if "=" in value:
        repo_id, observation_id = value.split("=", 1)
    elif ":" in value:
        repo_id, observation_id = value.split(":", 1)
    else:
        raise argparse.ArgumentTypeError("observation must look like repo_id=observation_id")
    return {"repo_id": repo_id.strip(), "observation_id": observation_id.strip()}


def _request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--chain", required=True, type=_chain_argument,
                        help="ordered repository chain, e.g. R1=obs-a,R2=obs-b or JSON")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--coordinate", type=_coordinate_argument,
                       help="subject coordinate groupId:artifactId:version")
    group.add_argument("--pom", help="explicit subject POM path inside --observation")
    parser.add_argument("--observation", type=_observation_argument,
                        help="observation holding an explicit --pom (repo=obs)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsdeps",
        description="Explain Maven dependency resolution inside explicit observation contexts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register one observation manifest")
    register.add_argument("--db", required=True, help="SQLite database path")
    register.add_argument("--manifest", required=True, help="manifest JSON file")

    resolve = sub.add_parser("resolve", help="resolve one explicit context")
    _request_arguments(resolve)

    explain = sub.add_parser("explain", help="resolve and narrate one dependency")
    _request_arguments(explain)
    explain.add_argument("--dependency", type=_dependency_argument,
                         help="groupId:artifactId[:type:classifier] to explain")

    compare = sub.add_parser("compare", help="compare two explicit contexts")
    compare.add_argument("--db", required=True)
    compare.add_argument("--a-chain", required=True, type=_chain_argument)
    compare.add_argument("--b-chain", required=True, type=_chain_argument)
    group_a = compare.add_mutually_exclusive_group(required=True)
    group_a.add_argument("--a-coordinate", type=_coordinate_argument)
    group_a.add_argument("--a-pom")
    compare.add_argument("--a-observation", type=_observation_argument)
    group_b = compare.add_mutually_exclusive_group(required=True)
    group_b.add_argument("--b-coordinate", type=_coordinate_argument)
    group_b.add_argument("--b-pom")
    compare.add_argument("--b-observation", type=_observation_argument)

    observations = sub.add_parser("observations", help="list registered observations")
    observations.add_argument("--db", required=True)

    files = sub.add_parser("files", help="list the file digest manifest of one observation")
    files.add_argument("--db", required=True)
    files.add_argument("--observation", required=True, type=_observation_argument)

    add_billing_parser(sub)
    add_stations_parser(sub)
    add_proxyvote_parser(sub)
    return parser


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
    sys.stdout.write("\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "billing":
        return run_billing(args)

    if args.command == "stations":
        return run_stations(args)

    if args.command == "proxyvote":
        return run_proxyvote(args)

    try:
        if args.command == "observations":
            with Api(args.db) as api:
                _emit(api.observations())
            return 0
        if args.command == "files":
            with Api(args.db) as api:
                _emit(api.files(args.observation["repo_id"], args.observation["observation_id"]))
            return 0
        if args.command == "register":
            with Api(args.db) as api:
                outcome = api.register_manifest(args.manifest)
            _emit(outcome)
            return 0
        if args.command == "resolve":
            _require_pom_observation(args)
            with Api(args.db) as api:
                result = api.resolve(
                    chain=args.chain,
                    coordinate=args.coordinate,
                    observation=args.observation,
                    pom=args.pom,
                )
            _emit(result)
            return 0 if result["status"] in ("resolved", "partial") else 2
        if args.command == "explain":
            _require_pom_observation(args)
            with Api(args.db) as api:
                result = api.explain(
                    chain=args.chain,
                    dependency=args.dependency,
                    coordinate=args.coordinate,
                    observation=args.observation,
                    pom=args.pom,
                )
            _emit(result)
            return 0 if result["status"] in ("resolved", "partial") else 2
        if args.command == "compare":
            _require_pom_observation(args, prefix="a_")
            _require_pom_observation(args, prefix="b_")
            with Api(args.db) as api:
                result = api.compare(
                    {
                        "chain": args.a_chain,
                        "coordinate": args.a_coordinate,
                        "observation": args.a_observation,
                        "pom": args.a_pom,
                    },
                    {
                        "chain": args.b_chain,
                        "coordinate": args.b_coordinate,
                        "observation": args.b_observation,
                        "pom": args.b_pom,
                    },
                )
            _emit(result)
            return 0
    except ManifestError as exc:
        _emit({"error": "manifest", "problems": [p.to_dict() for p in exc.problems]})
        return 1
    except Problematic as exc:
        _emit({"error": "problem", "problem": exc.problem.to_dict()})
        return 1
    except UsageError as exc:
        _emit({"error": "usage", "message": str(exc)})
        return 1
    raise AssertionError("unreachable")


def _require_pom_observation(args: argparse.Namespace, prefix: str = "") -> None:
    pom = getattr(args, f"{prefix}pom")
    observation = getattr(args, f"{prefix}observation")
    if pom is not None and observation is None:
        raise UsageError("--pom needs --observation (repo=obs) to identify the file")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
