"""Helpers for building observation manifests in tests and examples."""

from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Optional, Tuple


def pom(
    group: Optional[str],
    artifact: str,
    version: Optional[str],
    *,
    parent: Optional[Tuple[str, str, str]] = None,
    properties: Optional[Dict[str, str]] = None,
    dependencies: Iterable[Tuple[str, str, Optional[str]]] = (),
    management: Iterable[Tuple[str, str, Optional[str]]] = (),
    extra: str = "",
    namespace: str = "http://maven.apache.org/POM/4.0.0",
) -> str:
    """Build a small POM string.

    ``dependencies`` / ``management`` entries are
    ``(groupId, artifactId, version-or-None)``; use :func:`dep` for
    type/classifier variants.
    """
    ns = f' xmlns="{namespace}"' if namespace else ""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<project{ns}>"]
    lines.append("  <modelVersion>4.0.0</modelVersion>")
    if parent is not None:
        lines.append("  <parent>")
        lines.append(f"    <groupId>{parent[0]}</groupId>")
        lines.append(f"    <artifactId>{parent[1]}</artifactId>")
        lines.append(f"    <version>{parent[2]}</version>")
        lines.append("  </parent>")
    if group is not None:
        lines.append(f"  <groupId>{group}</groupId>")
    lines.append(f"  <artifactId>{artifact}</artifactId>")
    if version is not None:
        lines.append(f"  <version>{version}</version>")
    if properties:
        lines.append("  <properties>")
        for name, value in properties.items():
            lines.append(f"    <{name}>{value}</{name}>")
        lines.append("  </properties>")
    if management:
        lines.append("  <dependencyManagement>")
        lines.append("    <dependencies>")
        for entry in management:
            lines.append("      " + _dep_xml(entry))
        lines.append("    </dependencies>")
        lines.append("  </dependencyManagement>")
    if dependencies:
        lines.append("  <dependencies>")
        for entry in dependencies:
            lines.append("    " + _dep_xml(entry))
        lines.append("  </dependencies>")
    if extra:
        lines.append(extra)
    lines.append("</project>")
    return "\n".join(lines) + "\n"


def dep(
    group: str,
    artifact: str,
    version: Optional[str] = None,
    *,
    type: Optional[str] = None,
    classifier: Optional[str] = None,
    scope: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    return {
        "groupId": group,
        "artifactId": artifact,
        "version": version,
        "type": type,
        "classifier": classifier,
        "scope": scope,
    }


def _dep_xml(entry) -> str:
    if isinstance(entry, tuple):
        group, artifact, version = entry
        entry = {"groupId": group, "artifactId": artifact, "version": version,
                 "type": None, "classifier": None, "scope": None}
    lines = ["<dependency>"]
    lines.append(f"  <groupId>{entry['groupId']}</groupId>")
    lines.append(f"  <artifactId>{entry['artifactId']}</artifactId>")
    if entry.get("version") is not None:
        lines.append(f"  <version>{entry['version']}</version>")
    if entry.get("type") is not None:
        lines.append(f"  <type>{entry['type']}</type>")
    if entry.get("classifier") is not None:
        lines.append(f"  <classifier>{entry['classifier']}</classifier>")
    if entry.get("scope") is not None:
        lines.append(f"  <scope>{entry['scope']}</scope>")
    lines.append("</dependency>")
    return "\n      ".join(lines)


def dep_path(group: str, artifact: str, version: str, extension: str,
             classifier: str = "", value: Optional[str] = None) -> str:
    base = f"{group.replace('.', '/')}/{artifact}/{version}"
    suffix = f"-{classifier}" if classifier else ""
    return f"{base}/{artifact}-{value or version}{suffix}.{extension}"


def metadata(
    group: str,
    artifact: str,
    version: str,
    mappings: Iterable[Tuple[str, str, str]],
    *,
    timestamp: Optional[str] = None,
    build_number: Optional[str] = None,
) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<metadata xmlns="http://maven.apache.org/POM/4.0.0">']
    lines.append(f"  <groupId>{group}</groupId>")
    lines.append(f"  <artifactId>{artifact}</artifactId>")
    lines.append(f"  <version>{version}</version>")
    lines.append("  <versioning>")
    if timestamp is not None or build_number is not None:
        lines.append("    <snapshot>")
        if timestamp is not None:
            lines.append(f"      <timestamp>{timestamp}</timestamp>")
        if build_number is not None:
            lines.append(f"      <buildNumber>{build_number}</buildNumber>")
        lines.append("    </snapshot>")
    lines.append("    <snapshotVersions>")
    for extension, classifier, value in mappings:
        lines.append("      <snapshotVersion>")
        lines.append(f"        <extension>{extension}</extension>")
        if classifier:
            lines.append(f"        <classifier>{classifier}</classifier>")
        lines.append(f"        <value>{value}</value>")
        lines.append("      </snapshotVersion>")
    lines.append("    </snapshotVersions>")
    lines.append("  </versioning>")
    lines.append("</metadata>")
    return "\n".join(lines) + "\n"


class Bundle:
    """A directory of observation manifests plus inline file content."""

    def __init__(self) -> None:
        # (repo_id, observation_id) -> {"observed_at": str, "files": {path: content}}
        self.observations: Dict[Tuple[str, str], Dict[str, object]] = {}

    def observation(self, repo_id: str, observation_id: str, observed_at: str) -> "Bundle":
        self.observations[(repo_id, observation_id)] = {"observed_at": observed_at, "files": {}}
        return self

    def add(self, repo_id: str, observation_id: str, path: str, content: str) -> "Bundle":
        key = (repo_id, observation_id)
        if key not in self.observations:
            raise KeyError(f"declare observation {key} first")
        files = self.observations[key]["files"]
        assert isinstance(files, dict)
        files[path] = content
        return self

    def add_coordinate_pom(self, repo_id: str, observation_id: str,
                           group: str, artifact: str, version: str, content: str) -> "Bundle":
        return self.add(repo_id, observation_id, dep_path(group, artifact, version, "pom"), content)

    def manifest(self, repo_id: str, observation_id: str) -> Dict[str, object]:
        info = self.observations[(repo_id, observation_id)]
        files = info["files"]
        assert isinstance(files, dict)
        return {
            "observation": {
                "repo_id": repo_id,
                "observation_id": observation_id,
                "observed_at": info["observed_at"],
            },
            "files": [
                {"path": path, "content": content}
                for path, content in files.items()
            ],
        }

    def write(self, directory: str) -> Dict[Tuple[str, str], str]:
        os.makedirs(directory, exist_ok=True)
        written: Dict[Tuple[str, str], str] = {}
        for (repo_id, observation_id) in self.observations:
            path = os.path.join(directory, f"{repo_id}__{observation_id}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.manifest(repo_id, observation_id), handle, indent=2)
            written[(repo_id, observation_id)] = path
        return written
