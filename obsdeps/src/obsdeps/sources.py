"""Locatable source references used throughout the evidence trail."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class SourceRef:
    """A concrete file (optionally a line) inside an exact observation."""

    repo_id: str
    observation_id: str
    path: str
    sha256: str
    line: Optional[int] = None

    @property
    def locator(self) -> str:
        base = f"{self.repo_id}/{self.observation_id}/{self.path}"
        return f"{base}:{self.line}" if self.line is not None else base

    def with_line(self, line: Optional[int]) -> "SourceRef":
        return SourceRef(self.repo_id, self.observation_id, self.path, self.sha256, line)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "repo_id": self.repo_id,
            "observation_id": self.observation_id,
            "path": self.path,
            "sha256": self.sha256,
        }
        if self.line is not None:
            out["line"] = self.line
            out["locator"] = self.locator
        return out

    @staticmethod
    def from_file_fact(fact: Any, line: Optional[int] = None) -> "SourceRef":
        return SourceRef(
            repo_id=fact.repo_id,
            observation_id=fact.observation_id,
            path=fact.path,
            sha256=fact.sha256,
            line=line,
        )


def source_key(ref: Optional[SourceRef]) -> Optional[tuple]:
    if ref is None:
        return None
    return (ref.repo_id, ref.observation_id, ref.path, ref.sha256, ref.line)


def refs_equal(left: Optional[SourceRef], right: Optional[SourceRef]) -> bool:
    return source_key(left) == source_key(right)
