"""Observation-scoped Maven dependency explanation and offline settlements.

See ``docs/DESIGN.md`` for the Maven subset and ``docs/BILLING.md`` for the
offline electricity settlement feature, including the time, roll-over and
precision rules.
"""

from .api import Api
from .billing import BILLING_ENGINE_VERSION, BillingApi, BillingInputError, settle_inputs
from .errors import Problem, Problematic, UsageError
from .proxyvote import (PROXYVOTE_ENGINE_VERSION, ProxyVoteApi,
                        ProxyVoteInputError)
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
    "ProxyVoteApi",
    "ProxyVoteInputError",
    "PROXYVOTE_ENGINE_VERSION",
]
__version__ = "1.0.0"
