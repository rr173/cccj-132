"""Errors raised by the offline proxy-vote reconciliation package.

A :class:`ProxyVoteInputError` means the *document* is unusable as a whole:
malformed JSON, a non-array top-level shape, or an unreadable file.  A row
that is wrong, an authorization chain that cannot be interpreted, a ballot
that overshoots its authority -- all of those are *data* problems.  They are
reported as structured :class:`~obsdeps.errors.Problem` entries scoped to the
position they touch, so an unrelated position still reconciles and the run is
explicitly marked ``complete: false``.

A :class:`ReconciliationError` is raised by the API layer when the caller asks
for a result that cannot exist (for example ``rerun`` of an unknown request).
"""

from __future__ import annotations


class ProxyVoteInputError(Exception):
    """A proxy-vote input document cannot be parsed at all."""

    def __init__(self, message: str, *, source: str = ""):
        super().__init__(message)
        self.message = message
        self.source = source


class ReconciliationError(Exception):
    """A reconciliation request is internally impossible or unknown."""

    def __init__(self, message: str, *, code: str = "reconciliation-error",
                 locator: str = "", details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.locator = locator
        self.details = dict(details or {})

    def to_dict(self):
        result = {"code": self.code, "message": self.message}
        if self.locator:
            result["locator"] = self.locator
        if self.details:
            result["details"] = self.details
        return result
