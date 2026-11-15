"""Shared test state: import path, temporary databases, small POM builder."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def make_api(name: str = ":memory:"):
    from obsdeps import Api
    return Api(name)
