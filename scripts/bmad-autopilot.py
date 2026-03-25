#!/usr/bin/env python3
"""BMAD Autopilot entrypoint wrapper.

The orchestration implementation lives in `bmad_autopilot_runner.py`.
This wrapper keeps the historical script path stable for shell entrypoints,
tests, and direct CLI use.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bmad_autopilot_runner import *  # noqa: F403

if __name__ == "__main__":
    raise SystemExit(main())
