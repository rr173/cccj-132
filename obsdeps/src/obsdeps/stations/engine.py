"""The collection engine: page a provider feed into validated station records.

The engine is deliberately transport- and storage-free. Given source
configurations and a :class:`~obsdeps.stations.transport.Transport` it:

1. walks each source page by page, carrying the provider's opaque cursor back
   verbatim into the next request for *that provider only* -- cursors are never
   shared across sources, so equal cursor text in two providers still advances
   each one independently;
2. stops a source and reports a **cycle** when a cursor already used by that
   source is returned again, and never marks a source that stopped early as
   complete;
3. isolates failures -- a transport or envelope error on one source becomes a
   located page problem for that source while the other sources in the same
   batch are still collected and remain queryable;
4. keeps record-level rejections separate: one bad station row is refused with
   its original row position, every other valid row is still accepted.

Page arrival order is irrelevant to the *values* kept: the engine only gathers
evidence here. Which record becomes the current state is decided later from the
observation timestamp (see :mod:`obsdeps.stations.store`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import Problem
from . import adapters
from .adapters import ADAPTER_TYPES, NormalizedStation
from .errors import StationConfigError, TransportError
from .transport import Transport

# Backstop so a feed that hands out fresh-but-infinite cursors cannot hang a
# run forever. Real termination is the documented cursor-cycle check.
DEFAULT_MAX_PAGES = 1000


@dataclass(frozen=True)
class SourceConfig:
    provider_id: str
    endpoint: str
    adapter_type: str


@dataclass
class PageRecord:
    page_seq: int
    request_cursor: Optional[str]
    next_cursor: Optional[str]
    stations_accepted: int
    records_rejected: int


@dataclass
class SourceRun:
    provider_id: str
    endpoint: str
    adapter_type: str
    status: str = "complete"          # "complete" | "incomplete"
    pages: List[PageRecord] = field(default_factory=list)
    stations: List[NormalizedStation] = field(default_factory=list)
    record_problems: List[Problem] = field(default_factory=list)
    page_problems: List[Problem] = field(default_factory=list)


@dataclass
class CollectionResult:
    sources: List[SourceRun]

    def summary(self) -> Dict[str, int]:
        return {
            "sources": len(self.sources),
            "complete": sum(1 for s in self.sources if s.status == "complete"),
            "incomplete": sum(1 for s in self.sources if s.status == "incomplete"),
            "stations_accepted": sum(len(s.stations) for s in self.sources),
            "records_rejected": sum(len(s.record_problems)
                                    for s in self.sources),
            "page_errors": sum(len(s.page_problems) for s in self.sources),
        }


def validate_sources(raw_sources: List[Dict[str, Any]]) -> List[SourceConfig]:
    """Validate the run configuration before any request is made."""
    if not isinstance(raw_sources, list) or not raw_sources:
        raise StationConfigError("a collection run needs at least one source")
    configs: List[SourceConfig] = []
    seen_ids: Dict[str, int] = {}
    for index, raw in enumerate(raw_sources):
        locator = f"sources[{index}]"
        if not isinstance(raw, dict):
            raise StationConfigError("source entry must be an object",
                                     source=locator)
        provider_id = raw.get("provider_id")
        endpoint = raw.get("endpoint")
        adapter_type = raw.get("adapter_type")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise StationConfigError("provider_id must be a non-empty string",
                                     source=f"{locator}.provider_id")
        provider_id = provider_id.strip()
        if provider_id in seen_ids:
            raise StationConfigError(
                f"provider_id {provider_id!r} is duplicated in this run",
                source=f"{locator}.provider_id")
        seen_ids[provider_id] = index
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise StationConfigError("endpoint must be a non-empty string",
                                     source=f"{locator}.endpoint")
        if adapter_type not in ADAPTER_TYPES:
            raise StationConfigError(
                f"adapter_type must be one of {list(ADAPTER_TYPES)}, "
                f"got {adapter_type!r}",
                source=f"{locator}.adapter_type")
        configs.append(SourceConfig(provider_id=provider_id,
                                    endpoint=endpoint.strip(),
                                    adapter_type=adapter_type))
    return configs


def collect_sources(raw_sources: List[Dict[str, Any]], transport: Transport,
                    *, max_pages: int = DEFAULT_MAX_PAGES) -> CollectionResult:
    """Collect every configured source, isolating per-source failures."""
    configs = validate_sources(raw_sources)
    runs: List[SourceRun] = []
    for config in configs:
        runs.append(_collect_one(config, transport, max_pages=max_pages))
    return CollectionResult(sources=runs)


def _collect_one(config: SourceConfig, transport: Transport,
                 *, max_pages: int) -> SourceRun:
    run = SourceRun(provider_id=config.provider_id,
                    endpoint=config.endpoint,
                    adapter_type=config.adapter_type)
    cursor: Optional[str] = None
    # Cursors already *sent* for this source. The first request has no cursor;
    # any later request whose cursor was already sent means the feed is cycling.
    seen_cursors = {None}
    page_seq = 1

    while True:
        if page_seq > max_pages:
            run.status = "incomplete"
            run.page_problems.append(Problem(
                "page-limit-exceeded",
                f"more than {max_pages} pages without an end cursor; stopped "
                "to avoid an unbounded run",
                f"{config.provider_id}/page{page_seq}",
                {"cursor": cursor}))
            break

        try:
            payload = transport.fetch_page(
                provider_id=config.provider_id, endpoint=config.endpoint,
                cursor=cursor, page_seq=page_seq)
            parsed = adapters.parse_page(
                config.adapter_type, payload,
                provider_id=config.provider_id, page_seq=page_seq)
        except TransportError as exc:
            run.status = "incomplete"
            run.page_problems.append(Problem(
                "page-transport-error",
                exc.message, f"{config.provider_id}/page{page_seq}",
                {"endpoint": config.endpoint, "cursor": cursor,
                 "page_seq": page_seq}))
            break
        except ValueError as exc:
            run.status = "incomplete"
            run.page_problems.append(Problem(
                "page-structure-error",
                f"response does not match adapter {config.adapter_type!r}: {exc}",
                f"{config.provider_id}/page{page_seq}",
                {"endpoint": config.endpoint, "cursor": cursor,
                 "page_seq": page_seq}))
            break

        run.pages.append(PageRecord(
            page_seq=page_seq, request_cursor=cursor,
            next_cursor=parsed.next_cursor,
            stations_accepted=len(parsed.stations),
            records_rejected=len(parsed.rejected)))
        run.stations.extend(parsed.stations)
        run.record_problems.extend(parsed.rejected)

        if parsed.next_cursor is None:
            run.status = "complete"
            break
        if parsed.next_cursor in seen_cursors:
            run.status = "incomplete"
            run.page_problems.append(Problem(
                "page-cursor-cycle",
                f"cursor {parsed.next_cursor!r} was already requested for this "
                "provider; stopping to avoid an infinite loop",
                f"{config.provider_id}/page{page_seq}",
                {"cursor": parsed.next_cursor, "page_seq": page_seq,
                 "already_seen": sorted(
                     c for c in seen_cursors if c is not None)}))
            break

        seen_cursors.add(parsed.next_cursor)
        cursor = parsed.next_cursor
        page_seq += 1

    return run
