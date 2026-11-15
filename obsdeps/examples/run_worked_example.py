#!/usr/bin/env python3
"""Run the worked example and compare with hand-written expectations.

The expectations in ``expected.json`` were written by hand from the bundle
contents, not generated from the resolver, so agreement is meaningful.

Usage::

    python3 examples/run_worked_example.py [--keep]
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

from obsdeps import Api  # noqa: E402

# Running with -I keeps the script directory off sys.path on purpose; add the
# example directory only for the sibling input builder module.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from build_bundle import build  # noqa: E402

HERE_ABS = HERE

CONTEXT_A = {
    "chain": [{"repo_id": "R1", "observation_id": "obs-child-only"}],
    "coordinate": {"groupId": "com.acme", "artifactId": "shop", "version": "1.0"},
}
CONTEXT_B = {
    "chain": [{"repo_id": "R1", "observation_id": "obs-later"},
              {"repo_id": "R1", "observation_id": "obs-child-only"}],
    "coordinate": {"groupId": "com.acme", "artifactId": "shop", "version": "1.0"},
}
CONTEXT_C = {
    "chain": [{"repo_id": "R0", "observation_id": "obs-alt-parent"},
              {"repo_id": "R1", "observation_id": "obs-later"},
              {"repo_id": "R1", "observation_id": "obs-child-only"}],
    "coordinate": {"groupId": "com.acme", "artifactId": "shop", "version": "1.0"},
}
CONTEXT_SNAPSHOT = {
    "chain": [{"repo_id": "R1", "observation_id": "obs-snapshot-app"},
              {"repo_id": "R1", "observation_id": "obs-snapshot"}],
    "coordinate": {"groupId": "com.acme", "artifactId": "snapshot-app", "version": "1.0"},
}


def summarise(result):
    return {
        "status": result["status"],
        "versions": {
            entry["key"]["key"]: entry["version"]["resolved"]
            for entry in result["dependencies"]
        },
        "missing_codes": sorted({m["code"] for m in result["missing"]}),
        "problem_codes": sorted({p["code"] for p in result["problems"]}),
    }


def snapshot_files(result):
    out = {}
    for entry in result["dependencies"]:
        key = entry["key"]["key"]
        out[key] = {
            item["role"]: {
                "path": item.get("path"),
                "observed": item.get("observed"),
                "sha256": item.get("sha256"),
                "snapshot_value": (item.get("snapshot") or {}).get("value"),
            }
            for item in entry.get("files", [])
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", default=None, help="directory to build input manifests in")
    parser.add_argument("--expectations",
                        default=os.path.join(HERE, "expected.json"))
    args = parser.parse_args()

    bundle_dir = args.bundle or tempfile.mkdtemp(prefix="obsdeps-example-")
    manifests = build(bundle_dir)

    with open(args.expectations, "r", encoding="utf-8") as handle:
        expected = json.load(handle)

    api = Api(":memory:")
    for path in manifests:
        api.register_manifest(path)

    results = {
        "A": api.resolve(**CONTEXT_A),
        "B": api.resolve(**CONTEXT_B),
        "C": api.resolve(**CONTEXT_C),
        "snapshot": api.resolve(**CONTEXT_SNAPSHOT),
        "A_again": api.resolve(**CONTEXT_A),
        "B_again": api.resolve(**CONTEXT_B),
    }

    failures = []

    for name in ("A", "B", "C"):
        got = summarise(results[name])
        want = expected[name]
        if got["status"] != want["status"]:
            failures.append(f"{name}: status {got['status']} != {want['status']}")
        if got["versions"] != want["versions"]:
            failures.append(f"{name}: versions {got['versions']} != {want['versions']}")
        for code in want.get("missing_codes", []):
            if code not in got["missing_codes"]:
                failures.append(f"{name}: missing code {code} absent from {got['missing_codes']}")

    # Re-query stability.
    if summarise(results["A_again"]) != summarise(results["A"]):
        failures.append("A_again differs from A")
    if summarise(results["B_again"]) != summarise(results["B"]):
        failures.append("B_again differs from B")

    # Parent edge selection in C must be R0.
    edge = results["C"]["parent_edges"][0]
    if edge["selected"]["repo_id"] != expected["C"]["parent_repo"]:
        failures.append(
            f"C: parent selected in {edge['selected']['repo_id']} != {expected['C']['parent_repo']}"
        )

    # Explain view: changing the parent must name R0 as the source of the
    # telemetry version, and must point at the property that was NOT overridden.
    explanation = api.explain(
        **CONTEXT_C, dependency={"groupId": "com.acme", "artifactId": "telemetry"})
    body = explanation["explanation"]
    if body["final_version"] != expected["C"]["versions"]["com.acme:telemetry:jar:"]:
        failures.append(f"explain: telemetry final version {body['final_version']} unexpected")
    managed_step = next(
        (step for step in body["steps"] if "effective dependencyManagement" in step["fact"]),
        None,
    )
    if managed_step is None:
        failures.append("explain: no management step for telemetry")
    else:
        if "obs-alt-parent" not in (managed_step.get("managed_from") or {}).get("locator", ""):
            failures.append("explain: managed_from does not name the R0 parent")
        props = {p["name"]: p for p in managed_step.get("properties", [])}
        telemetry_prop = props.get("telemetry-version")
        if telemetry_prop is None or "obs-alt-parent" not in (
                telemetry_prop.get("defined_in") or {}).get("locator", ""):
            failures.append("explain: telemetry-version is not traced to the R0 parent")
        if "release" in props:
            failures.append("explain: telemetry evidence should not mention the unrelated "
                            "'release' property")

    # Pricing is managed by the child with ${release}: its evidence must point
    # at the child's own override, not at either parent.
    pricing = api.explain(
        **CONTEXT_C, dependency={"groupId": "com.acme", "artifactId": "pricing"})
    pricing_step = next(
        (step for step in pricing["explanation"]["steps"]
         if "effective dependencyManagement" in step["fact"]), None)
    if pricing_step is None:
        failures.append("explain: no management step for pricing")
    else:
        pricing_props = {p["name"]: p for p in pricing_step.get("properties", [])}
        release = pricing_props.get("release") or {}
        if release.get("value") != "2.0":
            failures.append("explain: pricing release is not the child override 2.0")
        if "obs-child-only" not in (release.get("defined_in") or {}).get("locator", ""):
            failures.append("explain: pricing release is not traced to the child POM")

    # Source change between B and C for the telemetry dependency only.
    comparison = api.compare_resolved(results["B"], results["C"])
    changed = sorted(row["key"] for row in comparison["dependency_changes"]
                     if row["classification"] == "version_changed")
    if changed != expected["B_vs_C"]["version_changed"]:
        failures.append(f"B vs C changed {changed} != {expected['B_vs_C']['version_changed']}")
    source_changed = sorted(row["key"] for row in comparison["dependency_changes"]
                            if row["classification"] == "source_changed")
    if source_changed != expected["B_vs_C"].get("source_changed", []):
        failures.append(
            f"B vs C source changes {source_changed} != {expected['B_vs_C'].get('source_changed', [])}"
        )

    # Independent digest check: recompute sha256 from the manifest bytes we
    # wrote, without asking the resolver.
    manifest_digests = {}
    for path in manifests:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        for entry in document["files"]:
            manifest_digests[entry["path"]] = hashlib.sha256(
                entry["content"].encode("utf-8")).hexdigest()

    snap = snapshot_files(results["snapshot"])
    for key, want in expected["snapshot"]["files"].items():
        got = snap.get(key, {})
        for role, want_role in want.items():
            got_role = got.get(role, {})
            for field, value in want_role.items():
                if got_role.get(field) != value:
                    failures.append(f"snapshot {key} {role}.{field}: "
                                    f"{got_role.get(field)!r} != {value!r}")
            reported = got_role.get("sha256")
            expected_digest = manifest_digests.get(got_role.get("path") or "")
            if reported is None or reported != expected_digest:
                failures.append(f"snapshot {key} {role}: reported digest {reported!r} "
                                f"does not match the input bytes ({expected_digest!r})")

    if failures:
        print("FAILED")
        for failure in failures:
            print(" -", failure)
        return 1

    print("worked example matches hand-written expectations")
    print(json.dumps({name: summarise(results[name]) for name in ("A", "B", "C")},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
