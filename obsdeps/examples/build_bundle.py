#!/usr/bin/env python3
"""Build the worked example bundle on disk.

This script only writes input material: manifests, POMs and metadata.  It
contains no resolver logic and no expected values; those live in
``examples/expected.json``, written independently by hand (see README).

Usage::

    python3 examples/build_bundle.py examples/bundle
"""

from __future__ import annotations

import json
import os
import sys


def pom(group, artifact, version, *, parent=None, properties=None,
        dependencies=(), management=(), extra=""):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<project xmlns="http://maven.apache.org/POM/4.0.0">',
             "  <modelVersion>4.0.0</modelVersion>"]
    if parent:
        lines += ["  <parent>",
                  f"    <groupId>{parent[0]}</groupId>",
                  f"    <artifactId>{parent[1]}</artifactId>",
                  f"    <version>{parent[2]}</version>",
                  "  </parent>"]
    if group:
        lines.append(f"  <groupId>{group}</groupId>")
    lines.append(f"  <artifactId>{artifact}</artifactId>")
    if version:
        lines.append(f"  <version>{version}</version>")
    if properties:
        lines.append("  <properties>")
        for name, value in properties.items():
            lines.append(f"    <{name}>{value}</{name}>")
        lines.append("  </properties>")
    if management:
        lines += ["  <dependencyManagement>", "    <dependencies>"]
        for entry in management:
            lines.append("      " + dependency_xml(*entry))
        lines += ["    </dependencies>", "  </dependencyManagement>"]
    if dependencies:
        lines.append("  <dependencies>")
        for entry in dependencies:
            lines.append("    " + dependency_xml(*entry))
        lines.append("  </dependencies>")
    if extra:
        lines.append(extra)
    lines.append("</project>")
    return "\n".join(lines) + "\n"


def dependency_xml(group, artifact, version, dep_type=None, classifier=None):
    parts = ["<dependency>",
             f"  <groupId>{group}</groupId>",
             f"  <artifactId>{artifact}</artifactId>"]
    if version is not None:
        parts.append(f"  <version>{version}</version>")
    if dep_type:
        parts.append(f"  <type>{dep_type}</type>")
    if classifier:
        parts.append(f"  <classifier>{classifier}</classifier>")
    parts.append("</dependency>")
    return "\n      ".join(parts)


def metadata(group, artifact, version, mappings):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<metadata xmlns="http://maven.apache.org/POM/4.0.0">',
             f"  <groupId>{group}</groupId>",
             f"  <artifactId>{artifact}</artifactId>",
             f"  <version>{version}</version>",
             "  <versioning>",
             "    <snapshotVersions>"]
    for extension, classifier, value in mappings:
        lines.append("      <snapshotVersion>")
        lines.append(f"        <extension>{extension}</extension>")
        if classifier:
            lines.append(f"        <classifier>{classifier}</classifier>")
        lines.append(f"        <value>{value}</value>")
        lines.append("      </snapshotVersion>")
    lines += ["    </snapshotVersions>", "  </versioning>", "</metadata>"]
    return "\n".join(lines) + "\n"


def path(group, artifact, version, extension="pom", classifier="", value=None):
    base = f"{group.replace('.', '/')}/{artifact}/{version}"
    suffix = f"-{classifier}" if classifier else ""
    return f"{base}/{artifact}-{value or version}{suffix}.{extension}"


def manifest(repo, observation, observed_at, files):
    return {
        "observation": {"repo_id": repo, "observation_id": observation,
                        "observed_at": observed_at},
        "files": [{"path": p, "content": c} for p, c in files.items()],
    }


