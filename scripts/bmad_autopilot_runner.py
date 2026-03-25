#!/usr/bin/env python3
"""BMAD Autopilot compatibility wrapper.

The real orchestration implementation now lives in
``.autopilot/scripts/internal/runner_core.py``.
"""

from __future__ import annotations

from internal.runner_core import *  # noqa: F403

if __name__ == "__main__":
    raise SystemExit(main())
