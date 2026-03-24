#!/usr/bin/env python3
"""BMAD Autopilot.

This is the readable orchestration core for the autopilot runner. The shell
wrapper in `bmad-autopilot.sh` only resolves the script location and execs this
Python file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable, Iterable, Optional, Sequence

import yaml
from pydantic import ValidationError

from internal.cockpit import (
    CockpitCodexSwitcher,
    cockpit_data_dir_candidates,
    extract_cockpit_quota_metrics,
    load_cockpit_codex_store,
    metric_above_threshold,
    metric_crossed_threshold,
    metric_margin_over_threshold,
    normalize_api_base_url,
    normalize_bool,
    normalize_int,
    normalize_text,
    pick_best_cockpit_switch_candidate,
    resolve_cockpit_current_account,
)
from internal.models import (
    AutopilotState,
    CockpitCodexAccount,
    CockpitCodexQuota,
    CockpitCodexStoreSnapshot,
    CockpitCodexSwitchCandidate,
    CockpitCodexSwitchSettings,
    CockpitCodexTokens,
    CodexAttemptResult,
    EpicDevOutput,
    PausedContext,
    PendingPR,
    Phase,
    ReviewDecisionOutput,
    ReviewSourceSnapshot,
    RuntimeConfig,
    SprintStatus,
    SprintStatusValue,
    StoryDevOutput,
    StoryTarget,
    ValidationFailure,
)
from internal.utils import read_text, timestamp, to_jsonable, utc_now, write_text
from internal.runner_environment import RunnerEnvironmentMixin
from internal.runner_legacy_pr_phases import LegacyPrPhasesMixin
from internal.runner_legacy_workflow_phases import LegacyWorkflowPhasesMixin
from internal.runner_review import RunnerReviewMixin
from internal.runner_state_worktree import RunnerStateWorktreeMixin
from internal.runner_update import RunnerUpdateMixin
from internal.runner_story_phases import StoryFlowPhasesMixin


class AutopilotRunner(RunnerEnvironmentMixin, RunnerStateWorktreeMixin, RunnerReviewMixin, RunnerUpdateMixin, StoryFlowPhasesMixin, LegacyWorkflowPhasesMixin, LegacyPrPhasesMixin):
    codex_reasoning_effort = "high"
    commit_split_reasoning_effort = "low"
    sound_profiles: dict[str, list[tuple[float, float]]] = {
        "quota": [(880.0, 0.14), (660.0, 0.14), (440.0, 0.26)],
        "review_ready": [(659.25, 0.12), (783.99, 0.12), (1046.50, 0.20)],
        "review_complete": [(523.25, 0.10), (659.25, 0.10), (783.99, 0.10), (1046.50, 0.14), (1318.51, 0.26)],
    }
    worktree_mirror_paths: tuple[Path, ...] = (
        Path(".autopilot"),
        Path(".codex"),
        Path(".env"),
        Path(".venv"),
        Path("skills"),
        Path("suggested_skills"),
        Path("frontend/node_modules"),
        Path("website/node_modules"),
    )
    sound_player_candidates = ("paplay", "aplay", "afplay", "ffplay", "play")

    allowed_config_keys = {
        "AUTOPILOT_DEBUG",
        "AUTOPILOT_VERBOSE",
        "AUTOPILOT_FLOW",
        "MAX_TURNS",
        "CHECK_INTERVAL",
        "MAX_CHECK_WAIT",
        "MAX_COPILOT_WAIT",
        "AUTOPILOT_RUN_MOBILE_NATIVE",
        "PARALLEL_MODE",
        "PARALLEL_CHECK_INTERVAL",
        "MAX_PENDING_PRS",
        "AUTOPILOT_BASE_BRANCH",
        "AUTOPILOT_CODEX_SWITCH_MODE",
        "AUTOPILOT_CODEX_SWITCH_PRIMARY_THRESHOLD",
        "AUTOPILOT_CODEX_SWITCH_SECONDARY_THRESHOLD",
        "AUTOPILOT_COCKPIT_DATA_DIR",
        "AUTOPILOT_QUOTA_RETRY_SECONDS",
        "AUTOPILOT_DEVELOPMENT_BLOCKED_RETRY_SECONDS",
    }

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project_root = self.detect_project_root().resolve()
        self.autopilot_dir = self.project_root / ".autopilot"
        self.config_file = self.autopilot_dir / "config"
        self.state_file = self.autopilot_dir / "state.json"
        self.log_file = self.autopilot_dir / "autopilot.log"
        self.tmp_dir = self.autopilot_dir / "tmp"
        self.debug_log = self.tmp_dir / "debug.log"
        self.worktree_dir = self.default_worktree_dir()
        self.sprint_status_file = self.project_root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"

        self.autopilot_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_dir.mkdir(parents=True, exist_ok=True)

        self.config = self.load_runtime_config()
        self.flow_mode = self.resolve_flow_mode()
        self.base_branch = self.config.base_branch or self.detect_base_branch()
        if not self.config.base_branch:
            self.config.base_branch = self.base_branch

        self.state = self.load_state()
        self.codex_switcher = CockpitCodexSwitcher(
            self.log,
            CockpitCodexSwitchSettings(
                mode=self.config.codex_switch_mode,
                primary_threshold=self.config.codex_switch_primary_threshold,
                secondary_threshold=self.config.codex_switch_secondary_threshold,
            ),
        )

        if self.config.debug_mode:
            write_text(
                self.debug_log,
                f"=== Debug session started: {timestamp()} ===\n"
                f"Config file: {self.config_file} (exists: {self.config_file.exists()})\n"
                f"Settings: MAX_TURNS={self.config.max_turns} CHECK_INTERVAL={self.config.check_interval} "
                f"MAX_CHECK_WAIT={self.config.max_check_wait}\n"
                f"Flow mode: {self.flow_mode}\n"
                f"Parallel mode: PARALLEL_MODE={self.config.parallel_mode} MAX_PENDING_PRS={self.config.max_pending_prs}\n",
            )

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------

def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BMAD Autopilot")
    parser.add_argument("epic_pattern", nargs="?", default="")
    parser.add_argument(
        "--from",
        dest="start_from",
        default="",
        help="Start selecting from a later story or epic, e.g. 3-1, 3.1, or 3",
    )
    parser.add_argument("--continue", dest="continue_run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-continue",
        dest="continue_run",
        action="store_false",
        help="Start fresh instead of resuming the previous state",
    )
    parser.set_defaults(continue_run=True)
    parser.add_argument(
        "--accept-dirty-worktree",
        action="store_true",
        help="Skip the interactive dirty-worktree confirmation prompt",
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    runner = AutopilotRunner(args)
    try:
        runner.run()
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        runner.log(f"❌ {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
