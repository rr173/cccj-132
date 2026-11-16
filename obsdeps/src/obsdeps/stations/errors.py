"""Errors raised by the charging-station directory feature.

* :class:`StationConfigError` -- a *source configuration* problem that makes a
  collection run impossible to start (unknown adapter, missing endpoint ...).
* :class:`TransportError` -- a single page could not be fetched or decoded; the
  engine turns it into a located page problem and keeps collecting the other
  sources instead of letting it escape.
"""

from __future__ import annotations


class StationConfigError(Exception):
    """The source/run configuration cannot be used as a whole."""

    def __init__(self, message: str, *, source: str = ""):
        super().__init__(message)
        self.message = message
        self.source = source


class TransportError(Exception):
    """One HTTP request failed at the transport level (network/HTTP/JSON)."""

    def __init__(self, message: str, *, url: str = "", cursor: str | None = None,
                 page_seq: int | None = None):
        super().__init__(message)
        self.message = message
        self.url = url
        self.cursor = cursor
        self.page_seq = page_seq
