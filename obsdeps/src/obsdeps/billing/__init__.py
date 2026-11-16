"""Offline time-of-use electricity settlement.

The subpackage is deliberately self-contained: it only reads local files,
uses the standard library (``zoneinfo`` for IANA timezones, ``sqlite3`` for
storage) and never accesses the network.  See ``docs/BILLING.md`` for the
time, rollover and precision rules.
"""

from .api import BillingApi
from .engine import BILLING_ENGINE_VERSION, settle_inputs
from .errors import BillingInputError

__all__ = ["BillingApi", "BillingInputError", "BILLING_ENGINE_VERSION", "settle_inputs"]
