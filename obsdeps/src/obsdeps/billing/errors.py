"""Errors raised by the offline billing package.

A :class:`BillingInputError` means an input *file* is unusable as a whole
(malformed JSON, unknown top-level shape).  Contradictions that can be
attributed to a single meter/period (duplicate readings, missing boundaries,
rule gaps ...) are never raised: they are returned as structured rejections so
that independent meters can still settle.
"""

from __future__ import annotations


class BillingInputError(Exception):
    """A billing input file cannot be parsed at all."""

    def __init__(self, message: str, *, source: str = ""):
        super().__init__(message)
        self.message = message
        self.source = source
