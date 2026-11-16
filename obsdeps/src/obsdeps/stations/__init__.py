"""Repeatable charging-station directory collection.

Collects cursor-paged station feeds from several charging networks, normalizes
two different JSON envelopes through adapter-specific field mappings, and keeps
an evidenced, queryable local picture of each station's current state in
SQLite. See ``docs/STATIONS.md`` for the identity rules, pagination failure
scope and rerun semantics.
"""

from .adapters import ADAPTER_TYPES
from .api import StationApi
from .engine import CollectionResult, SourceConfig, collect_sources
from .errors import StationConfigError, TransportError
from .store import CONFLICT_STATUS
from .transport import HttpTransport, MockTransport

__all__ = [
    "StationApi",
    "StationConfigError",
    "TransportError",
    "HttpTransport",
    "MockTransport",
    "collect_sources",
    "CollectionResult",
    "SourceConfig",
    "CONFLICT_STATUS",
    "ADAPTER_TYPES",
]
