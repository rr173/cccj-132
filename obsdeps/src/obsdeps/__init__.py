"""Observation-scoped Maven dependency explanation and offline TOU settlement.

See ``docs/DESIGN.md`` for the Maven subset and ``docs/BILLING.md`` for the
offline electricity settlement feature, including the time, roll-over and
precision rules.
"""

from .api import Api
from .billing import BILLING_ENGINE_VERSION, BillingApi, BillingInputError, settle_inputs
from .errors import Problem, Problematic, UsageError
from .resolution import ENGINE_VERSION

__all__ = [
    "Api",
    "Problem",
    "Problematic",
    "UsageError",
    "ENGINE_VERSION",
    "BillingApi",
    "BillingInputError",
    "BILLING_ENGINE_VERSION",
    "settle_inputs",
]
__version__ = "1.0.0"
