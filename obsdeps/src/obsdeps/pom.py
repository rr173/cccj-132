"""POM parsing for the supported subset, with explicit unsupported reporting.

Only these parts of a POM are interpreted:

* ``project`` coordinates (with ``parent`` inheritance of groupId/version)
* ``project/parent`` coordinates
* ``project/properties``
* ``project/dependencyManagement/dependencies``
* ``project/dependencies`` (direct only, no transitive solving)

Anything else that could meaningfully change dependency resolution is reported
in the ``unsupported`` bucket with a locator.  Benign descriptive metadata
(name, licenses, ...) is ignored, see ``BENIGN_TOP_LEVEL``.

Element matching uses the real namespace URI and local name, so prefixes do
not matter and a foreign default namespace is rejected rather than silently
treated as Maven's namespace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import Problem
from .xmlutil import XmlNode, parse_xml

MAVEN_POM_NAMESPACES = frozenset({"", "http://maven.apache.org/POM/4.0.0"})

#: Top-level nodes that cannot change the dependency coordinates we compute.
BENIGN_TOP_LEVEL = frozenset({
    "modelVersion",
    "name",
    "description",
    "url",
    "inceptionYear",
    "licenses",
    "license",
    "developers",
    "developer",
    "contributors",
    "contributor",
    "scm",
    "organization",
    "issueManagement",
    "ciManagement",
    "mailingLists",
    "packaging",          # does not change the default dependency type 'jar'
    "reporting",
})

#: Meaningful Maven nodes that this subset deliberately ignores, with a reason.
#: They are reported (so nobody believes the whole POM was interpreted) but do
#: not make the result partial, because the documented policy replaces them.
POLICY_IGNORED_TOP_LEVEL = {
    "repositories": "repository lookup is taken from the request's explicit chain only",
    "pluginRepositories": "plugin resolution is outside this subset",
    "distributionManagement": "deployment is outside this subset",
    "prerequisites": "prerequisites do not affect dependency coordinates in this subset",
}

#: Meaningful nodes this subset refuses to interpret, with an impact flag.
#: ``affects_resolution`` says whether ignoring the node could change the
#: dependency coordinates/versions we report.  ``relocation`` changes the real
#: artifact identity; ``profiles`` can add properties/management, so both are
#: resolution-affecting.  ``build``/``modules`` do not change coordinates here.
UNSUPPORTED_TOP_LEVEL = {
    "build": False,
    "modules": False,
    "profiles": True,
    "relocation": True,
}

_DEPENDENCY_FIELDS = (
    "groupId", "artifactId", "version", "type", "classifier",
    "scope", "systemPath", "exclusions", "optional",
)

_PARENT_FIELDS = ("groupId", "artifactId", "version", "relativePath")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Coordinate:
    group_id: str
    artifact_id: str
    version: str

    def as_string(self) -> str:
        return f"{self.group_id}:{self.artifact_id}:{self.version}"

    def as_dict(self) -> Dict[str, str]:
        return {"groupId": self.group_id, "artifactId": self.artifact_id, "version": self.version}

    def version_path(self) -> str:
        return f"{self.group_id.replace('.', '/')}/{self.artifact_id}/{self.version}"

    def pom_path(self) -> str:
        return f"{self.version_path()}/{self.artifact_id}-{self.version}.pom"

    def artifact_path(self, extension: str, classifier: str = "", value: Optional[str] = None) -> str:
        suffix = f"-{classifier}" if classifier else ""
        return f"{self.version_path()}/{self.artifact_id}-{value or self.version}{suffix}.{extension}"

    def metadata_path(self) -> str:
        return f"{self.version_path()}/maven-metadata.xml"


@dataclass(frozen=True)
class DepKey:
    group_id: str
    artifact_id: str
    type: str = "jar"
    classifier: str = ""

    def render(self) -> str:
        return f"{self.group_id}:{self.artifact_id}:{self.type}:{self.classifier}"

    def as_dict(self) -> Dict[str, str]:
        return {
            "groupId": self.group_id,
            "artifactId": self.artifact_id,
            "type": self.type,
            "classifier": self.classifier,
            "key": self.render(),
        }


@dataclass(frozen=True)
class RawProperty:
    name: str
    raw: str
    line: int


@dataclass
class ParentDecl:
    group_id_raw: Optional[str]
    artifact_id_raw: Optional[str]
    version_raw: Optional[str]
    relative_path: Optional[str]
    line: int
    problems: List[Problem] = field(default_factory=list)


@dataclass
class DependencyDecl:
    group_id_raw: Optional[str]
    artifact_id_raw: Optional[str]
    version_raw: Optional[str]
    type_raw: Optional[str]
    classifier_raw: Optional[str]
    scope_raw: Optional[str]
    line: int
    version_line: Optional[int]
    problems: List[Problem] = field(default_factory=list)
    unsupported: List[Problem] = field(default_factory=list)


@dataclass
class ManagementDecl:
    group_id_raw: Optional[str]
    artifact_id_raw: Optional[str]
    version_raw: Optional[str]
    type_raw: Optional[str]
    classifier_raw: Optional[str]
    scope_raw: Optional[str]
    line: int
    version_line: Optional[int]
    problems: List[Problem] = field(default_factory=list)
    unsupported: List[Problem] = field(default_factory=list)


@dataclass
class ParsedPom:
    file_locator: str
    path: str
    group_id_raw: Optional[str]
    artifact_id_raw: Optional[str]
    version_raw: Optional[str]
    packaging: Optional[str]
    parent: Optional[ParentDecl]
    properties: List[RawProperty]
    dependencies: List[DependencyDecl]
    management: List[ManagementDecl]
    problems: List[Problem] = field(default_factory=list)
    unsupported: List[Problem] = field(default_factory=list)

    def declared_group(self) -> Optional[str]:
        return self.group_id_raw

    def declared_version(self) -> Optional[str]:
        return self.version_raw


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_pom(raw: bytes, file_locator: str, path: str) -> ParsedPom:
    """Parse POM bytes.  Never raises for POM content problems; reports them."""
    try:
        root = parse_xml(raw, file_locator)
    except Exception as exc:  # XmlBuildError
        problem = getattr(exc, "problem", None) or Problem(
            "pom-xml-error", str(exc), file_locator
        )
        return ParsedPom(
            file_locator=file_locator,
            path=path,
            group_id_raw=None,
            artifact_id_raw=None,
            version_raw=None,
            packaging=None,
            parent=None,
            properties=[],
            dependencies=[],
            management=[],
            problems=[problem],
        )

    if root.local != "project":
        return ParsedPom(
            file_locator=file_locator, path=path, group_id_raw=None, artifact_id_raw=None,
            version_raw=None, packaging=None, parent=None, properties=[], dependencies=[],
            management=[],
            problems=[Problem(
                "pom-invalid",
                f"root element is <{root.local}>, expected <project>",
                f"{file_locator}:{root.line}",
            )],
        )

    parser = _PomParser(root, file_locator, path)
    return parser.parse()


class _PomParser:
    def __init__(self, root: XmlNode, file_locator: str, path: str):
        self.root = root
        self.file_locator = file_locator
        self.path = path
        self.uri = root.uri
        self.problems: List[Problem] = []
        self.unsupported: List[Problem] = []

    # ---------------------------------------------------------------- helpers
    def loc(self, node: XmlNode) -> str:
        return f"{self.file_locator}:{node.line}"

    def own_children(self, node: XmlNode) -> List[XmlNode]:
        return [child for child in node.children if child.uri == self.uri]

    def foreign(self, node: XmlNode) -> List[Problem]:
        out = []
        for child in node.children:
            if child.uri != self.uri:
                out.append(Problem(
                    "unsupported-node",
                    f"element <{child.local}> uses namespace {child.uri!r}; "
                    f"only {self.uri!r} is interpreted",
                    self.loc(child),
                    {"node": child.local, "namespace": child.uri},
                ))
        return out

    def child(self, node: XmlNode, local: str) -> Optional[XmlNode]:
        found = [c for c in self.own_children(node) if c.local == local]
        if len(found) > 1:
            code = "duplicate-property" if local == "properties" else "pom-duplicate-element"
            self.problems.append(Problem(
                code,
                (
                    f"<{local}> appears {len(found)} times under <{node.local}>; "
                    f"merging repeated property containers is not supported"
                    if local == "properties" else
                    f"<{local}> appears {len(found)} times under <{node.local}>"
                ),
                self.loc(found[1]),
                {"element": local, "parent": node.local},
            ))
        return found[0] if found else None

    def text_of(self, node: XmlNode) -> str:
        return node.stripped_text

    def unsupported_node(self, node: XmlNode, reason: str) -> Problem:
        problem = Problem(
            "unsupported-node",
            f"<{node.local}> is not interpreted: {reason}",
            self.loc(node),
            {"node": node.local, "reason": reason, "affects_resolution": True},
        )
        self.unsupported.append(problem)
        return problem

    # ------------------------------------------------------------------ parse
    def parse(self) -> ParsedPom:
        if self.root.uri not in MAVEN_POM_NAMESPACES:
            self.problems.append(Problem(
                "pom-namespace-unsupported",
                f"namespace {self.root.uri!r} is not the Maven POM namespace",
                self.loc(self.root),
            ))

        group_id_raw = self._single_text("groupId")
        artifact_id_raw = self._single_text("artifactId")
        version_raw = self._single_text("version")
        packaging = self._single_text("packaging")

        self.unsupported.extend(self.foreign(self.root))
        for child in self.own_children(self.root):
            local = child.local
            if local in (
                "groupId", "artifactId", "version", "packaging", "modelVersion",
                "properties", "parent", "dependencies", "dependencyManagement",
            ):
                continue
            if local in BENIGN_TOP_LEVEL:
                continue
            if local in POLICY_IGNORED_TOP_LEVEL:
                problem = self.unsupported_node(child, POLICY_IGNORED_TOP_LEVEL[local])
                problem.details["affects_resolution"] = False
                continue
            if local in UNSUPPORTED_TOP_LEVEL:
                problem = self.unsupported_node(
                    child, "not part of the supported POM subset"
                )
                problem.details["affects_resolution"] = UNSUPPORTED_TOP_LEVEL[local]
                continue
            self.unsupported_node(child, "unknown or unsupported project node")

        parent = self._parse_parent()
        properties = self._parse_properties()
        dependencies = self._parse_dependencies(self.child(self.root, "dependencies"))
        management_node = self.child(self.root, "dependencyManagement")
        management: List[ManagementDecl] = []
        if management_node is not None:
            management = self._parse_management(management_node)

        if artifact_id_raw is None:
            self.problems.append(Problem(
                "pom-missing-artifact-id",
                "project/artifactId is required and cannot be inherited",
                self.file_locator,
            ))
        return ParsedPom(
            file_locator=self.file_locator,
            path=self.path,
            group_id_raw=group_id_raw,
            artifact_id_raw=artifact_id_raw,
            version_raw=version_raw,
            packaging=packaging,
            parent=parent,
            properties=properties,
            dependencies=dependencies,
            management=management,
            problems=self.problems,
            unsupported=self.unsupported,
        )

    def _single_text(self, name: str) -> Optional[str]:
        node = self.child(self.root, name)
        if node is None:
            return None
        value = self.text_of(node)
        return value or None

    def _parse_parent(self) -> Optional[ParentDecl]:
        node = self.child(self.root, "parent")
        if node is None:
            return None
        self.unsupported.extend(self.foreign(node))
        values: Dict[str, Optional[str]] = {name: None for name in _PARENT_FIELDS}
        for child in self.own_children(node):
            if child.local not in _PARENT_FIELDS:
                self.unsupported_node(child, "unknown parent node")
                continue
            if values[child.local] is not None:
                self.problems.append(Problem(
                    "pom-duplicate-element",
                    f"<parent><{child.local}> appears more than once",
                    self.loc(child),
                ))
                continue
            values[child.local] = self.text_of(child) or None
        decl = ParentDecl(
            group_id_raw=values["groupId"],
            artifact_id_raw=values["artifactId"],
            version_raw=values["version"],
            relative_path=values["relativePath"],
            line=node.line,
        )
        for field_name, label in (
            ("group_id_raw", "groupId"),
            ("artifact_id_raw", "artifactId"),
            ("version_raw", "version"),
        ):
            if getattr(decl, field_name) is None:
                decl.problems.append(Problem(
                    "parent-coordinate-missing",
                    f"parent/{label} is required",
                    self.loc(node),
                    {"field": label},
                ))
        if decl.relative_path is not None:
            self.unsupported.append(Problem(
                "unsupported-node",
                "<parent><relativePath> is ignored: parent lookup uses the request's repository chain",
                self.loc(node),
                {"node": "relativePath", "reason": "repository-chain-only parent lookup"},
            ))
        return decl

    def _parse_properties(self) -> List[RawProperty]:
        node = self.child(self.root, "properties")
        if node is None:
            return []
        self.unsupported.extend(self.foreign(node))
        props: List[RawProperty] = []
        seen: Dict[str, RawProperty] = {}
        for child in self.own_children(node):
            if child.children:
                self.unsupported.append(Problem(
                    "unsupported-node",
                    f"property <{child.local}> has child elements; only text values are supported",
                    self.loc(child),
                    {"node": child.local},
                ))
                continue
            name = child.local
            raw = child.text
            if name in seen:
                self.problems.append(Problem(
                    "duplicate-property",
                    f"property {name!r} is defined more than once in this POM",
                    self.loc(child),
                    {"property": name, "first_line": seen[name].line},
                ))
                continue
            prop = RawProperty(name=name, raw=raw, line=child.line)
            seen[name] = prop
            props.append(prop)
        return props

    def _parse_dependencies(self, node: Optional[XmlNode]) -> List[DependencyDecl]:
        if node is None:
            return []
        self.unsupported.extend(self.foreign(node))
        deps: List[DependencyDecl] = []
        for child in self.own_children(node):
            if child.local != "dependency":
                self.unsupported_node(child, "unknown dependencies child")
                continue
            deps.append(self._parse_dependency(child, direct=True))
        return deps

    def _parse_management(self, node: XmlNode) -> List[ManagementDecl]:
        self.unsupported.extend(self.foreign(node))
        deps: List[ManagementDecl] = []
        for child in self.own_children(node):
            if child.local != "dependencies":
                self.unsupported_node(child, "unknown dependencyManagement child")
                continue
            self.unsupported.extend(self.foreign(child))
            for dep_node in self.own_children(child):
                if dep_node.local != "dependency":
                    self.unsupported_node(dep_node, "unknown dependencyManagement/dependencies child")
                    continue
                decl = self._parse_dependency(dep_node, direct=False)
                deps.append(ManagementDecl(
                    group_id_raw=decl.group_id_raw,
                    artifact_id_raw=decl.artifact_id_raw,
                    version_raw=decl.version_raw,
                    type_raw=decl.type_raw,
                    classifier_raw=decl.classifier_raw,
                    scope_raw=decl.scope_raw,
                    line=decl.line,
                    version_line=decl.version_line,
                    problems=decl.problems,
                    unsupported=decl.unsupported,
                ))
        return deps

    def _parse_dependency(self, node: XmlNode, *, direct: bool) -> DependencyDecl:
        self.unsupported.extend(self.foreign(node))
        values: Dict[str, Optional[str]] = {f: None for f in _DEPENDENCY_FIELDS}
        lines: Dict[str, int] = {}
        dep_unsupported: List[Problem] = []
        for child in self.own_children(node):
            if child.local not in _DEPENDENCY_FIELDS:
                self.unsupported_node(child, "unknown dependency node")
                continue
            if values[child.local] is not None:
                self.problems.append(Problem(
                    "pom-duplicate-element",
                    f"<dependency><{child.local}> appears more than once",
                    self.loc(child),
                ))
                continue
            values[child.local] = self.text_of(child) or None
            lines[child.local] = child.line
            if child.local in ("scope", "optional", "exclusions", "systemPath"):
                affects = child.local == "systemPath"
                dep_unsupported.append(Problem(
                    "unsupported-node",
                    f"dependency/{child.local} is recorded but not interpreted by this subset",
                    self.loc(child),
                    {
                        "node": child.local,
                        "dependency_scope": "direct" if direct else "managed",
                        "affects_resolution": affects,
                    },
                ))
        problems: List[Problem] = []
        for field_name, label in (
            ("groupId", "groupId"),
            ("artifactId", "artifactId"),
        ):
            if values[field_name] is None:
                problems.append(Problem(
                    "pom-invalid",
                    f"dependency is missing required {label}",
                    self.loc(node),
                    {"field": label},
                ))
        if values["type"] is not None and values["type"].strip() == "":
            values["type"] = None
        if values["classifier"] is not None and values["classifier"].strip() == "":
            values["classifier"] = None
        scope_import = bool(values["scope"] and values["scope"].strip() == "import")
        if not direct and scope_import:
            dep_unsupported.append(Problem(
                "unsupported-bom-import",
                "dependencyManagement entry uses scope=import (BOM import); the management "
                "entry is excluded from the effective table",
                self.loc(node),
                {"scope": "import"},
            ))
        elif values["scope"] is not None and not direct:
            dep_unsupported.append(Problem(
                "unsupported-node",
                "managed dependency scope is recorded but not used to filter management keys",
                self.loc(node),
                {"node": "scope"},
            ))
        return DependencyDecl(
            group_id_raw=values["groupId"],
            artifact_id_raw=values["artifactId"],
            version_raw=values["version"],
            type_raw=values["type"],
            classifier_raw=values["classifier"],
            scope_raw=values["scope"],
            line=node.line,
            version_line=lines.get("version"),
            problems=problems,
            unsupported=dep_unsupported,
        )

    def loc_at_line(self, line: int) -> str:
        return f"{self.file_locator}:{line}"