def build(directory):
    os.makedirs(directory, exist_ok=True)
    written = []

    def emit(name, data):
        target = os.path.join(directory, name)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        written.append(target)

    # -- observation 1: only the child ------------------------------------
    child = pom(
        "com.acme", "shop", "1.0",
        parent=("com.acme", "shop-parent", "1.0"),
        properties={"release": "2.0"},
        management=[("com.acme", "pricing", "${release}")],
        dependencies=[
            ("com.acme", "pricing", None),
            ("com.acme", "audit", "3.0"),
            ("com.acme", "telemetry", None),
        ],
    )
    emit("R1__obs-child-only.json", manifest(
        "R1", "obs-child-only", "2024-04-01T00:00:00Z",
        {path("com.acme", "shop", "1.0"): child},
    ))

    # -- observation 2: the R1 parent + dependencies ----------------------
    r1_parent = pom(
        "com.acme", "shop-parent", "1.0",
        properties={"release": "1.0", "telemetry-version": "4.1"},
        management=[
            ("com.acme", "pricing", "${release}"),
            ("com.acme", "telemetry", "${telemetry-version}"),
        ],
    )
    files_r1 = {path("com.acme", "shop-parent", "1.0"): r1_parent}
    for artifact, version in (("pricing", "2.0"), ("audit", "3.0"), ("telemetry", "4.1")):
        files_r1[path("com.acme", artifact, version)] = pom("com.acme", artifact, version)
        files_r1[path("com.acme", artifact, version, "jar")] = f"{artifact} {version} jar\n"
    emit("R1__obs-later.json", manifest(
        "R1", "obs-later", "2024-04-08T00:00:00Z", files_r1))

    # -- observation 3: an alternative parent in R0 -----------------------
    r0_parent = pom(
        "com.acme", "shop-parent", "1.0",
        properties={"release": "1.0", "telemetry-version": "5.5"},
        management=[
            ("com.acme", "pricing", "${release}"),
            ("com.acme", "telemetry", "${telemetry-version}"),
        ],
    )
    files_r0 = {
        path("com.acme", "shop-parent", "1.0"): r0_parent,
        path("com.acme", "telemetry", "5.5"): pom("com.acme", "telemetry", "5.5"),
        path("com.acme", "telemetry", "5.5", "jar"): "telemetry 5.5 jar\n",
    }
    emit("R0__obs-alt-parent.json", manifest(
        "R0", "obs-alt-parent", "2024-04-05T00:00:00Z", files_r0))

    # -- observation 4: a SNAPSHOT library with three mappings -------------
    snapshot_files = {
        path("com.acme", "stream", "2.0-SNAPSHOT", "jar", value="2.0-20240401.101010-1"):
            "stream main jar\n",
        path("com.acme", "stream", "2.0-SNAPSHOT", "jar", "tests",
             value="2.0-20240402.202020-2"): "stream tests jar\n",
        path("com.acme", "stream", "2.0-SNAPSHOT", extension="pom",
             value="2.0-20240403.303030-3"):
            pom("com.acme", "stream", "2.0-SNAPSHOT"),
        f"com/acme/stream/2.0-SNAPSHOT/maven-metadata.xml": metadata(
            "com.acme", "stream", "2.0-SNAPSHOT",
            [("pom", "", "2.0-20240403.303030-3"),
             ("jar", "", "2.0-20240401.101010-1"),
             ("jar", "tests", "2.0-20240402.202020-2")],
        ),
    }
    emit("R1__obs-snapshot.json", manifest(
        "R1", "obs-snapshot", "2024-04-02T00:00:00Z", snapshot_files))

    snapshot_app = pom(
        "com.acme", "snapshot-app", "1.0",
        dependencies=[
            ("com.acme", "stream", "2.0-SNAPSHOT"),
            ("com.acme", "stream", "2.0-SNAPSHOT", "jar", "tests"),
        ],
    )
    emit("R1__obs-snapshot-app.json", manifest(
        "R1", "obs-snapshot-app", "2024-04-02T00:00:00Z",
        {path("com.acme", "snapshot-app", "1.0"): snapshot_app},
    ))
    return written


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "examples/bundle"
    for filename in build(target):
        print(filename)
