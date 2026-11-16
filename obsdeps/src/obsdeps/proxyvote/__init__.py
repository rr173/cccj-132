"""Offline proxy-vote reconciliation for shareholder meetings.

The subpackage is self-contained: it reads local JSONL files or an immutable
local SQLite database, uses only the standard library, and never touches the
network.  See ``docs/PROXYVOTE.md`` for the half-open range model, the
cutoff-time authority semantics and the local conflict rule.
"""

from .api import ProxyVoteApi
from .engine import PROXYVOTE_ENGINE_VERSION, reconcile_meeting
from .errors import ProxyVoteInputError, ReconciliationError

__all__ = [
    "ProxyVoteApi",
    "ProxyVoteInputError",
    "ReconciliationError",
    "PROXYVOTE_ENGINE_VERSION",
    "reconcile_meeting",
]
