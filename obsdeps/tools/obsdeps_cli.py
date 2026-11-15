#!/usr/bin/env python3
"""Launcher for running the CLI from a source checkout without installation.

Kept in its own directory so ``python -I tools/obsdeps_cli.py`` does not put the
repository root or the data directory on ``sys.path``.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from obsdeps.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
