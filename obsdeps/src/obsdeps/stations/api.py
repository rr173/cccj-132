"""Public Python API for the repeatable charging-station directory collector.

Typical use::

    from obsdeps.stations import StationApi

    with StationApi("stations.sqlite3") as api:
        outcome = api.collect_from_file("sources.json", mock_fixture="mock.json")
        current = api.list_stations()
        one = api.get_station("P1", "S1")          # current state + evidence
        group = api.get_canonical_group("CAN-7")   # merged cross-network view

Collection is deterministic: the run id is a fingerprint of the normalized
evidence, so re-running the same fixture replaces the run with identical rows
and rebuilds the same derived state. A failed source never prevents the other
sources in the same batch from being collected, stored and queried.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from .engine import collect_sources
from .errors import StationConfigError
from .store import StationStore, deterministic_run_id
from .transport import HttpTransport, MockTransport, Transport

__all__ = ["StationApi", "StationConfigError"]


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def sources_from_document(document: Any) -> List[Dict[str, Any]]:
    """Accept either a bare source array or an object holding ``sources``."""
    if isinstance(document, dict) and isinstance(document.get("sources"), list):
        return document["sources"]
    if isinstance(document, list):
        return document
    raise StationConfigError(
        "source configuration must be an array or an object with a 'sources' array",
        source="sources")


class StationApi:
    """Facade over the collection engine, the transports and the SQLite store."""

    def __init__(self, database: str = ":memory:",
                 transport: Optional[Transport] = None):
        self.database = database
        self._store: Optional[StationStore] = None
        self._transport_override = transport

    # ------------------------------------------------------------- lifecycle
    def _ensure_store(self) -> StationStore:
        # Opened lazily so a configuration error rejected before any request
        # never creates an empty database file.
        if self._store is None:
            self._store = StationStore(self.database)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> "StationApi":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- collection
    def collect(self, sources: Sequence[Dict[str, Any]], *,
                transport: Optional[Transport] = None,
                run_id: Optional[str] = None,
                started_at: Optional[str] = None,
                max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Run one collection and persist it. Always returns a summary dict.

        A source-level transport/structure failure is reported inside the
        summary rather than raised: the remaining sources still complete and
        their rows remain queryable.
        """
        active_transport = transport or self._transport_override or HttpTransport()
        kwargs = {} if max_pages is None else {"max_pages": max_pages}
        result = collect_sources(list(sources), active_transport, **kwargs)
        resolved_run_id = run_id or deterministic_run_id(result)
        return self._ensure_store().save_run(
            result, run_id=resolved_run_id, started_at=started_at)

    def collect_from_file(self, sources_path: str, *,
                          mock_fixture: Optional[str] = None,
                          run_id: Optional[str] = None,
                          started_at: Optional[str] = None,
                          max_pages: Optional[int] = None) -> Dict[str, Any]:
        document = _load_json(sources_path)
        sources = sources_from_document(document)
        transport = MockTransport.from_file(mock_fixture) if mock_fixture else None
        return self.collect(sources, transport=transport, run_id=run_id,
                            started_at=started_at, max_pages=max_pages)

    # ------------------------------------------------------------- queries
    def list_stations(self, *, provider_id: Optional[str] = None,
                      status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._ensure_store().list_stations(
            provider_id=provider_id, status=status)

    def get_station(self, provider_id: str,
                    station_id: str) -> Optional[Dict[str, Any]]:
        return self._ensure_store().get_station(provider_id, station_id)

    def get_canonical_group(self, canonical_id: str) -> Optional[Dict[str, Any]]:
        return self._ensure_store().get_canonical_group(canonical_id)

    def list_runs(self) -> List[Dict[str, Any]]:
        return self._ensure_store().list_runs()

    def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._ensure_store().get_run_detail(run_id)

    def list_problems(self, *, run_id: Optional[str] = None) -> Dict[str, Any]:
        return self._ensure_store().list_problems(run_id=run_id)

    def list_sources(self) -> List[Dict[str, Any]]:
        return self._ensure_store().list_sources()
