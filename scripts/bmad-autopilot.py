#!/usr/bin/env python3
"""BMAD Autopilot.

This is the readable orchestration core for the autopilot runner. The shell
wrapper in `bmad-autopilot.sh` only resolves the script location and execs this
Python file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Any, Iterable, Optional, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class Phase(str, Enum):
    CHECK_PENDING_PR = "CHECK_PENDING_PR"
    FIND_EPIC = "FIND_EPIC"
    CREATE_BRANCH = "CREATE_BRANCH"
    DEVELOP_STORIES = "DEVELOP_STORIES"
    COMMIT_SPLIT = "COMMIT_SPLIT"
    QA_AUTOMATION_TEST = "QA_AUTOMATION_TEST"
    CODE_REVIEW = "CODE_REVIEW"
    CREATE_PR = "CREATE_PR"
    WAIT_COPILOT = "WAIT_COPILOT"
    WAIT_CHECKS = "WAIT_CHECKS"
    FIX_ISSUES = "FIX_ISSUES"
    MERGE_PR = "MERGE_PR"
    BLOCKED = "BLOCKED"
    DONE = "DONE"

    @classmethod
    def from_value(cls, value: str | Phase | None, default: Phase = None) -> Phase:
        if isinstance(value, Phase):
            return value
        if value:
            try:
                return cls(value)
            except ValueError:
                pass
        return default or cls.FIND_EPIC


@dataclass
class PendingPR:
    epic: str
    pr_number: int
    worktree: str
    status: str = "WAIT_REVIEW"
    last_check: str = ""
    last_copilot_id: Optional[str] = None


@dataclass
class PausedContext:
    epic: str
    phase: str


class SprintStatusValue(str, Enum):
    BACKLOG = "backlog"
    IN_PROGRESS = "in-progress"
    DONE = "done"
    READY_FOR_DEV = "ready-for-dev"
    REVIEW = "review"
    OPTIONAL = "optional"
    BLOCKED = "blocked"


class SprintStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated: datetime
    last_updated: datetime
    project: str
    project_key: str
    tracking_system: str
    story_location: Path
    development_status: dict[str, SprintStatusValue]

    def normalized_story_root(self, project_root: Path) -> Path:
        root = self.story_location if self.story_location.is_absolute() else project_root / self.story_location
        return root.resolve()

    def epic_key(self, epic_id: str) -> str:
        return f"epic-{epic_id}"

    def retrospective_key(self, epic_id: str) -> str:
        return f"epic-{epic_id}-retrospective"

    def epic_status(self, epic_id: str) -> SprintStatusValue | None:
        return self.development_status.get(self.epic_key(epic_id))

    def epic_story_entries(self, epic_id: str) -> list[tuple[str, SprintStatusValue]]:
        prefix = f"{epic_id}-"
        return [
            (key, status)
            for key, status in self.development_status.items()
            if key.startswith(prefix) and key != self.retrospective_key(epic_id)
        ]

    def active_epic_ids(self) -> list[str]:
        epic_ids: list[str] = []
        for key, status in self.development_status.items():
            match = re.fullmatch(r"epic-(\d+)", key)
            if not match:
                continue
            if status in {SprintStatusValue.BACKLOG, SprintStatusValue.DONE}:
                continue
            epic_ids.append(match.group(1))
        return epic_ids

    def story_files_for_epic(self, project_root: Path, epic_id: str) -> list[Path]:
        story_root = self.normalized_story_root(project_root)
        entries = self.epic_story_entries(epic_id)
        if not entries:
            raise ValueError(f"No story entries found in sprint status for epic {epic_id}")

        files: list[Path] = []
        missing: list[Path] = []
        for key, _status in entries:
            story_path = story_root / f"{key}.md"
            if story_path.exists():
                files.append(story_path)
            else:
                missing.append(story_path)

        if missing:
            missing_list = ", ".join(str(path) for path in missing)
            raise ValueError(f"Missing story file(s) for epic {epic_id}: {missing_list}")

        return files

    def story_context_lines(self, project_root: Path, epic_id: str) -> list[str]:
        story_root = self.normalized_story_root(project_root)
        lines: list[str] = []
        for key, status in self.epic_story_entries(epic_id):
            story_path = story_root / f"{key}.md"
            if not story_path.exists():
                raise ValueError(f"Missing story file for epic {epic_id}: {story_path}")
            lines.append(f"- {key} [{status.value}]: {story_path}")
        return lines


@dataclass
class AutopilotState:
    mode: str = "sequential"
    phase: Phase = Phase.FIND_EPIC
    current_epic: Optional[str] = None
    completed_epics: list[str] = field(default_factory=list)
    pending_prs: list[PendingPR] = field(default_factory=list)
    paused_context: Optional[PausedContext] = None
    active_phase: Optional[Phase] = None
    active_epic: Optional[str] = None
    active_worktree: Optional[str] = None

    @classmethod
    def initial(cls, parallel_mode: bool) -> AutopilotState:
        mode = "parallel" if parallel_mode else "sequential"
        return cls(
            mode=mode,
            phase=Phase.FIND_EPIC,
            current_epic=None,
            completed_epics=[],
            pending_prs=[],
            paused_context=None,
            active_phase=Phase.FIND_EPIC if parallel_mode else None,
            active_epic=None,
            active_worktree=None,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], parallel_mode: bool) -> AutopilotState:
        mode = data.get("mode") or ("parallel" if parallel_mode else "sequential")
        phase = Phase.from_value(data.get("phase"))
        active_phase = Phase.from_value(data.get("active_phase"), default=None) if data.get("active_phase") else None
        if parallel_mode and active_phase is None:
            active_phase = phase

        pending_prs = []
        for item in data.get("pending_prs", []) or []:
            if isinstance(item, dict):
                pending_prs.append(PendingPR.from_dict(item))

        paused_context = None
        if isinstance(data.get("paused_context"), dict):
            paused_context = PausedContext(
                epic=str(data["paused_context"].get("epic", "")),
                phase=str(data["paused_context"].get("phase", "")),
            )

        completed = [str(epic) for epic in data.get("completed_epics", []) or [] if epic is not None]

        return cls(
            mode=mode,
            phase=phase,
            current_epic=data.get("current_epic"),
            completed_epics=completed,
            pending_prs=pending_prs,
            paused_context=paused_context,
            active_phase=active_phase,
            active_epic=data.get("active_epic"),
            active_worktree=data.get("active_worktree"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "phase": self.phase.value,
            "current_epic": self.current_epic,
            "completed_epics": list(self.completed_epics),
            "pending_prs": [pr.to_dict() for pr in self.pending_prs],
            "paused_context": self.paused_context.to_dict() if self.paused_context else None,
            "active_phase": self.active_phase.value if self.active_phase else None,
            "active_epic": self.active_epic,
            "active_worktree": self.active_worktree,
        }

    @property
    def is_parallel(self) -> bool:
        return self.mode == "parallel"

    @property
    def effective_phase(self) -> Phase:
        if self.is_parallel and self.active_phase:
            return self.active_phase
        return self.phase

    @property
    def effective_epic(self) -> Optional[str]:
        if self.is_parallel and self.active_epic:
            return self.active_epic
        return self.current_epic


@dataclass
class RuntimeConfig:
    epic_pattern: str = ""
    continue_run: bool = False
    debug_mode: bool = False
    verbose_mode: bool = False
    max_turns: int = 80
    check_interval: int = 30
    max_check_wait: int = 60
    max_copilot_wait: int = 60
    run_mobile_native: bool = False
    parallel_mode: int = 0
    parallel_check_interval: int = 60
    max_pending_prs: int = 2
    base_branch: str = "main"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class AutopilotRunner:
    codex_model = "gpt-5.4-mini"
    codex_reasoning_effort = "xhigh"
    commit_split_reasoning_effort = "low"

    allowed_config_keys = {
        "AUTOPILOT_DEBUG",
        "AUTOPILOT_VERBOSE",
        "MAX_TURNS",
        "CHECK_INTERVAL",
        "MAX_CHECK_WAIT",
        "MAX_COPILOT_WAIT",
        "AUTOPILOT_RUN_MOBILE_NATIVE",
        "PARALLEL_MODE",
        "PARALLEL_CHECK_INTERVAL",
        "MAX_PENDING_PRS",
        "AUTOPILOT_BASE_BRANCH",
    }

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project_root = self.detect_project_root()
        self.autopilot_dir = self.project_root / ".autopilot"
        self.config_file = self.autopilot_dir / "config"
        self.state_file = self.autopilot_dir / "state.json"
        self.log_file = self.autopilot_dir / "autopilot.log"
        self.tmp_dir = self.autopilot_dir / "tmp"
        self.debug_log = self.tmp_dir / "debug.log"
        self.worktree_dir = self.autopilot_dir / "worktrees"
        self.sprint_status_file = self.project_root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"

        self.autopilot_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_dir.mkdir(parents=True, exist_ok=True)

        self.config = self.load_runtime_config()
        self.base_branch = self.config.base_branch or self.detect_base_branch()
        if not self.config.base_branch:
            self.config.base_branch = self.base_branch

        self.state = self.load_state()

        if self.config.debug_mode:
            write_text(
                self.debug_log,
                f"=== Debug session started: {timestamp()} ===\n"
                f"Config file: {self.config_file} (exists: {self.config_file.exists()})\n"
                f"Settings: MAX_TURNS={self.config.max_turns} CHECK_INTERVAL={self.config.check_interval} "
                f"MAX_CHECK_WAIT={self.config.max_check_wait}\n"
                f"Parallel mode: PARALLEL_MODE={self.config.parallel_mode} MAX_PENDING_PRS={self.config.max_pending_prs}\n",
            )

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------

    def detect_project_root(self) -> Path:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            return Path(result.stdout.strip())
        except subprocess.CalledProcessError:
            return Path.cwd()

    def detect_base_branch(self) -> str:
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            branch = result.stdout.strip().removeprefix("origin/")
            if branch:
                return branch
        except subprocess.CalledProcessError:
            pass

        if self.run_git(["show-ref", "--verify", "--quiet", "refs/heads/main"], check=False).returncode == 0:
            return "main"
        if self.run_git(["show-ref", "--verify", "--quiet", "refs/heads/master"], check=False).returncode == 0:
            return "master"
        return "main"

    def load_config_values(self) -> dict[str, str]:
        if not self.config_file.exists():
            return {}

        values: dict[str, str] = {}
        for raw_line in self.config_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
                value = value[1:-1]
            if key in self.allowed_config_keys:
                values[key] = value
            else:
                self.log(f"⚠️ Ignoring unknown config key: {key}")
        return values

    def load_runtime_config(self) -> RuntimeConfig:
        file_values = self.load_config_values()

        def env_or_file(key: str, default: str) -> str:
            return os.environ.get(key, file_values.get(key, default))

        debug_mode = self.args.debug or self.to_bool(env_or_file("AUTOPILOT_DEBUG", "0"))
        verbose_mode = self.args.verbose or self.to_bool(env_or_file("AUTOPILOT_VERBOSE", "0")) or debug_mode

        base_branch = env_or_file("AUTOPILOT_BASE_BRANCH", "")
        if not base_branch:
            base_branch = ""

        return RuntimeConfig(
            epic_pattern=self.args.epic_pattern or "",
            continue_run=bool(self.args.continue_run),
            debug_mode=debug_mode,
            verbose_mode=verbose_mode,
            max_turns=self.to_int(env_or_file("MAX_TURNS", "80"), 80),
            check_interval=self.to_int(env_or_file("CHECK_INTERVAL", "30"), 30),
            max_check_wait=self.to_int(env_or_file("MAX_CHECK_WAIT", "60"), 60),
            max_copilot_wait=self.to_int(env_or_file("MAX_COPILOT_WAIT", "60"), 60),
            run_mobile_native=self.to_bool(env_or_file("AUTOPILOT_RUN_MOBILE_NATIVE", "0")),
            parallel_mode=self.to_int(env_or_file("PARALLEL_MODE", "0"), 0),
            parallel_check_interval=self.to_int(env_or_file("PARALLEL_CHECK_INTERVAL", "60"), 60),
            max_pending_prs=self.to_int(env_or_file("MAX_PENDING_PRS", "2"), 2),
            base_branch=base_branch,
        )

    @staticmethod
    def to_bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def to_int(value: str, default: int) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def require_cmd(self, cmd: str) -> None:
        if shutil.which(cmd) is None:
            raise RuntimeError(f"❌ Required command not found: {cmd}")

    def require_tooling(self) -> None:
        for cmd in ("git", "gh", "codex", "python3"):
            self.require_cmd(cmd)

    def log(self, message: str) -> None:
        line = f"[{timestamp()}] {message}"
        print(line)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def verbose(self, message: str) -> None:
        line = f"[{timestamp()}] {message}"
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.config.verbose_mode:
            print(line)

    def debug(self, message: str) -> None:
        if not self.config.debug_mode:
            return
        line = f"[{timestamp()}] DEBUG: {message}"
        with self.debug_log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.config.verbose_mode:
            print(line)

    def run_process(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture_output: bool = False,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(command),
            cwd=str(cwd or self.project_root),
            text=True,
            input=input_text,
            capture_output=capture_output,
            env=env,
        )
        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            details = stderr or stdout or "command failed"
            raise RuntimeError(details)
        return result

    def run_json(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> Any:
        result = self.run_process(command, cwd=cwd, check=check, capture_output=True)
        output = (result.stdout or "").strip()
        if not output:
            return None
        return json.loads(output)

    def run_text(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture_output: bool = True,
    ) -> str:
        result = self.run_process(command, cwd=cwd, check=check, capture_output=capture_output)
        return result.stdout or ""

    def run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture_output: bool = False,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_process(["git", *args], cwd=cwd, check=check, capture_output=capture_output, input_text=input_text)

    def run_streaming_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        output_file: Path | None = None,
    ) -> int:
        cwd = cwd or self.project_root
        output_path = output_file or (self.tmp_dir / "codex-output.txt")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self.verbose(f"   Working dir: {cwd}")
        self.verbose(f"   Output: {output_path}")
        if input_text:
            preview = input_text[:200].replace("\n", "\\n")
            self.verbose(f"   Prompt (first 200 chars): {preview}...")

        with output_path.open("w", encoding="utf-8") as out_fh:
            proc = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            if input_text is not None and proc.stdin is not None:
                proc.stdin.write(input_text)
                proc.stdin.close()

            while True:
                line = proc.stdout.readline()
                if line == "" and proc.poll() is not None:
                    break
                if line:
                    print(line, end="")
                    out_fh.write(line)
                    out_fh.flush()

            returncode = proc.wait()
        return returncode

    def run_codex_exec(
        self,
        prompt: str,
        output_file: Path | None = None,
        *,
        cwd: Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> int:
        selected_model = model or self.codex_model
        selected_reasoning_effort = reasoning_effort or self.codex_reasoning_effort
        self.log(f"🤖 Codex exec ({selected_model}, reasoning={selected_reasoning_effort})")
        command = [
            "codex",
            "exec",
            "-c",
            f"model={json.dumps(selected_model)}",
            "-c",
            f"model_reasoning_effort={json.dumps(selected_reasoning_effort)}",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(cwd or self.project_root),
            "-",
        ]
        return self.run_streaming_command(command, cwd=cwd or self.project_root, input_text=prompt, output_file=output_file)

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def ensure_state_file(self) -> None:
        if not self.state_file.exists():
            self.state = AutopilotState.initial(self.config.parallel_mode >= 1)
            self.save_state()

    def load_state(self) -> AutopilotState:
        if not self.state_file.exists():
            return AutopilotState.initial(self.config.parallel_mode >= 1)
        data = json.loads(read_text(self.state_file, "{}"))
        return AutopilotState.from_dict(data, self.config.parallel_mode >= 1)

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        write_text(self.state_file, json.dumps(to_jsonable(self.state), indent=2) + "\n")

    def state_phase(self) -> Phase:
        return self.state.effective_phase

    def state_current_epic(self) -> Optional[str]:
        return self.state.effective_epic

    def state_set(self, phase: Phase | str, epic: str | None = None) -> None:
        phase_value = Phase.from_value(phase)
        if self.state.is_parallel:
            self.state.active_phase = phase_value
            self.state.active_epic = epic
            self.state.phase = phase_value
            self.state.current_epic = epic
        else:
            self.state.phase = phase_value
            self.state.current_epic = epic
        self.save_state()

    def state_mark_completed(self, epic: str) -> None:
        if epic not in self.state.completed_epics:
            self.state.completed_epics.append(epic)
        self.save_state()

    def state_add_pending_pr(self, epic_id: str, pr_number: int, wt_path: str) -> None:
        self.state.pending_prs = [pr for pr in self.state.pending_prs if pr.epic != epic_id]
        self.state.pending_prs.append(
            PendingPR(
                epic=epic_id,
                pr_number=int(pr_number),
                worktree=wt_path,
                status="WAIT_REVIEW",
                last_check=utc_now(),
                last_copilot_id=None,
            )
        )
        self.save_state()
        self.debug(f"Added pending PR: epic={epic_id} pr=#{pr_number}")

    def state_get_pending_pr(self, epic_id: str) -> Optional[PendingPR]:
        for pr in self.state.pending_prs:
            if pr.epic == epic_id:
                return pr
        return None

    def state_update_pending_pr(self, epic_id: str, field_name: str, value: Any) -> None:
        for pr in self.state.pending_prs:
            if pr.epic == epic_id:
                if hasattr(pr, field_name):
                    setattr(pr, field_name, value)
                break
        self.save_state()

    def state_remove_pending_pr(self, epic_id: str) -> None:
        self.state.pending_prs = [pr for pr in self.state.pending_prs if pr.epic != epic_id]
        self.save_state()
        self.debug(f"Removed pending PR: epic={epic_id}")

    def state_count_pending_prs(self) -> int:
        return len(self.state.pending_prs)

    def state_get_all_pending_prs(self) -> list[PendingPR]:
        return list(self.state.pending_prs)

    def state_save_active_context(self) -> None:
        epic_id = self.state_current_epic()
        phase = self.state_phase().value
        if epic_id:
            self.state.paused_context = PausedContext(epic=epic_id, phase=phase)
            self.save_state()
            self.log(f"💾 Saved active context: epic={epic_id} phase={phase}")

    def state_restore_active_context(self) -> bool:
        if not self.state.paused_context:
            return False
        paused = self.state.paused_context
        self.state.paused_context = None
        self.state_set(paused.phase, paused.epic)
        self.log(f"▶️ Restored active context: epic={paused.epic} phase={paused.phase}")
        return True

    # ------------------------------------------------------------------
    # Filesystem / worktree helpers
    # ------------------------------------------------------------------

    def worktree_path(self, epic_id: str) -> Path:
        return self.worktree_dir / f"epic-{epic_id}"

    def worktree_exists(self, epic_id: str) -> bool:
        return self.worktree_path(epic_id).exists()

    def worktree_create(self, epic_id: str, branch_name: str) -> Path:
        wt_path = self.worktree_path(epic_id)
        if wt_path.exists():
            self.debug(f"Worktree already exists: {wt_path}")
            return wt_path

        self.log(f"🌳 Creating worktree for {epic_id} at {wt_path}")
        self.run_process(["git", "worktree", "add", str(wt_path), branch_name], cwd=self.project_root, check=False)
        if not wt_path.exists():
            self.run_process(["git", "worktree", "add", "-b", branch_name, str(wt_path), self.base_branch], cwd=self.project_root)
        return wt_path

    def worktree_remove(self, epic_id: str) -> None:
        wt_path = self.worktree_path(epic_id)
        if not wt_path.exists():
            self.debug(f"Worktree does not exist: {wt_path}")
            return
        self.log(f"🗑️ Removing worktree for {epic_id}")
        self.run_process(["git", "worktree", "remove", "--force", str(wt_path)], cwd=self.project_root, check=False)

    def worktree_prune(self) -> None:
        self.log("🧹 Pruning orphaned worktrees...")
        self.run_process(["git", "worktree", "prune"], cwd=self.project_root, check=False)

    def sync_base_branch(self) -> None:
        self.run_process(["git", "checkout", self.base_branch], cwd=self.project_root, check=False)
        self.run_process(["git", "pull", "origin", self.base_branch], cwd=self.project_root, check=False)

    # ------------------------------------------------------------------
    # Epic discovery
    # ------------------------------------------------------------------

    def load_sprint_status(self) -> SprintStatus:
        if not self.sprint_status_file.exists():
            raise ValueError(f"Missing sprint status file: {self.sprint_status_file}")

        raw = yaml.safe_load(read_text(self.sprint_status_file))
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid sprint status YAML: {self.sprint_status_file}")

        try:
            sprint_status = SprintStatus.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid sprint status YAML: {self.sprint_status_file}") from exc

        expected_story_root = (self.project_root / "_bmad-output" / "implementation-artifacts").resolve()
        actual_story_root = sprint_status.normalized_story_root(self.project_root)
        if actual_story_root != expected_story_root:
            raise ValueError(
                "Sprint status story_location does not match the repository implementation-artifacts directory: "
                f"{actual_story_root} != {expected_story_root}"
            )

        return sprint_status

    def epic_matches_patterns(self, epic: str, sprint_status: SprintStatus) -> bool:
        if not self.config.epic_pattern:
            return True
        story_tokens = " ".join(key for key, _status in sprint_status.epic_story_entries(epic))
        haystack = " ".join([f"epic-{epic}", epic, story_tokens])
        for pattern in self.config.epic_pattern.split():
            if re.search(pattern, haystack, re.IGNORECASE):
                return True
        return False

    def find_next_epic(self, sprint_status: SprintStatus) -> Optional[str]:
        completed = set(self.state.completed_epics)
        pending = {pr.epic for pr in self.state.pending_prs}
        for epic in sprint_status.active_epic_ids():
            if not self.epic_matches_patterns(epic, sprint_status):
                continue
            if epic in completed or epic in pending:
                continue
            sprint_status.story_files_for_epic(self.project_root, epic)
            return epic
        return None

    # ------------------------------------------------------------------
    # GitHub helpers
    # ------------------------------------------------------------------

    def gh_repo_info(self) -> tuple[str, str]:
        result = self.run_json(["gh", "repo", "view", "--json", "owner,name"], cwd=self.project_root)
        if not result:
            raise RuntimeError("Could not determine repo info")
        return result["owner"]["login"], result["name"]

    def gh_pr_view(self, pr_number: int, fields_value: str) -> Any:
        return self.run_json(["gh", "pr", "view", str(pr_number), "--json", fields_value], cwd=self.project_root, check=False)

    def gh_pr_checks(self, pr_number: int) -> list[dict[str, Any]]:
        result = self.run_json(["gh", "pr", "checks", str(pr_number), "--json", "name,conclusion,status"], cwd=self.project_root, check=False)
        if isinstance(result, list):
            return result
        return []

    def gh_graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args.extend(["-F", f"{key}={value}"])
        result = self.run_json(args, cwd=self.project_root, check=False)
        return result or {}

    def count_unresolved_threads(self, pr_number: int) -> int:
        try:
            owner, repo = self.gh_repo_info()
        except RuntimeError:
            return 0
        query = dedent(
            """
            query($owner: String!, $repo: String!, $pr: Int!) {
              repository(owner: $owner, name: $repo) {
                pullRequest(number: $pr) {
                  reviewThreads(first: 100) {
                    nodes { isResolved }
                  }
                }
              }
            }
            """
        ).strip()
        data = self.gh_graphql(query, owner=owner, repo=repo, pr=pr_number)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        return sum(1 for node in nodes if not node.get("isResolved"))

    def get_unresolved_threads_content(self, pr_number: int) -> str:
        try:
            owner, repo = self.gh_repo_info()
        except RuntimeError:
            return ""
        query = dedent(
            """
            query($owner: String!, $repo: String!, $pr: Int!) {
              repository(owner: $owner, name: $repo) {
                pullRequest(number: $pr) {
                  reviewThreads(first: 100) {
                    nodes {
                      isResolved
                      path
                      line
                      comments(first: 10) {
                        nodes {
                          body
                          author { login }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
        ).strip()
        data = self.gh_graphql(query, owner=owner, repo=repo, pr=pr_number)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )

        parts: list[str] = []
        for thread in nodes:
            if thread.get("isResolved"):
                continue
            file_path = thread.get("path") or "unknown"
            line = thread.get("line") or "?"
            comment_lines = []
            for comment in thread.get("comments", {}).get("nodes", []):
                author = comment.get("author", {}).get("login", "unknown")
                body = comment.get("body", "")
                comment_lines.append(f"{author}: {body}")
            parts.append(f"📁 File: {file_path}:{line}\n" + "\n".join(comment_lines) + "\n---")
        return "\n".join(parts)

    def resolve_pr_review_threads(self, pr_number: int) -> None:
        try:
            owner, repo = self.gh_repo_info()
        except RuntimeError:
            self.log("⚠️ Could not determine repo info for resolving threads")
            return

        query = dedent(
            """
            query($owner: String!, $repo: String!, $pr: Int!) {
              repository(owner: $owner, name: $repo) {
                pullRequest(number: $pr) {
                  reviewThreads(first: 100) {
                    nodes {
                      id
                      isResolved
                    }
                  }
                }
              }
            }
            """
        ).strip()
        data = self.gh_graphql(query, owner=owner, repo=repo, pr=pr_number)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        unresolved = [node["id"] for node in nodes if not node.get("isResolved") and node.get("id")]
        if not unresolved:
            self.debug("No unresolved review threads found")
            return

        mutation = dedent(
            """
            mutation($threadId: ID!) {
              resolveReviewThread(input: {threadId: $threadId}) {
                thread { isResolved }
              }
            }
            """
        ).strip()
        resolved_count = 0
        for thread_id in unresolved:
            data = self.gh_graphql(mutation, threadId=thread_id)
            if data:
                resolved_count += 1
        if resolved_count:
            self.log(f"✅ Resolved {resolved_count} review thread(s)")

    def check_pending_pr_status(self, epic_id: str, pr_number: int, worktree: str) -> str:
        self.debug(f"Checking PR #{pr_number} for epic {epic_id}")
        pr_info = self.gh_pr_view(pr_number, "state,reviews")
        state = (pr_info or {}).get("state", "").upper()
        if state == "MERGED":
            return "merged"
        if state == "CLOSED":
            return "closed"
        if state != "OPEN":
            return "waiting"

        checks = self.gh_pr_checks(pr_number)
        if any(str(check.get("conclusion", "")).lower() == "failure" for check in checks):
            return "needs_fixes"

        ci_pending = any(
            str(check.get("status", "")).lower() != "completed" and str(check.get("conclusion", "")).lower() != "success"
            for check in checks
        )

        reviews = (pr_info or {}).get("reviews", []) or []
        approved = any(review.get("state") == "APPROVED" for review in reviews)
        copilot_reviews = [
            review
            for review in reviews
            if "copilot" in str(review.get("author", {}).get("login", "")).lower()
        ]
        copilot_reviews.sort(key=lambda review: review.get("submittedAt") or review.get("createdAt") or "")
        if copilot_reviews and copilot_reviews[-1].get("state") == "CHANGES_REQUESTED":
            return "needs_fixes"

        if self.count_unresolved_threads(pr_number) > 0:
            return "needs_fixes"

        if approved and not ci_pending:
            return "approved"

        return "waiting"

    def check_all_pending_prs(self) -> Optional[str]:
        pending_prs = self.state_get_all_pending_prs()
        if not pending_prs:
            self.debug("No pending PRs to check")
            return None

        self.log(f"🔍 Checking {len(pending_prs)} pending PR(s)...")
        pr_to_fix: Optional[str] = None

        for pr in list(pending_prs):
            status = self.check_pending_pr_status(pr.epic, pr.pr_number, pr.worktree)
            if status == "approved":
                self.log(f"✅ PR #{pr.pr_number} (epic {pr.epic}) is approved and ready to merge")
                self.handle_approved_pr(pr.epic, pr.pr_number, pr.worktree)
            elif status == "merged":
                self.log(f"✅ PR #{pr.pr_number} (epic {pr.epic}) was already merged")
                self.handle_merged_pr(pr.epic, pr.worktree)
            elif status == "closed":
                self.log(f"⚠️ PR #{pr.pr_number} (epic {pr.epic}) was closed without merge")
                self.state_remove_pending_pr(pr.epic)
                self.worktree_remove(pr.epic)
            elif status == "needs_fixes":
                self.log(f"⚠️ PR #{pr.pr_number} (epic {pr.epic}) needs fixes")
                if pr_to_fix is None:
                    pr_to_fix = pr.epic
            else:
                self.debug(f"PR #{pr.pr_number} (epic {pr.epic}) still waiting for review/CI")
                self.state_update_pending_pr(pr.epic, "last_check", utc_now())

        return pr_to_fix

    def handle_approved_pr(self, epic_id: str, pr_number: int, wt_path: str) -> None:
        self.log(f"🔀 Merging approved PR #{pr_number} for epic {epic_id}")
        if self.run_process(["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"], cwd=self.project_root, check=False).returncode == 0:
            self.log(f"✅ PR #{pr_number} merged successfully")
            self.sync_base_branch()
            self.state_remove_pending_pr(epic_id)
            self.state_mark_completed(epic_id)
            self.run_retrospective_for_epic(epic_id)
            self.worktree_remove(epic_id)
        else:
            self.log(f"❌ Failed to merge PR #{pr_number}")
            self.state_update_pending_pr(epic_id, "status", "MERGE_FAILED")

    def handle_merged_pr(self, epic_id: str, wt_path: str) -> None:
        self.state_remove_pending_pr(epic_id)
        self.state_mark_completed(epic_id)
        self.run_retrospective_for_epic(epic_id)
        self.worktree_remove(epic_id)
        self.log(f"🧹 Cleaned up after merged epic {epic_id}")

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def build_dev_story_prompt(self, epic_id: str, sprint_status: SprintStatus, story_files: list[Path]) -> str:
        return "$bmad-dev-story\n"

    def build_qa_prompt(self, epic_id: str, sprint_status: SprintStatus, story_files: list[Path]) -> str:
        return "$integration-tests-workflow\n"

    def build_code_review_prompt(self, epic_id: str) -> str:
        current_branch = self.run_text(["git", "branch", "--show-current"], cwd=self.project_root, check=False, capture_output=True).strip()
        branch_diff = self.run_text(
            ["git", "diff", "--name-only", f"origin/{self.base_branch}..HEAD"],
            cwd=self.project_root,
            check=False,
            capture_output=True,
        ).strip()
        working_tree = self.run_text(["git", "status", "--short"], cwd=self.project_root, check=False, capture_output=True).strip()
        return dedent(
            f"""
            $bmad-code-review

            Review target:
            - Epic: {epic_id}
            - Branch: {current_branch or f'feature/epic-{epic_id}'}
            - Base branch: origin/{self.base_branch}
            - Source: branch diff vs origin/{self.base_branch}

            Changed files:
            {branch_diff or "(none)"}

            Working tree status:
            {working_tree or "(clean)"}

            Review the branch diff first. If the tree is clean, review the latest commits on the branch.
            Do not ask for a diff source; use the context above.
            """
        ).strip() + "\n"

    def build_commit_split_prompt(self, epic_id: str) -> str:
        current_branch = self.run_text(["git", "branch", "--show-current"], cwd=self.project_root, check=False, capture_output=True).strip()
        working_tree = self.run_text(["git", "status", "--short"], cwd=self.project_root, check=False, capture_output=True).strip()
        return dedent(
            f"""
            $commit-split-workflow

            Context:
            - Epic: {epic_id}
            - Branch: {current_branch or f'feature/epic-{epic_id}'}
            - Goal: split the current story implementation into small, reviewable commits.

            Working tree status:
            {working_tree or "(clean)"}

            Use the repository's commit-message conventions and make the commit history easy to review.
            """
        ).strip() + "\n"

    def build_fix_issues_prompt(self, issues: str) -> str:
        return dedent(
            f"""
            Fix ONLY issues from CI failures and/or Copilot review feedback.

            Issues:
            {issues}

            Rules:
            - Do not introduce new features
            - Keep changes minimal
            - Fix each issue mentioned by Copilot
            - After fixes: git add -A && git commit -m "fix: address ci/review" && git push

            ## IMPORTANT: Generate a detailed reply for Copilot

            After fixing, you MUST generate a reply that addresses EACH point from Copilot's review.
            Format your reply EXACTLY like this (the REPLY_TO_COPILOT marker is required):

            REPLY_TO_COPILOT:
            ## Addressed Feedback

            | Copilot Suggestion | Action Taken |
            |-------------------|--------------|
            | [Quote or summarize first suggestion] | [What you did to fix it, include commit if relevant] |
            | [Quote or summarize second suggestion] | [What you did to fix it] |
            ...

            Additional notes: [Any other relevant context]

            END_REPLY

            At the end, output exactly:
            STATUS: FIXED
            """
        ).strip() + "\n"

    def build_retrospective_prompt(
        self,
        epic_id: str,
        story_files: list[Path],
        retro_file: Path,
        sprint_status_file: Path,
    ) -> str:
        return "$bmad-retrospective\n"

    # ------------------------------------------------------------------
    # Phase helpers
    # ------------------------------------------------------------------

    def autopilot_checks(self) -> bool:
        ok = True
        backend = self.project_root / "backend" / "Cargo.toml"
        frontend = self.project_root / "frontend" / "package.json"
        mobile = self.project_root / "mobile-native" / "gradlew"

        if backend.exists():
            self.run_process(["cargo", "fmt", "--check"], cwd=backend.parent)
            self.run_process(["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"], cwd=backend.parent)
            self.run_process(["cargo", "test", "--workspace"], cwd=backend.parent)

        if frontend.exists():
            package = json.loads(read_text(frontend, "{}"))
            scripts = (package.get("scripts") or {}) if isinstance(package, dict) else {}
            if "check" in scripts:
                self.run_process(["pnpm", "run", "check"], cwd=frontend.parent)
            if "typecheck" in scripts:
                self.run_process(["pnpm", "run", "typecheck"], cwd=frontend.parent)
            if "test" in scripts:
                self.run_process(["pnpm", "-r", "run", "test"], cwd=frontend.parent)

        if self.config.run_mobile_native and mobile.exists():
            self.run_process(["./gradlew", "build"], cwd=mobile.parent)

        return ok

    def phase_check_pending_pr(self) -> None:
        self.log("🔍 PHASE: CHECK_PENDING_PR")
        self.verbose("   Checking for open epic PRs...")

        open_epics = self.run_json(
            ["gh", "pr", "list", "--state", "open", "--json", "headRefName,number"],
            cwd=self.project_root,
            check=False,
        ) or []

        open_epic_branches = [
            f"{item['number']}:{item['headRefName']}"
            for item in open_epics
            if str(item.get("headRefName", "")).startswith("feature/epic-")
        ]
        self.verbose(f"   Found {len(open_epic_branches)} open epic PR(s)")

        if open_epic_branches:
            self.log(f"📋 Found {len(open_epic_branches)} open epic PR(s):")
            for pr_info in open_epic_branches:
                self.log(f"   - PR #{pr_info.split(':', 1)[0]} → {pr_info.split(':', 1)[1]}")

            first_pr_info = open_epic_branches[0]
            pr_number_str, open_epic_branch = first_pr_info.split(":", 1)
            self.log(f"⚠️ Resuming first open PR #{pr_number_str} ({open_epic_branch})")
            self.tmp_dir.joinpath("last_copilot_comment_id.txt").unlink(missing_ok=True)
            self.tmp_dir.joinpath("copilot.txt").unlink(missing_ok=True)
            self.tmp_dir.joinpath("copilot_latest.json").unlink(missing_ok=True)

            self.run_process(["git", "fetch", "origin", open_epic_branch], cwd=self.project_root, check=False)
            self.run_process(["git", "checkout", "-B", open_epic_branch, f"origin/{open_epic_branch}"], cwd=self.project_root, check=False)
            epic_id = open_epic_branch.removeprefix("feature/epic-")
            self.state_set(Phase.WAIT_COPILOT, epic_id)
            return

        current_branch = self.run_text(["git", "branch", "--show-current"], cwd=self.project_root, check=False, capture_output=True).strip()
        if current_branch.startswith("feature/epic-"):
            epic_id = current_branch.removeprefix("feature/epic-")
            self.log(f"Found feature branch: {current_branch} (epic: {epic_id})")
            pr_info = self.run_json(["gh", "pr", "view", "--json", "number,state"], cwd=self.project_root, check=False) or {}
            if pr_info:
                pr_number = int(pr_info.get("number", 0) or 0)
                pr_state = str(pr_info.get("state", ""))
                if pr_state == "OPEN":
                    self.log(f"⚠️ Found open PR #{pr_number} for epic {epic_id} - resuming PR flow")
                    self.tmp_dir.joinpath("last_copilot_comment_id.txt").unlink(missing_ok=True)
                    self.state_set(Phase.WAIT_COPILOT, epic_id)
                    return
                if pr_state == "MERGED":
                    self.log(f"✅ PR #{pr_number} was already merged")
                    self.sync_base_branch()
                    new_commits = self.run_text(["git", "log", f"origin/{self.base_branch}..HEAD", "--oneline"], cwd=self.project_root, check=False)
                    if new_commits.strip():
                        self.log(f"📝 Found new commit(s) since merge - will create new PR")
                        self.state_set(Phase.CODE_REVIEW, epic_id)
                        return
                    self.run_process(["git", "checkout", self.base_branch], cwd=self.project_root, check=False)
                    self.run_process(["git", "pull", "origin", self.base_branch], cwd=self.project_root, check=False)
                if pr_state == "CLOSED":
                    self.log(f"⚠️ PR #{pr_number} was closed (not merged)")
                    diff_names = self.run_text(["git", "diff", f"origin/{self.base_branch}..HEAD", "--name-only"], cwd=self.project_root, check=False)
                    if diff_names.strip():
                        self.log("📝 Branch has changes - will create new PR")
                        self.state_set(Phase.CODE_REVIEW, epic_id)
                        return
            else:
                self.log("Branch exists but no PR - checking if we need to create one")
                self.run_process(["git", "fetch", "origin", current_branch], cwd=self.project_root, check=False)
                self.run_process(["git", "fetch", "origin", self.base_branch], cwd=self.project_root, check=False)
                has_unpushed = self.run_text(["git", "log", f"origin/{current_branch}..HEAD", "--oneline"], cwd=self.project_root, check=False).strip()
                has_diff = self.run_text(["git", "diff", f"origin/{self.base_branch}..HEAD", "--name-only"], cwd=self.project_root, check=False).strip()
                if has_unpushed or has_diff:
                    self.log("Found unpushed changes - resuming from CODE_REVIEW")
                    self.state_set(Phase.CODE_REVIEW, epic_id)
                    return

        self.log("✅ No pending PRs found - proceeding to find next epic")
        self.state_set(Phase.FIND_EPIC, None)

    def phase_find_epic(self) -> None:
        self.log("📋 PHASE: FIND_EPIC")
        try:
            sprint_status = self.load_sprint_status()
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, None)
            return

        if self.config.parallel_mode >= 1 and self.state_count_pending_prs() >= self.config.max_pending_prs:
            self.log("⏸️ Pending PR cap reached - waiting for review/merge before starting a new epic")
            self.state_set(Phase.CHECK_PENDING_PR, None)
            return

        try:
            next_epic = self.find_next_epic(sprint_status)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, None)
            return
        if next_epic:
            self.log(f"✅ Found epic: {next_epic}")
            self.state_set(Phase.CREATE_BRANCH, next_epic)
            return

        if self.state_count_pending_prs() > 0:
            self.log("🕒 No new epics found yet, but pending PRs still need review/merge")
            self.state_set(Phase.CHECK_PENDING_PR, None)
            return

        self.log("🎉 No more active epics in sprint-status.yaml and no pending PRs - ALL DONE!")
        self.state_set(Phase.DONE, None)

    def phase_create_branch(self) -> None:
        self.log("🌿 PHASE: CREATE_BRANCH")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        branch_name = f"feature/epic-{epic_id}"
        self.log(f"Creating branch: {branch_name}")
        self.run_process(["git", "fetch", "origin"], cwd=self.project_root, check=False)
        self.run_process(["git", "checkout", self.base_branch], cwd=self.project_root, check=False)
        self.run_process(["git", "pull", "origin", self.base_branch], cwd=self.project_root, check=False)
        self.run_process(["git", "checkout", "-b", branch_name], cwd=self.project_root, check=False)
        self.run_process(["git", "push", "-u", "origin", branch_name], cwd=self.project_root, check=False)
        self.state_set(Phase.DEVELOP_STORIES, epic_id)
        self.log(f"✅ Branch ready: {branch_name}")

    def phase_develop_stories(self) -> None:
        self.log("💻 PHASE: DEVELOP_STORIES")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        if self.run_text(["git", "status", "--porcelain"], cwd=self.project_root, check=False).strip():
            self.log("⚠️ Git working tree not clean - committing pending changes first")
            self.run_process(["git", "add", "-A"], cwd=self.project_root, check=False)
            self.run_process(["git", "commit", "-m", "chore: auto-commit before story development"], cwd=self.project_root, check=False)

        try:
            sprint_status = self.load_sprint_status()
            story_files = sprint_status.story_files_for_epic(self.project_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return
        self.log(f"📄 Sprint status source: {self.sprint_status_file}")
        for story_file in story_files:
            self.log(f"📄 Story context: {story_file}")

        output_file = self.tmp_dir / "develop-stories-output.txt"
        return_code = self.run_codex_exec(self.build_dev_story_prompt(epic_id, sprint_status, story_files), output_file, cwd=self.project_root)
        output_text = read_text(output_file)
        if return_code != 0 or "STATUS: STORIES_BLOCKED" in output_text:
            self.log("❌ Codex reported stories blocked")
            self.state_set(Phase.BLOCKED, epic_id)
            return
        if "STATUS: STORIES_COMPLETE" not in output_text:
            self.log("⚠️ Codex did not report STORIES_COMPLETE; continuing cautiously")

        self.log("Running local checks gate...")
        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after story development: {exc}")

        self.state_set(Phase.COMMIT_SPLIT, epic_id)
        self.log("✅ Stories phase complete; running commit split workflow next")

    def phase_commit_split(self) -> None:
        self.log("🪓 PHASE: COMMIT_SPLIT")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        output_file = self.tmp_dir / "commit-split-output.txt"
        return_code = self.run_codex_exec(
            self.build_commit_split_prompt(epic_id),
            output_file,
            cwd=self.project_root,
            model=self.codex_model,
            reasoning_effort=self.commit_split_reasoning_effort,
        )
        output_text = read_text(output_file)
        if return_code != 0:
            self.log("❌ Codex reported commit split failed")
            if output_text.strip():
                self.verbose(output_text.strip())
            self.state_set(Phase.BLOCKED, epic_id)
            return

        self.log("✅ Commit split workflow complete")
        self.state_set(Phase.QA_AUTOMATION_TEST, epic_id)

    def phase_qa_automation_test(self) -> None:
        self.log("🧪 PHASE: QA_AUTOMATION_TEST")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        try:
            sprint_status = self.load_sprint_status()
            story_files = sprint_status.story_files_for_epic(self.project_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return
        self.log(f"📄 Sprint status source: {self.sprint_status_file}")
        for story_file in story_files:
            self.log(f"📄 Story context: {story_file}")

        output_file = self.tmp_dir / "qa-automation-output.txt"
        return_code = self.run_codex_exec(self.build_qa_prompt(epic_id, sprint_status, story_files), output_file, cwd=self.project_root)
        output_text = read_text(output_file)
        if return_code != 0 or "STATUS: QA_BLOCKED" in output_text:
            self.log("❌ Codex reported QA blocked")
            self.state_set(Phase.BLOCKED, epic_id)
            return
        if "STATUS: QA_COMPLETE" not in output_text:
            self.log("⚠️ Codex did not report QA_COMPLETE; continuing cautiously")

        self.log("Running local checks gate...")
        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after QA automation: {exc}")

        self.state_set(Phase.CODE_REVIEW, epic_id)
        self.log("✅ QA automation complete")

    def phase_code_review(self) -> None:
        self.log("🔍 PHASE: CODE_REVIEW")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        self.log(f"Running BMAD code-review workflow for epic {epic_id}")
        output_file = self.tmp_dir / "code-review-output.txt"
        return_code = self.run_codex_exec(self.build_code_review_prompt(epic_id), output_file, cwd=self.project_root)
        output_text = read_text(output_file)
        if return_code != 0 or "STATUS: CODE_REVIEW_DONE" not in output_text:
            self.log("⚠️ Codex did not report CODE_REVIEW_DONE cleanly")

        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after code review: {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        self.run_process(["git", "push"], cwd=self.project_root, check=False)
        self.state_set(Phase.CREATE_PR, epic_id)
        self.log("✅ Code review passed")

    def phase_create_pr(self) -> None:
        self.log("📝 PHASE: CREATE_PR")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        pr_number = 0
        pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=self.project_root, check=False)
        if isinstance(pr_view, dict) and pr_view.get("number"):
            pr_number = int(pr_view["number"])
        else:
            create_command = [
                "gh",
                "pr",
                "create",
                "--fill",
                "--label",
                "epic",
                "--label",
                "automated",
                "--label",
                f"epic-{epic_id}",
            ]
            create_result = self.run_process(create_command, cwd=self.project_root, check=False)
            if create_result.returncode != 0:
                self.run_process(["gh", "pr", "create", "--fill"], cwd=self.project_root, check=False)
            pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=self.project_root, check=False) or {}
            pr_number = int(pr_view.get("number", 0) or 0)

        if pr_number <= 0:
            self.log("❌ Could not determine PR number after creation")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        wt_path = self.worktree_path(epic_id)
        self.state_add_pending_pr(epic_id, pr_number, str(wt_path))
        self.log(f"✅ PR #{pr_number} created for epic {epic_id}, added to pending list")

        self.sync_base_branch()
        self.log("🔄 PR created, starting next epic (PR review runs in background)...")
        self.state_set(Phase.FIND_EPIC, None)

    def phase_wait_copilot(self) -> None:
        self.log("🤖 PHASE: WAIT_COPILOT")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=self.project_root, check=False) or {}
        pr_number = int(pr_view.get("number", 0) or 0)
        if not pr_number:
            self.log("❌ Could not get PR number - PR may not exist")
            self.state_set(Phase.BLOCKED, epic_id)
            return
        self.log(f"Waiting for GitHub Copilot review on PR #{pr_number}")

        last_processed_id = ""
        last_id_path = self.tmp_dir / "last_copilot_comment_id.txt"
        if last_id_path.exists():
            last_processed_id = read_text(last_id_path).strip()
            self.log(f"Last processed Copilot ID: {last_processed_id}")

        max_wait = self.config.max_copilot_wait
        for i in range(1, max_wait + 1):
            if i == 1:
                self.debug("Fetching all comments/reviews authors...")
                comments_reviews = self.run_json(["gh", "pr", "view", "--json", "comments,reviews"], cwd=self.project_root, check=False) or {}
                authors = [c.get("author", {}).get("login", "") for c in comments_reviews.get("comments", [])]
                authors += [f"{r.get('author', {}).get('login', '')}({r.get('state', '')})" for r in comments_reviews.get("reviews", [])]
                self.debug("Comments: " + ", ".join(authors))

            payload = self.run_json(["gh", "pr", "view", "--json", "comments,reviews"], cwd=self.project_root, check=False) or {}
            items: list[dict[str, Any]] = []
            for comment in payload.get("comments", []) or []:
                author = str(comment.get("author", {}).get("login", ""))
                if "copilot" in author.lower():
                    items.append(
                        {
                            "id": comment.get("id"),
                            "body": comment.get("body", ""),
                            "createdAt": comment.get("createdAt") or "",
                            "type": "comment",
                            "author": author,
                        }
                    )
            for review in payload.get("reviews", []) or []:
                author = str(review.get("author", {}).get("login", ""))
                if "copilot" in author.lower():
                    items.append(
                        {
                            "id": review.get("id"),
                            "body": review.get("body", ""),
                            "createdAt": review.get("submittedAt") or review.get("createdAt") or review.get("updatedAt") or "",
                            "type": "review",
                            "state": review.get("state", ""),
                            "author": author,
                        }
                    )
            items.sort(key=lambda item: item.get("createdAt") or "")

            if not items:
                self.verbose(f"   Iteration {i}/{max_wait}: No Copilot review yet, waiting {self.config.check_interval}s...")
                self.log(f"… waiting for Copilot to review ({i}/{max_wait})")
                time.sleep(self.config.check_interval)
                continue

            latest = items[-1]
            latest_id = str(latest.get("id") or "")
            latest_type = str(latest.get("type") or "")
            latest_author = str(latest.get("author") or "")
            if latest_id == last_processed_id:
                self.verbose(f"   Iteration {i}/{max_wait}: Already processed {latest_id}, waiting {self.config.check_interval}s...")
                self.log(f"… waiting for NEW Copilot activity (already processed {latest_id}) ({i}/{max_wait})")
                time.sleep(self.config.check_interval)
                continue

            self.log(f"✅ Copilot ({latest_author}) has posted a new {latest_type} (ID: {latest_id})")
            write_text(self.tmp_dir / "copilot.txt", str(latest.get("body", "")))
            write_text(last_id_path, latest_id)
            review_state = str(latest.get("state", ""))
            if review_state == "CHANGES_REQUESTED":
                self.log("⚠️ Copilot REQUESTED CHANGES")
                self.state_set(Phase.FIX_ISSUES, epic_id)
                return

            unresolved_count = self.count_unresolved_threads(pr_number)
            if unresolved_count > 0:
                self.log(f"⚠️ Found {unresolved_count} unresolved review thread(s) - need to fix")
                self.state_set(Phase.FIX_ISSUES, epic_id)
                return

            self.log("✅ Copilot review complete, no issues found")
            self.log("🔄 Adding PR to pending list, continuing to next epic...")
            wt_path = self.worktree_path(epic_id)
            self.state_add_pending_pr(epic_id, pr_number, str(wt_path))
            self.sync_base_branch()
            self.state_set(Phase.FIND_EPIC, None)
            return

        self.log(f"⚠️ Timeout waiting for Copilot review ({max_wait} iterations)")
        self.state_set(Phase.BLOCKED, epic_id)

    def phase_wait_checks(self) -> None:
        self.log("⏳ PHASE: WAIT_CHECKS (deprecated)")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.state_set(Phase.FIND_EPIC, None)
            return

        pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=self.project_root, check=False) or {}
        pr_number = int(pr_view.get("number", 0) or 0)
        if pr_number:
            wt_path = self.worktree_path(epic_id)
            self.state_add_pending_pr(epic_id, pr_number, str(wt_path))
            self.log(f"🔄 PR #{pr_number} added to pending list")

        self.sync_base_branch()
        self.state_set(Phase.FIND_EPIC, None)
        self.log("🔄 Continuing to next epic (auto-approve handles PR in background)")

    def phase_fix_issues(self) -> None:
        self.log("🔧 PHASE: FIX_ISSUES")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=self.project_root, check=False) or {}
        pr_number = int(pr_view.get("number", 0) or 0)
        if not pr_number:
            self.log("❌ Could not get PR number - PR may not exist")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        self.log("🔍 Fetching unresolved review threads...")
        threads_content = self.get_unresolved_threads_content(pr_number)
        issues_parts: list[str] = []
        has_copilot_issues = False
        if threads_content.strip():
            issues_parts.append(f"UNRESOLVED REVIEW THREADS:\n{threads_content}")
            has_copilot_issues = True

        copilot_file = self.tmp_dir / "copilot.txt"
        if copilot_file.exists() and copilot_file.read_text(encoding="utf-8").strip():
            issues_parts.append(f"COPILOT REVIEW BODY:\n{copilot_file.read_text(encoding='utf-8')}")
            has_copilot_issues = True

        failed_checks_file = self.tmp_dir / "failed-checks.json"
        if failed_checks_file.exists() and failed_checks_file.read_text(encoding="utf-8").strip():
            issues_parts.append(f"CI FAILURES:\n{failed_checks_file.read_text(encoding='utf-8')}")

        issues = "\n\n".join(issues_parts)
        output_file = self.tmp_dir / "fix-issues-output.txt"

        worktree = self.state_get_pending_pr(epic_id)
        cwd = Path(worktree.worktree) if worktree and Path(worktree.worktree).exists() else self.project_root
        return_code = self.run_codex_exec(self.build_fix_issues_prompt(issues), output_file, cwd=cwd)
        output_text = read_text(output_file)

        if return_code != 0 or "STATUS: FIXED" not in output_text:
            self.log("⚠️ Codex did not report FIXED cleanly")

        try:
            self.autopilot_checks()
        except Exception:
            pass

        if has_copilot_issues:
            self.log("Posting detailed reply to Copilot review...")
            reply_text = ""
            if "REPLY_TO_COPILOT:" in output_text:
                reply_section = output_text.split("REPLY_TO_COPILOT:", 1)[1]
                reply_section = reply_section.split("END_REPLY", 1)[0]
                reply_section = reply_section.split("STATUS: FIXED", 1)[0]
                reply_text = "\n".join(line for line in reply_section.splitlines() if line.strip())[:5000]

            if not reply_text.strip() or len(reply_text.split()) < 5:
                reply_text = (
                    "## ✅ Addressed Copilot Review Feedback\n\n"
                    "Thank you @copilot for the review! I've addressed the suggestions in the latest commit(s).\n\n"
                    "**Summary of changes:**\n"
                    "- Reviewed and fixed all actionable items from your feedback\n"
                    "- Ran local checks to verify the fixes\n\n"
                    "Please re-review when ready. 🙏"
                )
            else:
                reply_text = "## ✅ Addressed Copilot Review Feedback\n\n@copilot - Thank you for the review! Here's what I fixed:\n\n" + reply_text

            self.run_process(["gh", "pr", "comment", str(pr_number), "--body", reply_text], cwd=self.project_root, check=False)
            self.log("✅ Posted detailed reply to Copilot review")

        self.log("🔧 Resolving review threads...")
        self.resolve_pr_review_threads(pr_number)
        failed_checks_file.unlink(missing_ok=True)
        self.state_set(Phase.WAIT_COPILOT, epic_id)
        self.log("✅ Issues fixed, waiting for Copilot to re-review")

    def run_retrospective_for_epic(self, epic_id: str) -> bool:
        sprint_status_file = self.sprint_status_file
        retro_dir = self.project_root / "_bmad-output" / "implementation-artifacts"
        retro_dir.mkdir(parents=True, exist_ok=True)
        retro_file = retro_dir / f"epic-{epic_id}-retro-{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        output_file = self.tmp_dir / "retrospective-output.txt"
        self.log(f"🪞 Running retrospective for epic {epic_id}")
        try:
            sprint_status = self.load_sprint_status()
            story_files = sprint_status.story_files_for_epic(self.project_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            return False
        return_code = self.run_codex_exec(
            self.build_retrospective_prompt(epic_id, story_files, retro_file, sprint_status_file),
            output_file,
            cwd=self.project_root,
        )
        output_text = read_text(output_file)
        if return_code != 0 or "STATUS: RETROSPECTIVE_COMPLETE" not in output_text:
            self.log("⚠️ Codex did not report RETROSPECTIVE_COMPLETE cleanly")
            return False
        self.log(f"✅ Retrospective saved: {retro_file}")
        return True

    def phase_merge_pr(self) -> None:
        self.log("🔀 PHASE: MERGE_PR")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=self.project_root, check=False) or {}
        pr_number = int(pr_view.get("number", 0) or 0)
        if not pr_number:
            self.log("❌ Could not get PR number - PR may not exist")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        merge_result = self.run_process(["gh", "pr", "merge", "--squash", "--delete-branch"], cwd=self.project_root, check=False)
        if merge_result.returncode != 0:
            self.log("❌ Failed to merge PR - may need manual intervention")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        self.sync_base_branch()
        self.log("Running post-merge checks...")
        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Post-merge checks failed: {exc}")
        self.log(f"✅ PR #{pr_number} merged successfully")
        self.tmp_dir.joinpath("last_copilot_comment_id.txt").unlink(missing_ok=True)
        self.tmp_dir.joinpath("copilot.txt").unlink(missing_ok=True)
        self.tmp_dir.joinpath("copilot_latest.json").unlink(missing_ok=True)
        self.state_mark_completed(epic_id)
        self.run_retrospective_for_epic(epic_id)
        if self.config.parallel_mode >= 1:
            self.worktree_remove(epic_id)
            self.state_remove_pending_pr(epic_id)
        self.state_set(Phase.CHECK_PENDING_PR, None)
        self.log(f"✅ Epic merged and marked completed: {epic_id}")

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------

    def phase_dispatch(self) -> None:
        phase = self.state_phase()
        if phase == Phase.CHECK_PENDING_PR:
            self.phase_check_pending_pr()
        elif phase == Phase.FIND_EPIC:
            self.phase_find_epic()
        elif phase == Phase.CREATE_BRANCH:
            self.phase_create_branch()
        elif phase == Phase.DEVELOP_STORIES:
            self.phase_develop_stories()
        elif phase == Phase.COMMIT_SPLIT:
            self.phase_commit_split()
        elif phase == Phase.QA_AUTOMATION_TEST:
            self.phase_qa_automation_test()
        elif phase == Phase.CODE_REVIEW:
            self.phase_code_review()
        elif phase == Phase.CREATE_PR:
            self.phase_create_pr()
        elif phase == Phase.WAIT_COPILOT:
            self.phase_wait_copilot()
        elif phase == Phase.WAIT_CHECKS:
            self.phase_wait_checks()
        elif phase == Phase.FIX_ISSUES:
            self.phase_fix_issues()
        elif phase == Phase.MERGE_PR:
            self.phase_merge_pr()
        elif phase == Phase.BLOCKED:
            self.log("⚠️ BLOCKED - manual intervention needed")
            self.log(f"Fix manually then resume with: {Path(sys.argv[0]).name} \"{self.config.epic_pattern}\" --continue")
            raise SystemExit(1)
        elif phase == Phase.DONE:
            self.log("🎉 ALL EPICS COMPLETED!")
            completed = ", ".join(self.state.completed_epics)
            self.log(f"Completed epics: {completed}")
            if self.config.parallel_mode >= 1:
                self.worktree_prune()
            raise SystemExit(0)
        else:
            self.log(f"❌ Unknown phase: {phase}")
            raise SystemExit(1)

    def run(self) -> None:
        self.require_tooling()
        self.ensure_state_file()

        if self.config.verbose_mode:
            self.log("📋 Configuration:")
            self.log(f"   ROOT_DIR: {self.project_root}")
            self.log(f"   BASE_BRANCH: {self.base_branch}")
            self.log(f"   MAX_TURNS: {self.config.max_turns}")
            self.log(f"   CHECK_INTERVAL: {self.config.check_interval}s")
            self.log(f"   MAX_CHECK_WAIT: {self.config.max_check_wait} iterations")
            self.log(f"   MAX_COPILOT_WAIT: {self.config.max_copilot_wait} iterations")
            self.log(f"   PARALLEL_MODE: {self.config.parallel_mode}")
            self.log(f"   MAX_PENDING_PRS: {self.config.max_pending_prs}")
            self.log(f"   PARALLEL_CHECK_INTERVAL: {self.config.parallel_check_interval}s")
            self.log(f"   DEBUG_MODE: {int(self.config.debug_mode)}")
            self.log("")

        dirty = self.run_text(["git", "status", "--porcelain"], cwd=self.project_root, check=False).strip()
        if dirty:
            self.log("⚠️ WARNING: Git working tree has uncommitted changes")
            self.log("   Autopilot may checkout branches which could cause conflicts.")
            self.log("   Consider committing or stashing your changes first.")
            print("")
            answer = input("Continue anyway? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                self.log("Aborted by user.")
                raise SystemExit(1)
            self.log("Continuing with dirty working tree (user confirmed)...")

        if not self.config.epic_pattern:
            self.log("ℹ️ No epic pattern provided - will process ALL active epics from sprint-status.yaml in order")

        if self.config.parallel_mode >= 1:
            self.log(f"🔀 PARALLEL MODE enabled (max {self.config.max_pending_prs} concurrent PRs)")

        if not self.config.continue_run or not self.state_file.exists():
            self.log("🚀 BMAD Autopilot starting (fresh)")
            self.state = AutopilotState.initial(self.config.parallel_mode >= 1)
            self.state_set(Phase.CHECK_PENDING_PR, None)
        else:
            self.log("🚀 BMAD Autopilot resuming (--continue)")

        last_pending_check = 0.0
        while True:
            phase = self.state_phase()
            self.log(f"━━━ Current phase: {phase.value} ━━━")

            now = time.time()
            if now - last_pending_check >= self.config.parallel_check_interval:
                last_pending_check = now
                pending_count = self.state_count_pending_prs()
                if pending_count > 0:
                    self.debug(f"Periodic check: {pending_count} pending PR(s)")
                    pr_to_fix = self.check_all_pending_prs()
                    if pr_to_fix:
                        self.log(f"🔧 PR for epic {pr_to_fix} needs fixes, pausing...")
                        self.fix_pending_pr_issues(pr_to_fix)

            self.phase_dispatch()
            time.sleep(2)

    # ------------------------------------------------------------------
    # Backfill support methods
    # ------------------------------------------------------------------

    def fix_pending_pr_issues(self, epic_id: str) -> None:
        pr_info = self.state_get_pending_pr(epic_id)
        if not pr_info:
            self.log(f"❌ No pending PR found for epic {epic_id}")
            return

        pr_number = pr_info.pr_number
        wt_path = Path(pr_info.worktree)
        branch_name = f"feature/epic-{epic_id}"

        self.log(f"🔧 Fixing issues in PR #{pr_number} (epic {epic_id})")
        self.state_save_active_context()

        if not wt_path.exists():
            self.log(f"🌳 Creating worktree for {epic_id}...")
            self.worktree_dir.mkdir(parents=True, exist_ok=True)
            self.run_process(["git", "fetch", "origin", branch_name], cwd=self.project_root, check=False)
            result = self.run_process(["git", "worktree", "add", str(wt_path), branch_name], cwd=self.project_root, check=False)
            if result.returncode != 0:
                self.log(f"❌ Failed to create worktree for {branch_name}")
                self.state_restore_active_context()
                return

        if wt_path.exists():
            old_cwd = Path.cwd()
            try:
                os.chdir(wt_path)
                ci_failures = ""
                checks = self.gh_pr_checks(pr_number)
                failures = [check for check in checks if str(check.get("conclusion", "")).lower() == "failure"]
                if failures:
                    ci_failures = json.dumps(failures, indent=2)

                copilot_feedback = ""
                reviews = self.gh_pr_view(pr_number, "reviews") or {}
                review_rows = reviews.get("reviews", []) or []
                copilot_rows = [review for review in review_rows if "copilot" in str(review.get("author", {}).get("login", "")).lower()]
                if copilot_rows:
                    copilot_feedback = str(copilot_rows[-1].get("body", "") or "")

                issues_parts = []
                if copilot_feedback:
                    issues_parts.append(f"COPILOT REVIEW:\n{copilot_feedback}")
                if ci_failures:
                    issues_parts.append(f"CI FAILURES:\n{ci_failures}")
                issues = "\n\n".join(issues_parts)

                output_file = self.tmp_dir / "fix-pr-output.txt"
                self.log("🤖 Running Codex to fix PR issues...")
                return_code = self.run_codex_exec(self.build_fix_issues_prompt(issues), output_file, cwd=wt_path)
                output_text = read_text(output_file)
                if return_code != 0 or "STATUS: FIXED" not in output_text:
                    self.log("⚠️ Codex did not report FIXED cleanly while fixing pending PR")

                self.state_update_pending_pr(epic_id, "status", "WAIT_REVIEW")
                self.state_update_pending_pr(epic_id, "last_check", utc_now())
                self.log(f"✅ Fixes applied to PR #{pr_number}")
            finally:
                os.chdir(old_cwd)

        self.state_restore_active_context()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BMAD Autopilot")
    parser.add_argument("epic_pattern", nargs="?", default="")
    parser.add_argument("--continue", dest="continue_run", action="store_true")
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
