"""maven-metadata.xml parsing for SNAPSHOT version selection.

Only the ``versioning/snapshotVersions`` mapping is used.  Timestamps and
build numbers are parsed and reported as observed facts but are never used to
synthesise a file name.  A mapping must exist for every wanted
(extension, classifier) pair; missing mappings are input problems, not
something to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import Problem
from .pom import MAVEN_POM_NAMESPACES
from .xmlutil import XmlNode, parse_xml


@dataclass(frozen=True)
class SnapshotMapping:
    extension: str
    classifier: str
    value: str
    updated: Optional[str]
    line: int

    @property
    def key(self) -> Tuple[str, str]:
        return (self.extension, self.classifier)


@dataclass
class SnapshotMetadata:
    file_locator: str
    group_id: Optional[str]
    artifact_id: Optional[str]
    version: Optional[str]
    timestamp: Optional[str]
    build_number: Optional[str]
    mappings: List[SnapshotMapping] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    unsupported: List[Problem] = field(default_factory=list)

    def mapping_for(self, extension: str, classifier: str) -> Tuple[Optional[SnapshotMapping], List[Problem]]:
        """Return the unique mapping and any duplicate-conflict problems."""
        wanted = (extension, classifier or "")
        matches = [m for m in self.mappings if m.key == wanted]
        if not matches:
            return None, []
        if len(matches) > 1:
            values = sorted({m.value for m in matches})
            problem = Problem(
                "snapshot-mapping-duplicate",
                (
                    f"maven-metadata.xml maps extension={extension!r} classifier={classifier!r} "
                    f"{len(matches)} times ({', '.join(values)}); refusing to pick one"
                ),
                self.file_locator,
                {
                    "extension": extension,
                    "classifier": classifier,
                    "values": values,
                    "lines": [m.line for m in matches],
                },
            )
            return matches[0], [problem]
        return matches[0], []

    def describe_available(self) -> List[Dict[str, str]]:
        return [
            {"extension": m.extension, "classifier": m.classifier, "value": m.value}
            for m in self.mappings
        ]


def parse_metadata(raw: bytes, file_locator: str) -> SnapshotMetadata:
    try:
        root = parse_xml(raw, file_locator)
    except Exception as exc:
        problem = getattr(exc, "problem", None) or Problem("metadata-xml-error", str(exc), file_locator)
        return SnapshotMetadata(file_locator, None, None, None, None, None, problems=[problem])

    problems: List[Problem] = []
    unsupported: List[Problem] = []
    if root.local != "metadata":
        problems.append(Problem(
            "metadata-invalid",
            f"root element is <{root.local}>, expected <metadata>",
            f"{file_locator}:{root.line}",
        ))
        return SnapshotMetadata(file_locator, None, None, None, None, None,
                                problems=problems, unsupported=unsupported)
    if root.uri not in MAVEN_POM_NAMESPACES:
        problems.append(Problem(
            "metadata-namespace-unsupported",
            f"metadata namespace {root.uri!r} is not supported",
            f"{file_locator}:{root.line}",
        ))

    def child(node: XmlNode, local: str) -> Optional[XmlNode]:
        for c in node.children:
            if c.local == local and c.uri == root.uri:
                return c
        return None

    def text(node: XmlNode, local: str) -> Optional[str]:
        c = child(node, local)
        return c.stripped_text if c is not None and c.stripped_text else None

    group_id = text(root, "groupId")
    artifact_id = text(root, "artifactId")
    version = text(root, "version")
    versioning = child(root, "versioning")
    timestamp = build_number = None
    mappings: List[SnapshotMapping] = []
    if versioning is not None:
        snapshot = child(versioning, "snapshot")
        if snapshot is not None:
            timestamp = text(snapshot, "timestamp")
            build_number = text(snapshot, "buildNumber")
            local_copy = text(snapshot, "localCopy")
            if local_copy is not None:
                unsupported.append(Problem(
                    "unsupported-node",
                    "snapshot/localCopy is recorded but not interpreted",
                    f"{file_locator}:{snapshot.line}",
                ))
        snapshot_versions = child(versioning, "snapshotVersions")
        if snapshot_versions is not None:
            for mapping_node in snapshot_versions.children:
                if mapping_node.local != "snapshotVersion" or mapping_node.uri != root.uri:
                    if mapping_node.uri == root.uri and mapping_node.local != "snapshotVersion":
                        unsupported.append(Problem(
                            "unsupported-node",
                            f"<{mapping_node.local}> inside snapshotVersions is not interpreted",
                            f"{file_locator}:{mapping_node.line}",
                        ))
                    continue
                ext = text(mapping_node, "extension")
                cls = text(mapping_node, "classifier") or ""
                value = text(mapping_node, "value")
                updated = text(mapping_node, "updated")
                if not ext or not value:
                    problems.append(Problem(
                        "snapshot-mapping-invalid",
                        "snapshotVersion needs both extension and value",
                        f"{file_locator}:{mapping_node.line}",
                        {"extension": ext, "classifier": cls, "value": value},
                    ))
                    continue
                mappings.append(SnapshotMapping(
                    extension=ext,
                    classifier=cls,
                    value=value,
                    updated=updated,
                    line=mapping_node.line,
                ))
    if timestamps_ignored(timestamp, build_number):
        pass
    return SnapshotMetadata(
        file_locator=file_locator,
        group_id=group_id,
        artifact_id=artifact_id,
        version=version,
        timestamp=timestamp,
        build_number=build_number,
        mappings=mappings,
        problems=problems,
        unsupported=unsupported,
    )


def timestamps_ignored(timestamp: Optional[str], build_number: Optional[str]) -> bool:
    """True when a timestamp/buildNumber pair is present but unusable for us."""
    return bool(timestamp or build_number)


def check_snapshot_identity(
    meta: SnapshotMetadata,
    *,
    group_id: str,
    artifact_id: str,
    version: str,
    file_locator: str,
) -> List[Problem]:
    problems = []
    for label, actual, wanted in (
        ("groupId", meta.group_id, group_id),
        ("artifactId", meta.artifact_id, artifact_id),
        ("version", meta.version, version),
    ):
        if actual is not None and actual != wanted:
            problems.append(Problem(
                "metadata-coordinate-mismatch",
                f"maven-metadata.xml declares {label}={actual!r}, wanted {wanted!r}",
                file_locator,
                {"field": label, "declared": actual, "wanted": wanted},
            ))
    return problems
