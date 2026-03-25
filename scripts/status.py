#!/usr/bin/env python3
"""BMAD Autopilot status entrypoint wrapper."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from internal.status import *  # noqa: F403

if __name__ == "__main__":
    raise SystemExit(main())
