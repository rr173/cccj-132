"""Interpolation of the supported POM expression subset.

Supported references:

* ``${project.groupId}``, ``${project.artifactId}``, ``${project.version}``
* ``${pom.groupId}`` etc. (Maven's legacy aliases; accepted as the same thing)
* ``${name}`` where ``name`` is a property visible in the current project's
  property environment.

Explicitly not supported / refused:

* arbitrary ``project.*`` expressions (``${project.name}``, ``${project.basedir}``)
* environment / settings expressions (``${env.HOME}``, ``${settings.x}``)
* late evaluation.  Interpolation is single-pass over a fully expanded value
  graph; after expansion every ``${`` must be gone or the input is rejected.

Property graph rules
--------------------
Each property keeps its *raw* text plus the POM that declared it.  Resolution
is a recursive graph walk with:

* a visited chain for cycle detection, reported with the exact chain
* explicit unknown-reference problems with the chain that led to them
* per-property results that are computed in the child's environment only, so
  a parent's resolved values are unaffected by a child override.

The parent-coordinate step uses a restricted environment (see
``pomproject.read_parent``) so a parent can be found before its own resolved
coordinates exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional

from .errors import Problem
from .sources import SourceRef

EXPRESSION = re.compile(r"\$\{([^}]*)\}")
REFERENCE_ALLOWED = re.compile(r"^[A-Za-z0-9_.\-]+$")
PROJECT_PREFIXES = ("project.", "pom.")

PROJECT_COORD_FIELDS = ("groupId", "artifactId", "version")


@dataclass(frozen=True)
class PropertyDecl:
    """A raw property definition and where it came from."""

    name: str
    raw: str
    source: Optional[SourceRef]
    origin_project: str

    @property
    def locator(self) -> str:
        return self.source.locator if self.source is not None else self.origin_project

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "raw": self.raw,
            "defined_in": self.source.to_dict() if self.source is not None else None,
            "project": self.origin_project,
        }


@dataclass
class ExpandedProperty:
    name: str
    raw: str
    value: Optional[str]
    decl: PropertyDecl
    chain: List[str]
    problems: List[Problem] = field(default_factory=list)

    def evidence(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "name": self.name,
            "raw": self.raw,
            "value": self.value,
            "defined_in": self.decl.source.to_dict() if self.decl.source else None,
            "project": self.decl.origin_project,
        }
        if len(self.chain) > 1:
            out["resolved_through"] = list(self.chain)
        return out


@dataclass
class ExpansionResult:
    value: Optional[str]
    referenced: List[str]
    problems: List[Problem]

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.problems

    def unknown_references(self) -> List[str]:
        return [
            str(p.details.get("reference"))
            for p in self.problems
            if p.code in ("unknown-property", "unsupported-expression", "unknown-project-coordinate")
        ]


class Interpolator:
    """Expands raw strings against a project's effective property table."""

    def __init__(
        self,
        project_coords: Optional[Mapping[str, str]],
        property_decls: Mapping[str, PropertyDecl],
        *,
        project_id: str,
        context_locator: str,
        forbid_project_refs: bool = False,
    ):
        self.project_coords = dict(project_coords) if project_coords is not None else None
        self.forbid_project_refs = forbid_project_refs
        self.property_decls = dict(property_decls)
        self.project_id = project_id
        self.context_locator = context_locator
        self._cache: Dict[str, ExpandedProperty] = {}
        self._in_progress: List[str] = []
        self.problems: List[Problem] = []
        self.referenced: List[str] = []

    # -------------------------------------------------------------- expansion
    def expand(self, raw: Optional[str], *, locator: str, what: str) -> ExpansionResult:
        if raw is None:
            return ExpansionResult(None, [], [])
        self.referenced = []
        problems: List[Problem] = []
        value = self._expand_once(raw, locator=locator, what=what, problems=problems, depth=0)
        referenced = list(dict.fromkeys(self.referenced))
        if value is not None and "${" in value:
            problems.append(Problem(
                "unresolved-expression",
                f"{what} still contains an unresolved expression: {value!r}",
                locator,
                {"raw": raw, "remaining": value},
            ))
            value = None
        return ExpansionResult(value, referenced, problems)

    # -------------------------------------------------------------- internals
    def _expand_once(
        self,
        raw: str,
        *,
        locator: str,
        what: str,
        problems: List[Problem],
        depth: int,
    ) -> Optional[str]:
        if depth > 64:
            problems.append(Problem(
                "expression-depth-exceeded",
                f"expression nesting exceeded the supported depth for {what}",
                locator,
                {"raw": raw},
            ))
            return None
        out: List[str] = []
        position = 0
        ok = True
        for match in EXPRESSION.finditer(raw):
            out.append(raw[position:match.start()])
            reference = match.group(1).strip()
            self.referenced.append(reference)
            replacement = self._lookup(reference, locator=locator, what=what, problems=problems)
            if replacement is None:
                ok = False
                out.append(match.group(0))
            else:
                out.append(replacement)
            position = match.end()
        out.append(raw[position:])
        if not ok:
            return None
        return "".join(out)

    def _lookup(self, reference: str, *, locator: str, what: str, problems: List[Problem]) -> Optional[str]:
        if not REFERENCE_ALLOWED.match(reference):
            problems.append(Problem(
                "unsupported-expression",
                f"expression ${{{reference}}} is not supported (unsupported characters)",
                locator,
                {"reference": reference, "context": what},
            ))
            return None
        if reference.startswith(PROJECT_PREFIXES):
            if self.forbid_project_refs or self.project_coords is None:
                problems.append(Problem(
                    "coordinate-project-reference",
                    f"${{{reference}}} is used while resolving a coordinate; coordinates may not "
                    f"depend on project.* values (chicken-and-egg: coordinates must exist before "
                    f"project.* can be evaluated)",
                    locator,
                    {"reference": reference, "context": what},
                ))
                return None
            prefix, field_name = reference.split(".", 1)
            if field_name not in PROJECT_COORD_FIELDS:
                problems.append(Problem(
                    "unsupported-expression",
                    f"expression ${{{reference}}} is not supported: only project.groupId/"
                    f"artifactId/version may be referenced",
                    locator,
                    {"reference": reference, "context": what},
                ))
                return None
            value = self.project_coords.get(field_name) if self.project_coords is not None else None
            if value is None:
                problems.append(Problem(
                    "unknown-project-coordinate",
                    f"${{{reference}}} is used but this project has no resolved {field_name}",
                    locator,
                    {"reference": reference, "context": what},
                ))
                return None
            return value
        if reference.startswith(("env.", "settings.", "system.")):
            problems.append(Problem(
                "unsupported-expression",
                f"expression ${{{reference}}} is not supported by the offline subset",
                locator,
                {"reference": reference, "context": what},
            ))
            return None
        return self._property_value(reference, locator=locator, what=what, problems=problems)

    def _property_value(self, name: str, *, locator: str, what: str, problems: List[Problem]) -> Optional[str]:
        if name in self._in_progress:
            cycle = self._in_progress[self._in_progress.index(name):] + [name]
            problems.append(Problem(
                "property-cycle",
                f"property cycle while expanding {what}: {' -> '.join(cycle)}",
                locator,
                {
                    "chain": cycle,
                    "context": what,
                    "sources": list(dict.fromkeys(
                    self._decl_location(n) for n in cycle if n in self.property_decls
                )),
                },
            ))
            return None
        decl = self.property_decls.get(name)
        if decl is None:
            problems.append(Problem(
                "unknown-property",
                f"${{{name}}} is not defined by this project, its effective properties "
                f"or project coordinates",
                locator,
                {"reference": name, "context": what},
            ))
            return None
        cached = self._cache.get(name)
        if cached is not None:
            if cached.value is None:
                problems.extend(cached.problems)
                return None
            return cached.value

        self._in_progress.append(name)
        try:
            sub_problems: List[Problem] = []
            value = self._expand_once(
                decl.raw,
                locator=decl.locator,
                what=f"property {name}",
                problems=sub_problems,
                depth=len(self._in_progress),
            )
            self._cache[name] = ExpandedProperty(
                name=name,
                raw=decl.raw,
                value=value,
                decl=decl,
                chain=list(self._in_progress),
                problems=list(sub_problems),
            )
            if value is None:
                problems.extend(sub_problems)
                return None
            return value
        finally:
            self._in_progress.pop()

    def _decl_location(self, name: str) -> str:
        decl = self.property_decls.get(name)
        return decl.locator if decl is not None else ""

    # --------------------------------------------------------------- evidence
    def expanded_property(self, name: str) -> Optional[ExpandedProperty]:
        return self._cache.get(name)

    def evaluate_property(self, name: str) -> Optional[ExpandedProperty]:
        """Expand one property for reporting without recording top-level problems.

        Used by the property report so every effective property can show its
        value (or the reason it has none).  Problems stay attached to the
        property entry; they become resolution errors only when a version or
        dependency coordinate actually references the property.
        """
        if name not in self.property_decls:
            return None
        self._in_progress = []
        scratch: List[Problem] = []
        self._property_value(name, locator=self.context_locator, what=f"property {name}",
                             problems=scratch)
        return self._cache.get(name)

    def property_evidence(self, names: Iterable[str]) -> List[Dict[str, object]]:
        out = []
        seen = set()
        for name in names:
            if name in seen or name.startswith(PROJECT_PREFIXES):
                continue
            seen.add(name)
            expanded = self._cache.get(name)
            if expanded is not None:
                out.append(expanded.evidence())
                continue
            decl = self.property_decls.get(name)
            if decl is not None:
                out.append(decl.to_dict())
        return out
