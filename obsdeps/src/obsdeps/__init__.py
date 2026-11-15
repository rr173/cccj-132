"""Observation-scoped Maven dependency explanation.

See ``docs/DESIGN.md`` for the supported subset, the deliberate differences
from real Maven, and the guarantees this package makes.
"""

from .api import Api
from .errors import Problem, Problematic, UsageError
from .resolution import ENGINE_VERSION

__all__ = ["Api", "Problem", "Problematic", "UsageError", "ENGINE_VERSION"]
__version__ = "1.0.0"
