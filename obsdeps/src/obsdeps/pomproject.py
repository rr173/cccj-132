"""Parent-chain loading and effective project state.

The parent chain is resolved before any effective property or dependency table
exists, under a deliberately restricted rule set:

* parent coordinates may use the declaring POM's *own literal* properties,
  expanded with that POM's own properties only;
* they may not use ``project.*`` (directly or through a property), and they may
  not depend on properties that only the parent would supply -- otherwise
  finding the parent would require the parent we are trying to find;
* lookup walks the request's ordered repository chain; the first selected
  observation that contains the coordinate's POM wins, and a damaged POM there
  is a contradiction rather than a reason to keep searching.

The walk produces nodes ordered outermost-ancestor-first, with every edge
recording repository, observation, path and digest.  Effective properties are
computed per project, top-down: raw definitions are inherited, the child's
definitions override them, and values are expanded only in the environment of
the project being asked about.  A parent's own resolved values are never
rewritten by a descendant's override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import Problem
from .interpolate import Interpolator, PropertyDecl
from .pom import Coordinate, ParsedPom
from .resolution import FileSelector, ParseCache, RepoSelection
from .sources import SourceRef
from .store import FileFact

MAX_PARENT_DEPTH = 64


@dataclass
class ParentEdge:
    child_pom: SourceRef
    child_line: Optional[int]
    parent_raw: Dict[str, Optional[str]]
    parent_coordinate: Optional[Coordinate] = None
    repo_id: Optional[str] = None
    observation_id: Optional[str] = None
    path: Optional[str] = None
    sha256: Optional[str] = None
    search_trail: List[Dict[str, object]] = field(default_factory=list)
    snapshot: Optional[Dict[str, object]] = None

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "child": self.child_pom.to_dict(),
            "child_line": self.child_line,
            "declared_parent": self.parent_raw,
        }
        if self.parent_coordinate is not None:
            out["parent_coordinate"] = self.parent_coordinate.as_dict()
        if self.path is not None:
            out["selected"] = {
                "repo_id": self.repo_id,
                "observation_id": self.observation_id,
                "path": self.path,
                "sha256": self.sha256,
            }
        if self.search_trail:
            out["search_trail"] = self.search_trail
        if self.snapshot is not None:
            out["snapshot"] = self.snapshot
        return out


@dataclass
class ProjectNode:
    fact: FileFact
    parsed: ParsedPom
    requested_coordinate: Optional[Coordinate]
    coordinate: Optional[Coordinate] = None
    parent_edge: Optional[ParentEdge] = None
    group_id_from_parent: bool = False
    version_from_parent: bool = False
    effective_raw_properties: Dict[str, PropertyDecl] = field(default_factory=dict)

    @property
    def ref(self) -> SourceRef:
        return SourceRef.from_file_fact(self.fact)

    def label(self) -> str:
        return self.coordinate.as_string() if self.coordinate is not None else self.fact.locator


@dataclass
class ChainResult:
    subject: Optional[ProjectNode] = None
    nodes: List[ProjectNode] = field(default_factory=list)   # outermost first
    problems: List[Problem] = field(default_factory=list)    # contradictions
    missing: List[Problem] = field(default_factory=list)      # absent material
    unsupported: List[Problem] = field(default_factory=list)
    property_lineage: Dict[str, List[Dict[str, object]]] = field(default_factory=dict)
    parent_edges: List[ParentEdge] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems and not self.missing

    def chain_summary(self) -> List[Dict[str, object]]:
        return [
            {
                "coordinate": node.coordinate.as_dict() if node.coordinate else None,
                "file": node.fact.to_ref(),
                "groupId_from_parent": node.group_id_from_parent,
                "version_from_parent": node.version_from_parent,
                "parent": node.parent_edge.to_dict() if node.parent_edge else None,
            }
            for node in self.nodes
        ]


class ProjectLoader:
    def __init__(
        self,
        selector: FileSelector,
        cache: ParseCache,
        chain: Sequence[RepoSelection],
    ):
        self.selector = selector
        self.cache = cache
        self.chain = tuple(chain)
        self._nodes: List[ProjectNode] = []

    # ------------------------------------------------------------------ entry
    def load_subject(
        self,
        *,
        coordinate: Optional[Coordinate],
        explicit_file: Optional[Tuple[str, str, str]],
    ) -> ChainResult:
        result = ChainResult()
        root = self._locate_root(result, coordinate, explicit_file)
        if root is None:
            return result
        self._collect_parse_facts(root, result)
        if root.parsed.problems:
            result.nodes = [root]
            self._finalize_partial(result)
            return result

        self._walk_up(root, result)
        if result.problems or result.missing:
            self._finalize_partial(result)
            return result

        result.subject = result.nodes[-1]
        return result

    def _locate_root(
        self,
        result: ChainResult,
        coordinate: Optional[Coordinate],
        explicit_file: Optional[Tuple[str, str, str]],
    ) -> Optional[ProjectNode]:
        if explicit_file is not None:
            repo_id, observation_id, path = explicit_file
            if not any(s.repo_id == repo_id and s.observation_id == observation_id for s in self.chain):
                result.problems.append(Problem(
                    "root-file-outside-context",
                    "explicit root POM belongs to an observation that is not in the request chain",
                    f"{repo_id}/{observation_id}/{path}",
                    {"chain": [s.to_dict() for s in self.chain]},
                ))
                return None
            fact = self.selector.locate_exact_in(repo_id, observation_id, path)
            if fact is None:
                result.missing.append(Problem(
                    "root-file-missing",
                    "explicit root POM path is not present in that observation",
                    f"{repo_id}/{observation_id}/{path}",
                ))
                return None
            return ProjectNode(fact=fact, parsed=self.cache.pom(fact), requested_coordinate=coordinate)

        assert coordinate is not None
        selection, problems, missing = self.selector.select_pom(coordinate, role="subject-pom")
        result.problems.extend(problems)
        result.missing.extend(missing)
        if not selection.present:
            return None
        assert selection.found is not None
        return ProjectNode(
            fact=selection.found,
            parsed=self.cache.pom(selection.found),
            requested_coordinate=coordinate,
        )

    # ------------------------------------------------------------- fact intake
    def _collect_parse_facts(self, node: ProjectNode, result: ChainResult) -> None:
        result.unsupported.extend(node.parsed.unsupported)
        result.problems.extend(node.parsed.problems)
        for dep in node.parsed.dependencies:
            result.unsupported.extend(dep.unsupported)
            result.problems.extend(dep.problems)
        for mgmt in node.parsed.management:
            result.unsupported.extend(mgmt.unsupported)
            result.problems.extend(mgmt.problems)

    # ------------------------------------------------------------------- walk
    def _walk_up(self, subject: ProjectNode, result: ChainResult) -> None:
        read: List[ProjectNode] = [subject]          # subject first while walking
        visited_files: List[str] = []
        visited_coords: List[str] = []
        current_label = self._node_label(subject)
        current = subject
        while True:
            visited_files.append(current.fact.locator)
            if current_label is not None:
                visited_coords.append(current_label)
            decl = current.parsed.parent
            if decl is None:
                break
            if len(result.parent_edges) >= MAX_PARENT_DEPTH:
                result.problems.append(Problem(
                    "parent-depth-exceeded",
                    f"parent chain deeper than {MAX_PARENT_DEPTH}",
                    current.fact.locator,
                    {"source_chain": list(visited_files)},
                ))
                break

            parent_coordinate, coord_problems = self._parent_coordinate(current, decl)
            edge = ParentEdge(
                child_pom=current.ref,
                child_line=decl.line,
                parent_raw={
                    "groupId": decl.group_id_raw,
                    "artifactId": decl.artifact_id_raw,
                    "version": decl.version_raw,
                },
                parent_coordinate=parent_coordinate,
            )
            current.parent_edge = edge
            result.parent_edges.append(edge)
            if coord_problems:
                result.problems.extend(coord_problems)
                break
            assert parent_coordinate is not None

            selection, problems, missing = self.selector.select_pom(parent_coordinate, role="parent-pom")
            result.problems.extend(problems)
            if not selection.present:
                for problem in missing:
                    result.missing.append(Problem(
                        problem.code,
                        problem.message,
                        problem.locator,
                        {
                            **problem.details,
                            "parent_of": current.fact.to_ref(),
                            "parent_of_coordinate": current_label,
                            "parent_coordinate": parent_coordinate.as_dict(),
                        },
                    ))
                break
            assert selection.found is not None
            edge.repo_id = selection.found.repo_id
            edge.observation_id = selection.found.observation_id
            edge.path = selection.found.path
            edge.sha256 = selection.found.sha256
            edge.search_trail = selection.trail
            edge.snapshot = selection.snapshot

            if selection.found.locator in visited_files:
                cycle_files = visited_files + [selection.found.locator]
                result.problems.append(Problem(
                    "parent-cycle",
                    "parent chain revisits a POM: " + " -> ".join(cycle_files),
                    current.fact.locator_at(decl.line),
                    {
                        "coordinates": visited_coords + [parent_coordinate.as_string()],
                        "source_chain": cycle_files,
                        "edges": [e.to_dict() for e in result.parent_edges],
                    },
                ))
                break

            parent_node = ProjectNode(
                fact=selection.found,
                parsed=self.cache.pom(selection.found),
                requested_coordinate=parent_coordinate,
            )
            self._collect_parse_facts(parent_node, result)
            if parent_node.parsed.problems:
                result.problems.append(Problem(
                    "parent-pom-invalid",
                    "the first selected observation that contains this parent POM has an "
                    "unusable POM; the chain stops here instead of falling through to a "
                    "later repository (records are not mixed across repositories)",
                    selection.found.locator,
                    {
                        "parent_coordinate": parent_coordinate.as_dict(),
                        "selected": selection.found.to_ref(),
                        "search_trail": selection.trail,
                    },
                ))
                break
            read.append(parent_node)
            current = parent_node
            current_label = parent_coordinate.as_string()

        result.nodes = list(reversed(read))
        self._nodes = result.nodes
        self._resolve_coordinates(result)
        if result.problems:
            return
        self._compute_effective_properties(result)

    @staticmethod
    def _node_label(node: ProjectNode) -> Optional[str]:
        if node.requested_coordinate is not None:
            return node.requested_coordinate.as_string()
        if node.coordinate is not None:
            return node.coordinate.as_string()
        return None

    # ---------------------------------------------------------- parent coords
    def _parent_coordinate(
        self, node: ProjectNode, decl
    ) -> Tuple[Optional[Coordinate], List[Problem]]:
        locator = node.fact.locator_at(decl.line)
        interpolator = Interpolator(
            None,
            self._own_literal_properties(node),
            project_id=node.fact.locator,
            context_locator=locator,
            forbid_project_refs=True,
        )
        problems: List[Problem] = []
        values: Dict[str, Optional[str]] = {}
        for field_name, raw in (
            ("groupId", decl.group_id_raw),
            ("artifactId", decl.artifact_id_raw),
            ("version", decl.version_raw),
        ):
            if raw is None:
                problems.append(Problem(
                    "parent-coordinate-missing",
                    f"parent/{field_name} is required",
                    locator,
                    {"field": field_name},
                ))
                values[field_name] = None
                continue
            expanded = interpolator.expand(raw, locator=locator, what=f"parent/{field_name}")
            if not expanded.ok:
                problems.append(Problem(
                    "parent-coordinate-invalid",
                    f"parent/{field_name} cannot be resolved using only this POM's literal "
                    f"properties: "
                    f"{expanded.problems[0].message if expanded.problems else 'unresolved'}",
                    locator,
                    {"field": field_name, "raw": raw,
                     "causes": [p.to_dict() for p in expanded.problems]},
                ))
                values[field_name] = None
            else:
                values[field_name] = expanded.value
        if problems:
            return None, problems
        return Coordinate(values["groupId"], values["artifactId"], values["version"]), []

    @staticmethod
    def _own_literal_properties(node: ProjectNode) -> Dict[str, PropertyDecl]:
        decls: Dict[str, PropertyDecl] = {}
        for prop in node.parsed.properties:
            decls[prop.name] = PropertyDecl(
                name=prop.name,
                raw=prop.raw,
                source=node.ref.with_line(prop.line),
                origin_project=node.fact.locator,
            )
        return decls

    # ---------------------------------------------------------- coordinates
    def _resolve_coordinates(self, result: ChainResult) -> None:
        for index, node in enumerate(result.nodes):
            parent_node = result.nodes[index - 1] if index > 0 else None
            parsed = node.parsed
            group = version = None

            if parsed.group_id_raw is not None:
                group, problems = self._expand_own_coordinate_field(node, parsed.group_id_raw, "groupId")
                result.problems.extend(problems)
                node.group_id_from_parent = False
            elif parent_node is not None and parent_node.coordinate is not None:
                group = parent_node.coordinate.group_id
                node.group_id_from_parent = True

            if parsed.version_raw is not None:
                version, problems = self._expand_own_coordinate_field(node, parsed.version_raw, "version")
                result.problems.extend(problems)
                node.version_from_parent = False
            elif parent_node is not None and parent_node.coordinate is not None:
                version = parent_node.coordinate.version
                node.version_from_parent = True

            artifact = parsed.artifact_id_raw
            if artifact is None:
                result.problems.append(Problem(
                    "coordinate-unresolved",
                    "artifactId is required on the project itself and cannot be inherited",
                    node.fact.locator,
                    {"field": "artifactId"},
                ))
                continue
            artifact_value, artifact_problems = self._expand_own_coordinate_field(
                node, artifact, "artifactId"
            )
            result.problems.extend(artifact_problems)
            if artifact_problems:
                continue
            if group is None or version is None:
                missing_fields = [f for f, v in (("groupId", group), ("version", version)) if v is None]
                # A declared parent that could not be read is already reported
                # as missing material (blocked).  Only complain here when the
                # POM itself is simply incomplete.
                if parsed.parent is None:
                    result.problems.append(Problem(
                        "coordinate-unresolved",
                        "cannot determine " + " and ".join(missing_fields)
                        + ": absent from this POM and the POM declares no parent",
                        node.fact.locator,
                        {"fields": missing_fields,
                         "groupId_raw": parsed.group_id_raw,
                         "version_raw": parsed.version_raw},
                    ))
                continue
            node.coordinate = Coordinate(group, artifact_value, version)
            if node.requested_coordinate is not None and node.coordinate != node.requested_coordinate:
                result.problems.append(Problem(
                    "coordinate-mismatch",
                    f"POM selected for {node.requested_coordinate.as_string()} declares "
                    f"{node.coordinate.as_string()}",
                    node.fact.locator,
                    {
                        "requested": node.requested_coordinate.as_dict(),
                        "declared": node.coordinate.as_dict(),
                    },
                ))

    def _expand_own_coordinate_field(
        self, node: ProjectNode, raw: str, field_name: str
    ) -> Tuple[Optional[str], List[Problem]]:
        # A project's own coordinates may use the properties it inherits, but
        # never project.* (that would be self-referential).
        decls = self._properties_visible_to(node)
        interpolator = Interpolator(
            None,
            decls,
            project_id=node.fact.locator,
            context_locator=node.fact.locator,
            forbid_project_refs=True,
        )
        expanded = interpolator.expand(raw, locator=node.fact.locator, what=f"project/{field_name}")
        if expanded.ok:
            return expanded.value, []
        return None, [Problem(
            "coordinate-invalid",
            f"project/{field_name} cannot be resolved: "
            f"{expanded.problems[0].message if expanded.problems else 'unresolved'}",
            node.fact.locator,
            {"field": field_name, "raw": raw,
             "causes": [p.to_dict() for p in expanded.problems]},
        )]

    def _properties_visible_to(self, node: ProjectNode) -> Dict[str, PropertyDecl]:
        try:
            index = self._nodes.index(node)
        except ValueError:
            index = len(self._nodes) - 1
        decls: Dict[str, PropertyDecl] = {}
        for ancestor in self._nodes[: index + 1]:
            for prop in ancestor.parsed.properties:
                decls[prop.name] = PropertyDecl(
                    name=prop.name,
                    raw=prop.raw,
                    source=ancestor.ref.with_line(prop.line),
                    origin_project=ancestor.fact.locator,
                )
        return decls

    # ------------------------------------------------------------ properties
    def _compute_effective_properties(self, result: ChainResult) -> None:
        effective: Dict[str, PropertyDecl] = {}
        lineage: Dict[str, List[Dict[str, object]]] = {}
        for node in result.nodes:
            for prop in node.parsed.properties:
                decl = PropertyDecl(
                    name=prop.name,
                    raw=prop.raw,
                    source=node.ref.with_line(prop.line),
                    origin_project=node.fact.locator,
                )
                project_label = node.coordinate.as_string() if node.coordinate else node.fact.locator
                if prop.name in effective:
                    previous = effective[prop.name]
                    lineage.setdefault(prop.name, []).append({
                        **previous.to_dict(),
                        "status": "overridden",
                        "project": previous.origin_project,
                        "overridden_by": decl.source.to_dict() if decl.source else None,
                        "overridden_by_project": project_label,
                    })
                effective[prop.name] = decl
                lineage.setdefault(prop.name, []).append({
                    **decl.to_dict(),
                    "status": "effective",
                    "project": node.coordinate.as_string() if node.coordinate else node.fact.locator,
                })
            node.effective_raw_properties = dict(effective)
        result.property_lineage = lineage

    def _finalize_partial(self, result: ChainResult) -> None:
        """Best-effort known facts for a blocked/invalid chain.

        No coordinate is invented: values are expanded literally when possible
        and otherwise left absent.  Effective properties are still exposed so a
        blocked report can show what *is* known.
        """
        if not result.nodes:
            return
        self._nodes = result.nodes
        result.subject = result.nodes[-1]
        self._compute_effective_properties(result)
        for index, node in enumerate(result.nodes):
            if node.coordinate is not None:
                continue
            parent_node = result.nodes[index - 1] if index > 0 else None
            parsed = node.parsed
            group = self._try_expand(node, parsed.group_id_raw)
            if group is None and parent_node is not None and parent_node.coordinate is not None:
                group = parent_node.coordinate.group_id
            version = self._try_expand(node, parsed.version_raw)
            if version is None and parent_node is not None and parent_node.coordinate is not None:
                version = parent_node.coordinate.version
            artifact = self._try_expand(node, parsed.artifact_id_raw)
            if group and artifact and version:
                node.coordinate = Coordinate(group, artifact, version)

    @staticmethod
    def _try_expand(node: ProjectNode, raw: Optional[str]) -> Optional[str]:
        if raw is None:
            return None
        interpolator = Interpolator(
            None,
            node.effective_raw_properties,
            project_id=node.fact.locator,
            context_locator=node.fact.locator,
            forbid_project_refs=True,
        )
        expanded = interpolator.expand(raw, locator=node.fact.locator, what="literal")
        return expanded.value if expanded.ok else None
