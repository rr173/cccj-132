"""Namespace-aware XML tree builder that keeps source line numbers.

POM elements are matched by their real namespace URI and local name, never by
the prefix spelling in the document.  A small expat-based builder is used
because :mod:`xml.etree` throws line numbers away and POM problems must be
locatable.  DTDs are refused: they are not used by the supported subset and
would open the door to entity expansion tricks.
"""

from __future__ import annotations

import xml.parsers.expat as expat
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import Problem


@dataclass
class XmlNode:
    uri: str
    local: str
    line: int
    attrs: Dict[Tuple[str, str], str] = field(default_factory=dict)
    children: List["XmlNode"] = field(default_factory=list)
    text_parts: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ text
    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def stripped_text(self) -> str:
        return self.text.strip()

    def child(self, local: str) -> Optional["XmlNode"]:
        for node in self.children:
            if node.local == local:
                return node
        return None

    def children_named(self, local: str) -> List["XmlNode"]:
        return [node for node in self.children if node.local == local]

    def path(self) -> str:
        return f"{self.local}@{self.line}"


class XmlBuildError(Exception):
    def __init__(self, problem: Problem):
        super().__init__(problem.render())
        self.problem = problem


class _Builder:
    def __init__(self, origin: str):
        self.origin = origin
        self.root: Optional[XmlNode] = None
        self.stack: List[XmlNode] = []

    def start(self, name: str, attrs: Dict[str, str]) -> None:
        uri, local = _split_name(name)
        node = XmlNode(
            uri=uri,
            local=local,
            line=self._line(),
            attrs={_split_attr(k): value for k, value in attrs.items()},
        )
        if self.stack:
            self.stack[-1].children.append(node)
        elif self.root is None:
            self.root = node
        self.stack.append(node)

    def end(self, name: str) -> None:
        self.stack.pop()

    def chars(self, data: str) -> None:
        if self.stack:
            self.stack[-1].text_parts.append(data)

    def _line(self) -> int:
        return self._parser.CurrentLineNumber

    _parser: "expat.XMLParserType"  # set by parse()


def _split_name(name: str) -> Tuple[str, str]:
    if "}" in name:
        uri, local = name.split("}", 1)
        return uri, local
    return "", name


def _split_attr(name: str) -> Tuple[str, str]:
    return _split_name(name)


def parse_xml(raw: bytes, origin: str) -> XmlNode:
    builder = _Builder(origin)
    parser = expat.ParserCreate(namespace_separator="}")
    builder._parser = parser
    parser.buffer_text = True

    def refuse_doctype(*_args: object) -> None:
        raise XmlBuildError(
            Problem("pom-doctype-unsupported", "DTDs are not supported in the POM subset", origin)
        )

    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.chars
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise XmlBuildError(
            Problem(
                "pom-xml-error",
                f"cannot parse XML: {exc}",
                f"{origin}:{getattr(exc, 'lineno', '?')}",
            )
        )
    if builder.root is None:
        raise XmlBuildError(Problem("pom-xml-error", "document has no root element", origin))
    return builder.root


def iter_children(node: XmlNode, local: str, *, root_uri: str) -> List[XmlNode]:
    """Children with ``local`` in the document's real namespace."""
    return [child for child in node.children if child.local == local and child.uri == root_uri]


def foreign_children(node: XmlNode, root_uri: str) -> List[XmlNode]:
    return [child for child in node.children if child.uri != root_uri]
