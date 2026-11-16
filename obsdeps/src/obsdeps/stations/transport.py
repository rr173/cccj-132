"""Page transports for the station directory collector.

Two implementations share one small interface (:meth:`Transport.fetch_page`):

* :class:`HttpTransport` -- real HTTP(S) using only the standard library. The
  provider's opaque cursor is sent back verbatim as the ``cursor`` query
  parameter; the transport never inspects or modifies it.
* :class:`MockTransport` -- a fully local, deterministic "interface" used by
  the tests and the worked example. It reads a JSON fixture that maps
  ``(provider_id, cursor)`` to prepared response bodies, so two providers can
  return identical cursor text while still advancing independently.

A failed fetch raises :class:`~obsdeps.stations.errors.TransportError` carrying
the URL, the cursor that was used and the page sequence, which the engine turns
into a located page problem scoped to that one provider.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Protocol

from .errors import TransportError


class Transport(Protocol):
    def fetch_page(self, *, provider_id: str, endpoint: str,
                   cursor: Optional[str], page_seq: int) -> Any:
        """Return the decoded JSON body for one page request."""


class HttpTransport:
    """Fetch pages over HTTP(S), sending the cursor back unchanged."""

    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout

    def fetch_page(self, *, provider_id: str, endpoint: str,
                   cursor: Optional[str], page_seq: int) -> Any:
        url = endpoint
        if cursor is not None:
            separator = "&" if urllib.parse.urlparse(endpoint).query else "?"
            url = f"{endpoint}{separator}cursor={urllib.parse.quote(cursor)}"
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise TransportError(
                f"HTTP {exc.code} while fetching page",
                url=url, cursor=cursor, page_seq=page_seq) from exc
        except urllib.error.URLError as exc:
            raise TransportError(
                f"network error while fetching page: {exc.reason}",
                url=url, cursor=cursor, page_seq=page_seq) from exc
        except (TimeoutError, OSError) as exc:
            raise TransportError(
                f"transport failure while fetching page: {exc}",
                url=url, cursor=cursor, page_seq=page_seq) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(
                f"response body is not valid JSON: {exc}",
                url=url, cursor=cursor, page_seq=page_seq) from exc


class MockTransport:
    """Deterministic local transport backed by an in-memory fixture.

    The fixture is a mapping keyed by ``provider_id``; each provider maps
    cursor values (``null``/``""`` for the first page) to JSON bodies. Because
    the key includes the provider, equal cursor strings from two providers stay
    independent -- exactly the situation the suite must prove.

    A provider may instead supply an ordered list of bodies; the transport then
    walks it page by page, independent of cursor text.
    """

    def __init__(self, fixture: Dict[str, Any]):
        if not isinstance(fixture, dict):
            raise TypeError("mock fixture must be an object keyed by provider_id")
        self._fixture = fixture
        self.request_log = []  # (provider_id, cursor, page_seq); aids assertions

    @classmethod
    def from_file(cls, path: str) -> "MockTransport":
        with open(path, "r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    def fetch_page(self, *, provider_id: str, endpoint: str,
                   cursor: Optional[str], page_seq: int) -> Any:
        self.request_log.append((provider_id, cursor, page_seq))
        provider_pages = self._fixture.get(provider_id)
        if provider_pages is None:
            raise TransportError(
                f"mock fixture has no provider {provider_id!r}",
                url=endpoint, cursor=cursor, page_seq=page_seq)

        if isinstance(provider_pages, list):
            if page_seq - 1 >= len(provider_pages):
                raise TransportError(
                    "mock fixture ran out of prepared pages",
                    url=endpoint, cursor=cursor, page_seq=page_seq)
            return provider_pages[page_seq - 1]

        if isinstance(provider_pages, dict):
            key = cursor if cursor is not None else ""
            if key not in provider_pages:
                raise TransportError(
                    f"mock fixture has no page for cursor {cursor!r}",
                    url=endpoint, cursor=cursor, page_seq=page_seq)
            return provider_pages[key]

        raise TransportError(
            "mock fixture entry must be an object of cursors or a list of pages",
            url=endpoint, cursor=cursor, page_seq=page_seq)
