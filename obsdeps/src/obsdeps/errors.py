"""Problem objects that can be located in the user's input.

Every refusal is reported as a :class:`Problem` rather than a bare exception
wherever it can be attributed to a specific file, XML element, manifest entry,
request field or comparison side.  ``code`` is stable (documented in
docs/DESIGN.md) and ``locator`` points at the concrete input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    locator: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "locator": self.locator,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result

    def render(self) -> str:
        loc = f" [{self.locator}]" if self.locator else ""
        return f"{self.code}{loc}: {self.message}"


class Problematic(Exception):
    """An input problem that the caller asked us to raise instead of report."""

    def __init__(self, problem: Problem):
        super().__init__(problem.render())
        self.problem = problem


class UsageError(Exception):
    """A command line / API usage mistake that is not about repository data."""
