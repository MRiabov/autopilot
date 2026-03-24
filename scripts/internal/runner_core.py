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


class AutopilotRunner:
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

    def default_worktree_dir(self) -> Path:
        repo_digest = hashlib.sha1(str(self.project_root).encode("utf-8")).hexdigest()[:10]
        return Path(tempfile.gettempdir()) / "bmad-autopilot" / f"{self.project_root.name}-{repo_digest}"

    def sprint_status_path(self, root: Path | None = None) -> Path:
        base_root = root or self.project_root
        return base_root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"

    def epic_workspace_root(self, epic_id: str | None = None) -> Path:
        if epic_id:
            pending = self.state_get_pending_pr(epic_id)
            if pending and pending.worktree:
                pending_root = Path(pending.worktree)
                if pending_root.exists():
                    return pending_root
            wt_path = self.worktree_path(epic_id)
            if wt_path.exists():
                return wt_path

        if self.state.active_worktree:
            active_root = Path(self.state.active_worktree)
            if active_root.exists():
                return active_root

        return self.project_root

    def confirm_dirty_worktree(self, root: Path, *, context: str) -> None:
        dirty = self.run_text(["git", "status", "--short"], cwd=root, check=False, capture_output=True).strip()
        if not dirty:
            return
        if self.config.accept_dirty_worktree:
            self.log("⚠️ WARNING: Git working tree has uncommitted changes")
            self.log(f"   Context: {context}")
            self.log(f"   Root: {root}")
            self.log("   Dirty worktree accepted via --accept-dirty-worktree.")
            return

        self.log("⚠️ WARNING: Git working tree has uncommitted changes")
        self.log(f"   Context: {context}")
        self.log(f"   Root: {root}")
        self.log("   Autopilot will continue only after explicit confirmation.")
        print("")
        try:
            answer = input("Continue anyway? [y/N] ").strip().lower()
        except EOFError:
            self.log("Aborted: dirty working tree requires explicit yes/no confirmation.")
            raise SystemExit(1)
        if answer not in {"y", "yes"}:
            self.log("Aborted by user.")
            raise SystemExit(1)
        self.log("Continuing with dirty working tree (user confirmed)...")

    def collect_review_source_snapshot(self, repo_root: Path) -> ReviewSourceSnapshot:
        base_branch = getattr(self, "base_branch", None) or self.detect_base_branch()

        def filter_internal_paths(text: str) -> str:
            ignored_prefixes = (
                ".autopilot/tmp/",
                ".autopilot/state.json",
                ".autopilot/autopilot.log",
                "_bmad-outputs/review-artifacts/",
            )
            kept_lines = []
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if any(prefix in line for prefix in ignored_prefixes):
                    continue
                kept_lines.append(line)
            return "\n".join(kept_lines)

        current_branch = self.run_text(["git", "branch", "--show-current"], cwd=repo_root, check=False, capture_output=True).strip()
        branch_diff = filter_internal_paths(
            self.run_text(
                ["git", "diff", "--name-only", f"origin/{base_branch}..HEAD"],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
        )
        staged_diff = filter_internal_paths(
            self.run_text(
                ["git", "diff", "--name-only", "--cached"],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
        )
        unstaged_diff = filter_internal_paths(
            self.run_text(
                ["git", "diff", "--name-only"],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
        )
        working_tree_status = filter_internal_paths(
            self.run_text(["git", "status", "--short"], cwd=repo_root, check=False, capture_output=True)
        )
        working_tree_status = "\n".join(
            line
            for line in working_tree_status.splitlines()
            if not line.startswith("?? .autopilot/")
            and not line.startswith("?? .autopilot")
        )
        has_reviewable_source = bool(branch_diff.strip() or staged_diff.strip() or unstaged_diff.strip())
        return ReviewSourceSnapshot(
            current_branch=current_branch,
            branch_diff=branch_diff,
            staged_diff=staged_diff,
            unstaged_diff=unstaged_diff,
            working_tree_status=working_tree_status,
            has_reviewable_source=has_reviewable_source,
        )

    def mirror_worktree_support_dirs(self, wt_path: Path) -> None:
        for rel_path in self.worktree_mirror_paths:
            source = self.project_root / rel_path
            if not source.exists():
                continue

            destination = wt_path / rel_path
            destination.parent.mkdir(parents=True, exist_ok=True)

            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or destination.is_file():
                    destination.unlink()
                else:
                    shutil.rmtree(destination)

            try:
                os.symlink(source, destination, target_is_directory=source.is_dir())
            except OSError:
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, destination)

    def set_active_worktree(self, worktree: Path | None) -> None:
        self.state.active_worktree = str(worktree) if worktree else None
        self.save_state()

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
            start_from=self.args.start_from or "",
            flow_mode=env_or_file("AUTOPILOT_FLOW", "auto").strip().lower() or "auto",
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
            codex_switch_mode=env_or_file("AUTOPILOT_CODEX_SWITCH_MODE", "auto").strip().lower() or "auto",
            codex_switch_primary_threshold=self.to_int(
                env_or_file("AUTOPILOT_CODEX_SWITCH_PRIMARY_THRESHOLD", "20"),
                20,
            ),
            codex_switch_secondary_threshold=self.to_int(
                env_or_file("AUTOPILOT_CODEX_SWITCH_SECONDARY_THRESHOLD", "20"),
                20,
            ),
            cockpit_data_dir=env_or_file("AUTOPILOT_COCKPIT_DATA_DIR", ""),
            accept_dirty_worktree=bool(self.args.accept_dirty_worktree),
            quota_retry_seconds=self.to_int(
                env_or_file(
                    "AUTOPILOT_QUOTA_RETRY_SECONDS",
                    env_or_file("AUTOPILOT_DEVELOPMENT_BLOCKED_RETRY_SECONDS", "1800"),
                ),
                1800,
            ),
        )

    def resolve_flow_mode(self) -> str:
        flow = (self.config.flow_mode or "auto").strip().lower()
        if flow in {"story", "legacy"}:
            return flow
        if not self.sprint_status_file.exists():
            return "legacy"
        sprint_status = self.load_sprint_status()
        return "story" if sprint_status.story_entries() else "legacy"

    def is_story_flow(self) -> bool:
        return self.flow_mode == "story"

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
        required = ["git", "codex", "python3"]
        if not self.is_story_flow():
            required.append("gh")
        for cmd in required:
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
            self.verbose(self.format_prompt_preview(input_text))

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

    def _sound_path(self, sound_name: str) -> Path:
        return self.tmp_dir / "sounds" / f"{sound_name}.wav"

    def _synthesize_sound(self, sound_name: str) -> Path:
        notes = self.sound_profiles[sound_name]
        output_path = self._sound_path(sound_name)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        sample_rate = 44100
        amplitude = 0.95
        gap_seconds = 0.04
        attack_seconds = 0.008
        release_seconds = 0.012

        frames = bytearray()
        for frequency, duration in notes:
            note_samples = max(1, int(sample_rate * duration))
            attack_samples = max(1, min(note_samples // 4, int(sample_rate * attack_seconds)))
            release_samples = max(1, min(note_samples // 4, int(sample_rate * release_seconds)))

            for sample_index in range(note_samples):
                if sample_index < attack_samples:
                    envelope = sample_index / attack_samples
                elif sample_index >= note_samples - release_samples:
                    envelope = max(0.0, (note_samples - sample_index) / release_samples)
                else:
                    envelope = 1.0

                sample = int(
                    32767
                    * amplitude
                    * envelope
                    * math.sin(2.0 * math.pi * frequency * (sample_index / sample_rate))
                )
                frames.extend(sample.to_bytes(2, byteorder="little", signed=True))

            gap_samples = int(sample_rate * gap_seconds)
            for _ in range(gap_samples):
                frames.extend((0).to_bytes(2, byteorder="little", signed=True))

        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(bytes(frames))

        return output_path

    def _sound_player_command(self, sound_path: Path) -> list[str] | None:
        if shutil.which("paplay"):
            return ["paplay", str(sound_path)]
        if shutil.which("aplay"):
            return ["aplay", "-q", str(sound_path)]
        if shutil.which("afplay"):
            return ["afplay", str(sound_path)]
        if shutil.which("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(sound_path)]
        if shutil.which("play"):
            return ["play", "-q", str(sound_path)]
        return None

    def play_sound(self, sound_name: str) -> None:
        if sound_name not in self.sound_profiles:
            return
        try:
            sound_path = self._synthesize_sound(sound_name)
            command = self._sound_player_command(sound_path)
            if not command:
                return
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.verbose(f"⚠️ Sound notification failed for {sound_name}: {exc}")

    @staticmethod
    def _looks_like_quota_exhaustion(output_text: str) -> bool:
        text = output_text.lower()
        patterns = (
            r"\bquota\b",
            r"out of credits",
            r"insufficient credits?",
            r"rate limit exceeded",
            r"billing",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def run_codex_exec(
        self,
        prompt: str,
        output_file: Path | None = None,
        *,
        cwd: Path | None = None,
        reasoning_effort: str | None = None,
    ) -> int:
        return self.run_codex_session(
            prompt,
            output_file=output_file,
            cwd=cwd,
            reasoning_effort=reasoning_effort,
        ).return_code

    @staticmethod
    def format_codex_event(event: Any) -> str | None:
        if not isinstance(event, dict):
            return None

        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            return f"thread.started: {thread_id}" if thread_id else "thread.started"
        if event_type == "turn.started":
            return "turn.started"
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                parts = []
                for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                    if key in usage:
                        parts.append(f"{key}={usage[key]}")
                if parts:
                    return "turn.completed: " + ", ".join(parts)
            return "turn.completed"
        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type") or "item"
                item_id = item.get("id")
                text = item.get("text")
                if item_id and text:
                    preview = str(text).strip().replace("\n", " ")
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    return f"item.completed: {item_type} {item_id} {preview}"
                if item_id:
                    return f"item.completed: {item_type} {item_id}"
                return f"item.completed: {item_type}"
            return "item.completed"
        return None

    @staticmethod
    def format_prompt_preview(prompt: str, *, max_lines: int = 16, max_line_width: int = 120) -> str:
        lines = prompt.splitlines()
        preview_lines = lines[:max_lines]
        rendered = ["Prompt preview:"]
        for index, line in enumerate(preview_lines, start=1):
            clipped = line if len(line) <= max_line_width else line[: max_line_width - 3] + "..."
            rendered.append(f"  {index:02d}| {clipped}")
        if len(lines) > max_lines:
            rendered.append(f"  ... ({len(lines) - max_lines} more line(s))")
        return "\n".join(rendered)

    def run_codex_session(
        self,
        prompt: str,
        output_file: Path | None = None,
        *,
        cwd: Path | None = None,
        reasoning_effort: str | None = None,
        session_id: str | None = None,
    ) -> CodexAttemptResult:
        selected_reasoning_effort = reasoning_effort or self.codex_reasoning_effort
        effective_output_file = output_file or (self.tmp_dir / "codex-output.txt")
        effective_output_file.parent.mkdir(parents=True, exist_ok=True)
        working_dir = cwd or self.project_root
        quota_retry_seconds = max(0, int(self.config.quota_retry_seconds))

        while True:
            self.codex_switcher.maybe_switch(self.config.cockpit_data_dir)
            self.log(f"🤖 Codex exec (reasoning={selected_reasoning_effort})")
            if prompt.strip():
                self.log(self.format_prompt_preview(prompt))
            command = [
                "codex",
                "exec",
                "--json",
                "-c",
                f"model_reasoning_effort={json.dumps(selected_reasoning_effort)}",
                "--dangerously-bypass-approvals-and-sandbox",
                "--cd",
                str(working_dir),
                "-o",
                str(effective_output_file),
            ]
            if session_id:
                command.extend(["resume", session_id, "-"])
            else:
                command.append("-")

            effective_output_file.unlink(missing_ok=True)
            thread_id: str | None = None
            with subprocess.Popen(
                command,
                cwd=str(working_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            ) as proc:
                assert proc.stdout is not None
                if proc.stdin is not None:
                    proc.stdin.write(prompt)
                    proc.stdin.close()

                while True:
                    line = proc.stdout.readline()
                    if line == "" and proc.poll() is not None:
                        break
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        print(line, end="")
                        continue
                    pretty = self.format_codex_event(event)
                    if pretty:
                        print(pretty)
                    else:
                        print(line, end="")
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        event_thread_id = event.get("thread_id")
                        if isinstance(event_thread_id, str) and event_thread_id.strip():
                            thread_id = event_thread_id.strip()

                returncode = proc.wait()

            output_text = read_text(effective_output_file)
            if returncode != 0 and self._looks_like_quota_exhaustion(output_text):
                self.play_sound("quota")
                self.log("⚠️ Codex quota exhausted; switching accounts or waiting for quota to restore")
                if quota_retry_seconds > 0:
                    self.log(f"⏳ Retrying Codex after {quota_retry_seconds} seconds")
                    time.sleep(quota_retry_seconds)
                continue

            return CodexAttemptResult(return_code=returncode, thread_id=thread_id, output_text=output_text)

    def run_codex_session_with_retry(
        self,
        *,
        initial_prompt: str,
        output_file: Path | None = None,
        cwd: Path | None = None,
        reasoning_effort: str | None = None,
        max_attempts: int = 2,
        phase_name: str,
        contract: str,
        validator: Callable[[str], ValidationFailure | None],
    ) -> CodexAttemptResult:
        current_prompt = initial_prompt
        session_id: str | None = None
        last_result: CodexAttemptResult | None = None

        for attempt in range(1, max_attempts + 1):
            result = self.run_codex_session(
                current_prompt,
                output_file=output_file,
                cwd=cwd,
                reasoning_effort=reasoning_effort,
                session_id=session_id,
            )
            if result.thread_id:
                session_id = result.thread_id

            last_result = result
            if result.return_code != 0:
                return result

            failure = validator(result.output_text)
            if failure is None:
                return result

            last_result = CodexAttemptResult(
                return_code=result.return_code,
                thread_id=result.thread_id,
                output_text=result.output_text,
                validation_failure=failure,
            )

            if attempt >= max_attempts:
                return last_result
            if not session_id:
                return CodexAttemptResult(
                    return_code=1,
                    thread_id=None,
                    output_text=result.output_text,
                    validation_failure=ValidationFailure(
                        error_code="missing_session_id",
                        field="thread_id",
                        message=f"{phase_name} retry requested but Codex did not emit a resumable session id",
                        expected="a resumable thread_id from codex exec --json",
                    ),
                )

            current_prompt = self.build_retry_prompt(
                phase_name=phase_name,
                attempt=attempt + 1,
                max_attempts=max_attempts,
                failure=failure,
                previous_output=result.output_text,
                contract=contract,
            )

        assert last_result is not None
        return last_result

    def build_retry_prompt(
        self,
        *,
        phase_name: str,
        attempt: int,
        max_attempts: int,
        failure: ValidationFailure,
        previous_output: str,
        contract: str,
    ) -> str:
        failure_yaml = yaml.safe_dump(to_jsonable(failure), sort_keys=False).strip()
        previous_output = previous_output.strip() or "(empty)"
        return dedent(
            f"""
            Retry attempt {attempt} of {max_attempts} for {phase_name}.

            Validation failure:
            {failure_yaml}

            Previous output:
            {previous_output}

            Fix only the structured output contract for the same task and workspace context.
            {contract}
            """
        ).strip() + "\n"

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
        self.state.current_story = None
        self.state.current_story_file = None
        self.save_state()

    def state_current_story(self) -> Optional[str]:
        return self.state.current_story

    def state_set_story(self, phase: Phase | str, story_key: str, story_file: Path | None = None) -> None:
        phase_value = Phase.from_value(phase)
        epic_id = story_key.split("-", 1)[0] if story_key else None
        if self.state.is_parallel:
            self.state.active_phase = phase_value
            self.state.active_epic = epic_id
            self.state.phase = phase_value
            self.state.current_epic = epic_id
        else:
            self.state.phase = phase_value
            self.state.current_epic = epic_id
        self.state.current_story = story_key
        self.state.current_story_file = str(story_file) if story_file else None
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

    def worktree_create(
        self,
        epic_id: str,
        branch_name: str,
        *,
        start_point: str | None = None,
        prefer_existing_branch: bool = False,
    ) -> Path:
        wt_path = self.worktree_path(epic_id)
        if wt_path.exists():
            self.debug(f"Worktree already exists: {wt_path}")
            self.mirror_worktree_support_dirs(wt_path)
            return wt_path

        self.log(f"🌳 Creating worktree for {epic_id} at {wt_path}")
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_process(["git", "fetch", "origin", self.base_branch], cwd=self.project_root, check=False)

        add_result: subprocess.CompletedProcess[str]
        if prefer_existing_branch:
            add_result = self.run_process(["git", "worktree", "add", str(wt_path), branch_name], cwd=self.project_root, check=False)
            if add_result.returncode != 0:
                start_ref = start_point or f"origin/{branch_name}"
                add_result = self.run_process(
                    ["git", "worktree", "add", "-b", branch_name, str(wt_path), start_ref],
                    cwd=self.project_root,
                    check=False,
                )
        else:
            start_ref = start_point or f"origin/{self.base_branch}"
            add_result = self.run_process(
                ["git", "worktree", "add", "-b", branch_name, str(wt_path), start_ref],
                cwd=self.project_root,
                check=False,
            )

        if add_result.returncode != 0 or not wt_path.exists():
            raise RuntimeError(f"Failed to create worktree for {branch_name} at {wt_path}")

        self.mirror_worktree_support_dirs(wt_path)
        return wt_path

    def worktree_remove(self, epic_id: str) -> None:
        wt_path = self.worktree_path(epic_id)
        if not wt_path.exists():
            self.debug(f"Worktree does not exist: {wt_path}")
            return
        self.log(f"🗑️ Removing worktree for {epic_id}")
        self.run_process(["git", "worktree", "remove", "--force", str(wt_path)], cwd=self.project_root, check=False)
        if self.state.active_worktree and Path(self.state.active_worktree) == wt_path:
            self.set_active_worktree(None)

    def worktree_prune(self) -> None:
        self.log("🧹 Pruning orphaned worktrees...")
        self.run_process(["git", "worktree", "prune"], cwd=self.project_root, check=False)

    def sync_base_branch(self) -> None:
        self.run_process(["git", "fetch", "origin", self.base_branch], cwd=self.project_root, check=False)

    # ------------------------------------------------------------------
    # Epic discovery
    # ------------------------------------------------------------------

    def load_sprint_status(self, root: Path | None = None) -> SprintStatus:
        sprint_status_file = self.sprint_status_path(root)
        if not sprint_status_file.exists():
            raise ValueError(f"Missing sprint status file: {sprint_status_file}")

        raw = yaml.safe_load(read_text(sprint_status_file))
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid sprint status YAML: {sprint_status_file}")

        try:
            sprint_status = SprintStatus.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid sprint status YAML: {sprint_status_file}") from exc

        expected_story_root = (root or self.project_root) / "_bmad-output" / "implementation-artifacts"
        actual_story_root = sprint_status.normalized_story_root(root or self.project_root)
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

    def story_matches_patterns(self, story_key: str, sprint_status: SprintStatus) -> bool:
        if not self.config.epic_pattern:
            return True
        epic_id = story_key.split("-", 1)[0]
        haystack = " ".join([story_key, f"epic-{epic_id}", epic_id])
        for pattern in self.config.epic_pattern.split():
            if re.search(pattern, haystack, re.IGNORECASE):
                return True
        return False

    def normalize_selection_reference(self, value: str) -> str:
        return value.strip().replace(".", "-")

    def selection_start_story_index(self, sprint_status: SprintStatus) -> int:
        start_from = self.normalize_selection_reference(self.config.start_from)
        if not start_from:
            return 0

        stories = sprint_status.story_entries()
        story_index_by_key = {story_key: index for index, (story_key, _status) in enumerate(stories)}
        if start_from in story_index_by_key:
            return story_index_by_key[start_from]

        prefix_match = re.fullmatch(r"(?:epic-)?(\d+)(?:-(\d+))?", start_from)
        if prefix_match:
            epic_id = prefix_match.group(1)
            story_num = prefix_match.group(2)
            story_prefix = f"{epic_id}-"
            if story_num:
                story_prefix = f"{epic_id}-{story_num}-"
            for index, (story_key, _status) in enumerate(stories):
                if story_key.startswith(story_prefix):
                    return index

        raise ValueError(f"Start-from reference not found in sprint status: {self.config.start_from}")

    def selection_start_epic_index(self, sprint_status: SprintStatus) -> int:
        start_from = self.normalize_selection_reference(self.config.start_from)
        if not start_from:
            return 0

        epic_match = re.fullmatch(r"(?:epic-)?(\d+)(?:-\d+)?", start_from)
        if not epic_match:
            raise ValueError(f"Start-from reference is not an epic selector: {self.config.start_from}")

        epic_id = epic_match.group(1)
        active_epics = sprint_status.active_epic_ids()
        for index, active_epic in enumerate(active_epics):
            if active_epic == epic_id:
                return index

        raise ValueError(f"Start-from epic not found in active sprint epics: {self.config.start_from}")

    def story_file_for_key(self, sprint_status: SprintStatus, story_key: str, root: Path | None = None) -> Path:
        return sprint_status.normalized_story_root(root or self.project_root) / f"{story_key}.md"

    def select_next_story(self, sprint_status: SprintStatus) -> StoryTarget | None:
        priority_order = [
            SprintStatusValue.IN_PROGRESS,
            SprintStatusValue.REVIEW,
            SprintStatusValue.READY_FOR_DEV,
            SprintStatusValue.BACKLOG,
        ]
        stories = sprint_status.story_entries()
        start_index = self.selection_start_story_index(sprint_status)
        for status in priority_order:
            for story_key, story_status in stories[start_index:]:
                if story_status != status:
                    continue
                if not self.story_matches_patterns(story_key, sprint_status):
                    continue
                story_path = self.story_file_for_key(sprint_status, story_key)
                if story_status != SprintStatusValue.BACKLOG and not story_path.exists():
                    raise ValueError(f"Missing story file for story {story_key}: {story_path}")
                return StoryTarget(key=story_key, path=story_path, status=story_status)
        return None

    def find_next_epic(self, sprint_status: SprintStatus) -> Optional[str]:
        completed = set(self.state.completed_epics)
        pending = {pr.epic for pr in self.state.pending_prs}
        active_epics = sprint_status.active_epic_ids()
        start_index = self.selection_start_epic_index(sprint_status)
        for epic in active_epics[start_index:]:
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

    def build_dev_story_prompt(
        self,
        epic_id: str,
        sprint_status: SprintStatus,
        story_files: list[Path],
        *,
        sprint_status_file: Path,
        workspace_root: Path | None = None,
    ) -> str:
        workspace_root = workspace_root or self.project_root
        story_context = "\n".join(f"- {path}" for path in story_files)
        review_context = self.latest_review_artifact_prompt(root=workspace_root)
        parts = [
            "$bmad-dev-story",
            "",
            "Epic context:",
            f"- Epic: {epic_id}",
            f"- Sprint status: {sprint_status_file}",
            "- Story files:",
            story_context or "  - (none)",
        ]
        if review_context:
            parts.extend(["", review_context])
        return dedent(
            "\n".join(parts)
            + """

            Output contract:
            - Return YAML frontmatter only.
            - Required fields:
              - workflow_status: stories_complete | stories_blocked
              - epic_id: the exact epic id above
              - story_status: review | in-progress
              - blocking_reason: required only when workflow_status is stories_blocked
            - If implementation is complete, set workflow_status: stories_complete and story_status: review.
            - If blocked, set workflow_status: stories_blocked, story_status: in-progress, and include blocking_reason.
            - A blocked dev pass is transient: the orchestrator will reroute immediately without marking the story blocked.
            - Do not emit STATUS markers.
            """
        ).strip() + "\n"

    def build_story_create_prompt(self, story_key: str, story_path: Path) -> str:
        return dedent(
            f"""
            $bmad-create-story

            Target story:
            - Story key: {story_key}
            - Story file: {story_path}

            Create or refresh exactly this story. Do not select a different backlog item.
            Keep the story scoped to this one backlog item and preserve the expected story file structure.
            When finished, leave the story ready for the next dev pass and do not alter unrelated stories.
            """
        ).strip() + "\n"

    def build_story_dev_prompt(
        self,
        story_key: str,
        story_path: Path,
        sprint_status_file: Path,
        *,
        workspace_root: Path | None = None,
    ) -> str:
        workspace_root = workspace_root or self.project_root
        review_context = self.latest_review_artifact_prompt(root=workspace_root)
        parts = [
            "$bmad-dev-story",
            "",
            "Story context:",
            f"- Story key: {story_key}",
            f"- Story file: {story_path}",
            f"- Sprint status: {sprint_status_file}",
        ]
        if review_context:
            parts.extend(["", review_context])
        return dedent(
            "\n".join(parts)
            + """

            Output contract:
            - Return YAML frontmatter only.
            - Required fields:
              - workflow_status: stories_complete | stories_blocked
              - story_key: the exact story key above
              - story_status: review | in-progress
              - blocking_reason: required only when workflow_status is stories_blocked
            - If implementation is complete, set workflow_status: stories_complete and story_status: review.
            - If blocked, set workflow_status: stories_blocked, story_status: in-progress, and include blocking_reason.
            - A blocked dev pass is transient: the orchestrator will reroute immediately without marking the story blocked.
            - Do not emit STATUS markers.

            Use the explicit story file above. Do not switch to a different story.
            """
        ).strip() + "\n"

    def build_story_qa_prompt(self, story_key: str, story_path: Path) -> str:
        return dedent(
            f"""
            $integration-tests-workflow

            Reference:
            - Integration test spec: {self.project_root / ".autopilot" / "specs" / "integration-tests.md"}
            - Follow the INT-xxx catalog and HTTP/system-boundary rules from that spec.
            - Use ./scripts/run_integration_tests.sh as the execution entrypoint.

            Story context:
            - Story key: {story_key}
            - Story file: {story_path}

            Focus on the automated validation that supports this story and its acceptance criteria.
            Output contract:
            - Return YAML frontmatter only.
            - Required fields:
              - review_status: pass | fail
            - Use review_status: pass when QA succeeds.
            - Use review_status: fail when QA is blocked or acceptance criteria fail.
            - Keep the review notes after the frontmatter.
            - Report deterministic failures only.
            """
        ).strip() + "\n"

    def build_story_code_review_prompt(
        self,
        story_key: str,
        story_path: Path,
        *,
        workspace_root: Path | None = None,
    ) -> str:
        workspace_root = workspace_root or self.project_root
        source = self.collect_review_source_snapshot(workspace_root)
        return dedent(
            f"""
            $bmad-code-review

            Review target:
            - Story: {story_key}
            - Story file: {story_path}
            - Branch: {source.current_branch or 'detached'}
            - Base branch: origin/{self.base_branch}
            - Source: current workspace snapshot (committed diff + working tree diff)

            Committed branch diff:
            {source.branch_diff or "(none)"}

            Staged diff:
            {source.staged_diff or "(none)"}

            Unstaged diff:
            {source.unstaged_diff or "(none)"}

            Working tree status:
            {source.working_tree_status or "(clean)"}

            Reviewability:
            - Reviewable source present: {"yes" if source.has_reviewable_source else "no"}
            - If reviewable source is no, fail closed and do not emit pass.
            - Review scope fingerprint: {self.review_scope_fingerprint(source)}

            Review the current workspace snapshot first. Do not ignore uncommitted changes.
            If no reviewable source exists, fail closed instead of approving an empty review scope.
            Output contract:
            - Return YAML frontmatter only.
            - Required fields:
              - review_status: pass | fail
              - review_scope_fingerprint: must exactly match the fingerprint above
              - reviewed_files: list of reviewed file paths relative to the repository root
            - Use review_status: pass when the review is clean.
            - Use review_status: fail when actionable findings remain.
            - Keep the review notes after the frontmatter.
            - Do not emit STATUS markers.
            - The review artifact will be persisted automatically; use it to preserve findings and verdict.
            """
        ).strip() + "\n"

    def build_qa_prompt(
        self,
        epic_id: str,
        sprint_status: SprintStatus,
        story_files: list[Path],
        *,
        repo_root: Path | None = None,
    ) -> str:
        repo_root = repo_root or self.project_root
        story_context = "\n".join(f"- {path}" for path in story_files)
        return dedent(
            f"""
            $integration-tests-workflow

            Reference:
            - Integration test spec: {repo_root / ".autopilot" / "specs" / "integration-tests.md"}
            - Follow the INT-xxx catalog and HTTP/system-boundary rules from that spec.
            - Use ./scripts/run_integration_tests.sh as the execution entrypoint.

            Epic context:
            - Epic: {epic_id}
            - Story files:
            {story_context or "  - (none)"}

            Run the relevant integration coverage for this epic and report exact deterministic failures.
            Output contract:
            - Return YAML frontmatter only.
            - Required fields:
              - review_status: pass | fail
            - Use review_status: pass when QA succeeds.
            - Use review_status: fail when QA is blocked or acceptance criteria fail.
            - Keep the review notes after the frontmatter.
            """
        ).strip() + "\n"

    def review_artifacts_dir(self, root: Path | None = None) -> Path:
        return (root or self.project_root) / "_bmad-outputs" / "review-artifacts"

    def next_review_round(self, review_type: str, *, root: Path | None = None) -> int:
        review_dir = self.review_artifacts_dir(root)
        pattern = re.compile(rf"^{re.escape(review_type)}-round-(\d+)\.md$")
        highest_round = 0
        if review_dir.exists():
            for path in review_dir.glob(f"{review_type}-round-*.md"):
                match = pattern.match(path.name)
                if match:
                    highest_round = max(highest_round, int(match.group(1)))
        return highest_round + 1

    def latest_review_artifacts(self, *, root: Path | None = None) -> dict[str, Path]:
        review_dir = self.review_artifacts_dir(root)
        latest: dict[str, tuple[int, Path]] = {}
        if not review_dir.exists():
            return {}
        for review_type in ("code-review", "qa-review"):
            pattern = re.compile(rf"^{re.escape(review_type)}-round-(\d+)\.md$")
            for path in review_dir.glob(f"{review_type}-round-*.md"):
                match = pattern.match(path.name)
                if not match:
                    continue
                round_number = int(match.group(1))
                current = latest.get(review_type)
                if current is None or round_number > current[0]:
                    latest[review_type] = (round_number, path)
        return {review_type: path for review_type, (_round_number, path) in latest.items()}

    def latest_review_artifact_prompt(self, *, root: Path | None = None) -> str:
        latest = self.latest_review_artifacts(root=root)
        if not latest:
            return ""

        lines = [
            "Latest persisted review artifacts:",
        ]
        for review_type in ("code-review", "qa-review"):
            path = latest.get(review_type)
            if path:
                lines.append(f"- {review_type}: {path}")
        if root:
            lines.append(f"- Workspace root: {root}")
        lines.append(
            "Read the latest persisted review files above before making further dev-story changes; "
            "they begin with YAML frontmatter containing review_status: pass|fail."
        )
        return "\n".join(lines)

    def story_file_status(self, story_file: Path) -> str | None:
        if not story_file.exists():
            return None
        match = re.search(r"(?im)^Status:\s*([A-Za-z0-9_-]+)\s*$", read_text(story_file))
        if not match:
            return None
        return match.group(1).strip().lower()

    @staticmethod
    def output_has_status(output_text: str, status: str) -> bool:
        return re.search(rf"(?im)^STATUS:\s*{re.escape(status)}\s*$", output_text) is not None

    @staticmethod
    def parse_yaml_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        if not text:
            return {}, text

        normalized = text.lstrip("\ufeff").lstrip()
        if not normalized.startswith("---"):
            return {}, text

        lines = normalized.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text

        end_index = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end_index = index
                break

        if end_index is None:
            return {}, text

        frontmatter_text = "\n".join(lines[1:end_index]).strip()
        if not frontmatter_text:
            return {}, "\n".join(lines[end_index + 1 :])

        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError:
            return {}, text

        if not isinstance(frontmatter, dict):
            return {}, text

        body = "\n".join(lines[end_index + 1 :])
        return frontmatter, body

    def review_status_from_output(self, output_text: str) -> str | None:
        frontmatter, _body = self.parse_yaml_frontmatter(output_text)
        value = frontmatter.get("review_status")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"pass", "fail"}:
                return normalized
        return None

    def review_status_from_artifact(self, review_type: str, *, root: Path | None = None) -> str | None:
        path = self.latest_review_artifacts(root=root).get(review_type)
        if not path or not path.exists():
            return None
        return self.review_status_from_output(read_text(path))

    def review_status_for_output(self, output_text: str, *, return_code: int, status_hint: str | None = None) -> str:
        explicit = self.review_status_from_output(output_text)
        if explicit == "pass" and return_code == 0:
            return "pass"
        return "fail"

    def review_artifact_text(self, review_type: str, *, root: Path | None = None) -> str:
        path = self.latest_review_artifacts(root=root).get(review_type)
        return read_text(path) if path else ""

    @staticmethod
    def validation_failure_from_exception(
        exc: Exception,
        *,
        default_error_code: str,
        default_field: str | None,
        expected: str | None = None,
    ) -> ValidationFailure:
        error_code = default_error_code
        field = default_field
        message = str(exc).strip() or default_error_code

        if isinstance(exc, ValidationError):
            errors = exc.errors()
            if errors:
                first = errors[0]
                loc = first.get("loc") or ()
                if loc:
                    field = ".".join(str(part) for part in loc)
                message = str(first.get("msg") or message).strip() or message
                error_type = first.get("type")
                if isinstance(error_type, str) and error_type.strip():
                    error_code = error_type.strip()

        return ValidationFailure(error_code=error_code, field=field, message=message, expected=expected)

    @staticmethod
    def review_scope_file_names(text: str) -> list[str]:
        files: set[str] = set()
        for line in text.splitlines():
            name = line.strip()
            if name:
                files.add(name)
        return sorted(files)

    def review_scope_fingerprint(self, source: ReviewSourceSnapshot) -> str:
        payload = {
            "current_branch": source.current_branch,
            "branch_diff": self.review_scope_file_names(source.branch_diff),
            "staged_diff": self.review_scope_file_names(source.staged_diff),
            "unstaged_diff": self.review_scope_file_names(source.unstaged_diff),
            "working_tree_status": source.working_tree_status.splitlines(),
        }
        payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()

    def parse_story_dev_output(
        self,
        output_text: str,
        *,
        expected_story_key: str,
    ) -> tuple[StoryDevOutput | None, ValidationFailure | None]:
        frontmatter, _body = self.parse_yaml_frontmatter(output_text)
        if not frontmatter:
            return None, ValidationFailure(
                error_code="missing_frontmatter",
                field="frontmatter",
                message="missing YAML frontmatter",
                expected="workflow_status, story_key, story_status, blocking_reason?",
            )
        try:
            parsed = StoryDevOutput.model_validate(frontmatter)
        except ValidationError as exc:
            return None, self.validation_failure_from_exception(
                exc,
                default_error_code="invalid_story_output",
                default_field="frontmatter",
                expected="workflow_status, story_key, story_status, blocking_reason?",
            )
        if parsed.story_key != expected_story_key:
            return None, ValidationFailure(
                error_code="mismatched_story_key",
                field="story_key",
                message=f"expected story_key {expected_story_key}, got {parsed.story_key}",
                expected=expected_story_key,
            )
        return parsed, None

    def parse_epic_dev_output(
        self,
        output_text: str,
        *,
        expected_epic_id: str,
    ) -> tuple[EpicDevOutput | None, ValidationFailure | None]:
        frontmatter, _body = self.parse_yaml_frontmatter(output_text)
        if not frontmatter:
            return None, ValidationFailure(
                error_code="missing_frontmatter",
                field="frontmatter",
                message="missing YAML frontmatter",
                expected="workflow_status, epic_id, story_status, blocking_reason?",
            )
        try:
            parsed = EpicDevOutput.model_validate(frontmatter)
        except ValidationError as exc:
            return None, self.validation_failure_from_exception(
                exc,
                default_error_code="invalid_epic_output",
                default_field="frontmatter",
                expected="workflow_status, epic_id, story_status, blocking_reason?",
            )
        if parsed.epic_id != expected_epic_id:
            return None, ValidationFailure(
                error_code="mismatched_epic_id",
                field="epic_id",
                message=f"expected epic_id {expected_epic_id}, got {parsed.epic_id}",
                expected=expected_epic_id,
            )
        return parsed, None

    def parse_review_output(
        self,
        output_text: str,
        *,
        expected_fingerprint: str,
        valid_files: set[str],
    ) -> tuple[ReviewDecisionOutput | None, ValidationFailure | None]:
        frontmatter, _body = self.parse_yaml_frontmatter(output_text)
        if not frontmatter:
            return None, ValidationFailure(
                error_code="missing_frontmatter",
                field="frontmatter",
                message="missing YAML frontmatter",
                expected="review_status, review_scope_fingerprint, reviewed_files",
            )
        try:
            parsed = ReviewDecisionOutput.model_validate(frontmatter)
        except ValidationError as exc:
            return None, self.validation_failure_from_exception(
                exc,
                default_error_code="invalid_review_output",
                default_field="frontmatter",
                expected="review_status, review_scope_fingerprint, reviewed_files",
            )
        if parsed.review_scope_fingerprint != expected_fingerprint:
            return None, ValidationFailure(
                error_code="mismatched_review_scope_fingerprint",
                field="review_scope_fingerprint",
                message="review_scope_fingerprint does not match the current workspace snapshot",
                expected=expected_fingerprint,
            )
        invalid_files = [path for path in parsed.reviewed_files if path not in valid_files]
        if invalid_files:
            return None, ValidationFailure(
                error_code="invalid_reviewed_files",
                field="reviewed_files",
                message=f"reviewed_files contains paths outside the review scope: {', '.join(invalid_files)}",
                expected="subset of the current workspace snapshot files",
            )
        return parsed, None

    def validate_review_output(
        self,
        output_text: str,
        *,
        expected_fingerprint: str,
        valid_files: set[str],
    ) -> ValidationFailure | None:
        _parsed, failure = self.parse_review_output(
            output_text,
            expected_fingerprint=expected_fingerprint,
            valid_files=valid_files,
        )
        return failure

    def validate_story_progress(
        self,
        *,
        output_text: str,
        expected_story_key: str,
        story_path: Path,
        sprint_status_root: Path,
    ) -> ValidationFailure | None:
        parsed, failure = self.parse_story_dev_output(output_text, expected_story_key=expected_story_key)
        if failure:
            return failure
        assert parsed is not None

        story_status = self.story_file_status(story_path)
        sprint_status = self.load_sprint_status(root=sprint_status_root)
        actual_story_status = dict(sprint_status.story_entries()).get(expected_story_key)

        if parsed.workflow_status == "stories_complete":
            if story_status not in {"review", "done"}:
                return ValidationFailure(
                    error_code="story_status_not_review",
                    field="story_file.Status",
                    message=f"expected story file status review, got {story_status or 'missing'}",
                    expected="review",
                )
            if actual_story_status != SprintStatusValue.REVIEW:
                return ValidationFailure(
                    error_code="sprint_status_not_review",
                    field=f"development_status.{expected_story_key}",
                    message=f"expected sprint-status entry {expected_story_key} to be review, got {actual_story_status.value if actual_story_status else 'missing'}",
                    expected=SprintStatusValue.REVIEW.value,
                )
        else:
            if story_status != "in-progress":
                return ValidationFailure(
                    error_code="story_status_not_in_progress",
                    field="story_file.Status",
                    message=f"expected story file status in-progress for a transient blocked dev pass, got {story_status or 'missing'}",
                    expected="in-progress",
                )
            if actual_story_status != SprintStatusValue.IN_PROGRESS:
                return ValidationFailure(
                    error_code="sprint_status_not_in_progress",
                    field=f"development_status.{expected_story_key}",
                    message=f"expected sprint-status entry {expected_story_key} to remain in-progress for a transient blocked dev pass, got {actual_story_status.value if actual_story_status else 'missing'}",
                    expected=SprintStatusValue.IN_PROGRESS.value,
                )
        return None

    def validate_epic_progress(
        self,
        *,
        output_text: str,
        expected_epic_id: str,
        story_files: list[Path],
    ) -> ValidationFailure | None:
        parsed, failure = self.parse_epic_dev_output(output_text, expected_epic_id=expected_epic_id)
        if failure:
            return failure
        assert parsed is not None

        story_statuses = [self.story_file_status(path) for path in story_files]
        if parsed.workflow_status == "stories_complete":
            if any(status not in {"review", "done"} for status in story_statuses):
                return ValidationFailure(
                    error_code="story_files_not_review",
                    field="story_files",
                    message="all story files must be marked review or done after a complete dev pass",
                    expected="each story file status review or done",
                )
        return None

    def persist_review_artifact(
        self,
        review_type: str,
        *,
        phase_name: str,
        source_output: Path,
        return_code: int,
        output_text: str,
        context_lines: list[str],
        status_hint: str | None = None,
        root: Path | None = None,
    ) -> Path:
        round_number = self.next_review_round(review_type, root=root)
        artifact_path = self.review_artifacts_dir(root) / f"{review_type}-round-{round_number}.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        persisted_review_status = self.review_status_for_output(output_text, return_code=return_code, status_hint=status_hint)

        lines = [
            "---",
            f"review_status: {persisted_review_status}",
            "---",
            "",
            f"# {review_type.replace('-', ' ').title()} Round {round_number}",
            "",
            f"- Timestamp: {utc_now()}",
            f"- Phase: {phase_name}",
            f"- Return code: {return_code}",
            f"- Source output: {source_output}",
        ]
        if status_hint:
            lines.append(f"- Status marker: {status_hint}")
        if context_lines:
            lines.append("- Context:")
            lines.extend(f"  - {line}" for line in context_lines)
        lines.extend(
            [
                "",
                "---",
                "",
                output_text.rstrip() or "(empty)",
                "",
            ]
        )
        write_text(artifact_path, "\n".join(lines))
        self.log(f"📝 Saved {review_type} review artifact: {artifact_path}")
        return artifact_path

    def build_code_review_prompt(self, epic_id: str, *, repo_root: Path | None = None) -> str:
        repo_root = repo_root or self.project_root
        source = self.collect_review_source_snapshot(repo_root)
        return dedent(
            f"""
            $bmad-code-review

            Review target:
            - Epic: {epic_id}
            - Branch: {source.current_branch or f'feature/epic-{epic_id}'}
            - Base branch: origin/{self.base_branch}
            - Source: current workspace snapshot (committed diff + working tree diff)

            Committed branch diff:
            {source.branch_diff or "(none)"}

            Staged diff:
            {source.staged_diff or "(none)"}

            Unstaged diff:
            {source.unstaged_diff or "(none)"}

            Working tree status:
            {source.working_tree_status or "(clean)"}

            Reviewability:
            - Reviewable source present: {"yes" if source.has_reviewable_source else "no"}
            - If reviewable source is no, fail closed and do not emit pass.
            - Review scope fingerprint: {self.review_scope_fingerprint(source)}

            Review the current workspace snapshot first. Do not ignore uncommitted changes.
            If no reviewable source exists, fail closed instead of approving an empty review scope.
            Output contract:
            - Return YAML frontmatter only.
            - Required fields:
              - review_status: pass | fail
              - review_scope_fingerprint: must exactly match the fingerprint above
              - reviewed_files: list of reviewed file paths relative to the repository root
            - Use review_status: pass when the review is clean.
            - Use review_status: fail when actionable findings remain.
            - Keep the review notes after the frontmatter.
            - Do not emit STATUS markers.
            """
        ).strip() + "\n"

    def build_commit_split_prompt(
        self,
        *,
        epic_id: str | None = None,
        story_key: str | None = None,
        story_path: Path | None = None,
        story_files: list[Path] | None = None,
        repo_root: Path | None = None,
    ) -> str:
        repo_root = repo_root or self.project_root
        current_branch = self.run_text(["git", "branch", "--show-current"], cwd=repo_root, check=False, capture_output=True).strip()
        working_tree = self.run_text(["git", "status", "--short"], cwd=repo_root, check=False, capture_output=True).strip()

        context_lines: list[str] = []
        if story_key and story_path:
            context_lines.append(f"- Story: {story_key}")
            context_lines.append(f"- Story file: {story_path}")
        if epic_id:
            context_lines.append(f"- Epic: {epic_id}")
        if story_files:
            context_lines.append("- Story files:")
            context_lines.extend(f"  - {path}" for path in story_files)
        if not context_lines:
            context_lines.append("- Context: no specific story or epic context available")
        context_text = "\n".join(context_lines)

        return dedent(
            f"""
            $commit-split-workflow

            Context:
            {context_text}
            - Branch: {current_branch or 'detached'}
            - Goal: split the current implementation into small, reviewable commits.

            Working tree status:
            {working_tree or "(clean)"}

            Use the repository's commit-message conventions and keep commit groups aligned to intent.
            """
        ).strip() + "\n"

    def update_story_file_status(self, story_file: Path, new_status: str) -> None:
        if not story_file.exists():
            raise ValueError(f"Missing story file: {story_file}")
        text = read_text(story_file)
        updated, count = re.subn(r"^Status:\s*.*$", f"Status: {new_status}", text, count=1, flags=re.MULTILINE)
        if count == 0:
            raise ValueError(f"Could not find Status line in story file: {story_file}")
        write_text(story_file, updated)

    def update_sprint_status_story_status(self, story_key: str, new_status: SprintStatusValue | str) -> None:
        if not self.sprint_status_file.exists():
            raise ValueError(f"Missing sprint status file: {self.sprint_status_file}")

        if isinstance(new_status, SprintStatusValue):
            status_value = new_status.value
        else:
            status_value = str(new_status)

        raw = read_text(self.sprint_status_file)
        now = utc_now()
        updated, count = re.subn(
            rf"^(\s*{re.escape(story_key)}:\s*)([A-Za-z0-9_-]+)\s*$",
            rf"\1{status_value}",
            raw,
            count=1,
            flags=re.MULTILINE,
        )
        if count == 0:
            raise ValueError(f"Story key not found in sprint status: {story_key}")

        updated = re.sub(r"^# last_updated: .*$", f"# last_updated: {now}", updated, count=1, flags=re.MULTILINE)
        updated = re.sub(r"^last_updated: .*$", f"last_updated: {now}", updated, count=1, flags=re.MULTILINE)
        write_text(self.sprint_status_file, updated)

    def mark_story_review(self, story_key: str, story_path: Path) -> None:
        self.update_story_file_status(story_path, "review")
        self.update_sprint_status_story_status(story_key, SprintStatusValue.REVIEW)

    def mark_story_in_progress(self, story_key: str, story_path: Path) -> None:
        self.update_story_file_status(story_path, "in-progress")
        self.update_sprint_status_story_status(story_key, SprintStatusValue.IN_PROGRESS)

    def mark_story_done(self, story_key: str, story_path: Path) -> None:
        self.update_story_file_status(story_path, "done")
        self.update_sprint_status_story_status(story_key, SprintStatusValue.DONE)

    def reroute_to_development(
        self,
        *,
        epic_id: str,
        story_key: str | None = None,
        story_path: Path | None = None,
        reason: str | None = None,
    ) -> None:
        self.log("⚠️ Rerouting back to development")
        if reason:
            self.log(f"   Reason: {reason}")

        if story_key and story_path:
            try:
                self.mark_story_in_progress(story_key, story_path)
            except Exception as exc:
                self.log(f"⚠️ Could not mark story back in progress before rerun: {exc}")
            self.state_set_story(Phase.DEVELOP_STORIES, story_key, story_path)
            return

        self.state_set(Phase.DEVELOP_STORIES, epic_id)

    def reroute_development_after_blocked(
        self,
        *,
        epic_id: str,
        story_key: str | None = None,
        story_path: Path | None = None,
        reason: str | None = None,
    ) -> None:
        if reason:
            self.log(f"⚠️ Development reported blocked; rerouting immediately: {reason}")
        self.reroute_to_development(
            epic_id=epic_id,
            story_key=story_key,
            story_path=story_path,
            reason=reason,
        )

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

    def autopilot_checks(self, root: Path | None = None) -> bool:
        root = root or self.project_root
        ok = True
        backend = root / "backend" / "Cargo.toml"
        frontend = root / "frontend" / "package.json"
        mobile = root / "mobile-native" / "gradlew"

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

    def phase_find_story(self) -> None:
        self.log("📋 PHASE: FIND_STORY")
        try:
            sprint_status = self.load_sprint_status()
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, None)
            return

        try:
            target = self.select_next_story(sprint_status)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, None)
            return

        if not target:
            self.log("🎉 No more active stories in sprint-status.yaml")
            self.state_set(Phase.DONE, None)
            return

        self.log(f"✅ Found story: {target.key} [{target.status.value}]")
        if target.status == SprintStatusValue.BACKLOG:
            self.state_set_story(Phase.CREATE_STORY, target.key, target.path)
            return
        if target.status in {SprintStatusValue.READY_FOR_DEV, SprintStatusValue.IN_PROGRESS}:
            self.mark_story_in_progress(target.key, target.path)
            self.state_set_story(Phase.DEVELOP_STORIES, target.key, target.path)
            return
        if target.status == SprintStatusValue.REVIEW:
            self.state_set_story(Phase.CODE_REVIEW, target.key, target.path)
            return

        self.log(f"❌ Unsupported story status: {target.status.value}")
        self.state_set(Phase.BLOCKED, None)

    def phase_create_story(self) -> None:
        self.log("📝 PHASE: CREATE_STORY")
        story_key = self.state_current_story()
        if not story_key:
            self.log("❌ current_story missing")
            self.state_set(Phase.BLOCKED, None)
            return

        try:
            sprint_status = self.load_sprint_status()
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, None)
            return

        story_path = self.story_file_for_key(sprint_status, story_key)
        output_file = self.tmp_dir / "create-story-output.txt"
        return_code = self.run_codex_exec(
            self.build_story_create_prompt(story_key, story_path),
            output_file,
            cwd=self.project_root,
        )
        if return_code != 0:
            self.log("❌ Codex reported create-story failed")
            self.state_set(Phase.BLOCKED, story_key.split("-", 1)[0] if story_key else None)
            return

        try:
            refreshed = self.load_sprint_status()
            story_status = dict(refreshed.story_entries()).get(story_key)
            if story_status == SprintStatusValue.BACKLOG:
                self.log(f"⚠️ Story {story_key} is still backlog after create-story; continuing anyway")
            else:
                self.log(f"✅ Story {story_key} is now {story_status.value if story_status else 'unknown'}")
        except Exception as exc:
            self.log(f"⚠️ Unable to verify created story status: {exc}")

        self.state_set(Phase.FIND_EPIC, None)

    def phase_develop_story(self) -> None:
        self.log("💻 PHASE: DEVELOP_STORY")
        story_key = self.state_current_story()
        if not story_key:
            self.log("❌ current_story missing")
            self.state_set(Phase.BLOCKED, None)
            return

        story_path: Path | None = None
        try:
            sprint_status = self.load_sprint_status()
            target = dict(sprint_status.story_entries()).get(story_key)
            if target is None:
                raise ValueError(f"Missing story entry in sprint status: {story_key}")
            story_path = self.story_file_for_key(sprint_status, story_key)
            if target == SprintStatusValue.REVIEW:
                self.log(f"⏯️ Story {story_key} is already in review; skipping back to QA")
                self.state_set_story(Phase.QA_AUTOMATION_TEST, story_key, story_path)
                return
            if target == SprintStatusValue.DONE:
                self.log(f"⏯️ Story {story_key} is already done; selecting the next story")
                self.state_set(Phase.FIND_EPIC, None)
                return
            if target != SprintStatusValue.BACKLOG and not story_path.exists():
                raise ValueError(f"Missing story file for story {story_key}: {story_path}")
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path or (Path(self.state.current_story_file) if self.state.current_story_file else None),
                reason=str(exc),
            )
            return

        self.log(f"📄 Sprint status source: {self.sprint_status_file}")
        self.log(f"📄 Story context: {story_path}")

        output_file = self.tmp_dir / "develop-story-output.txt"
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_story_dev_prompt(
                story_key,
                story_path,
                self.sprint_status_file,
                workspace_root=self.project_root,
            ),
            output_file=output_file,
            cwd=self.project_root,
            reasoning_effort=self.codex_reasoning_effort,
            max_attempts=2,
            phase_name="story-development",
            contract=dedent(
                """
                Return YAML frontmatter only with:
                - workflow_status: stories_complete | stories_blocked
                - story_key: exact story key
                - story_status: review | in-progress
                - blocking_reason: required only when blocked
                """
            ).strip(),
            validator=lambda output_text: self.validate_story_progress(
                output_text=output_text,
                expected_story_key=story_key,
                story_path=story_path,
                sprint_status_root=self.project_root,
            ),
        )
        output_text = result.output_text
        parsed_output, validation_failure = self.parse_story_dev_output(output_text, expected_story_key=story_key)
        if parsed_output and parsed_output.workflow_status == "stories_blocked":
            self.reroute_development_after_blocked(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=parsed_output.blocking_reason or "story development blocked",
            )
            return
        if result.return_code != 0:
            self.log("❌ Codex reported story development failed")
            if result.validation_failure:
                self.log(f"   Validation error: {to_jsonable(result.validation_failure)}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=result.validation_failure.message if result.validation_failure else "story development returned non-zero",
            )
            return
        if validation_failure or not parsed_output:
            self.log("❌ Codex did not produce a valid story-development response")
            if validation_failure:
                self.log(f"   Validation error: {to_jsonable(validation_failure)}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=validation_failure.message if validation_failure else "invalid story-development output",
            )
            return

        if parsed_output.workflow_status == "stories_blocked":
            self.log("❌ Codex reported story development blocked")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=parsed_output.blocking_reason or "story development blocked",
            )
            return

        if parsed_output.workflow_status != "stories_complete":
            self.log("❌ Codex did not report stories_complete")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=f"unexpected workflow_status: {parsed_output.workflow_status}",
            )
            return

        self.log("Running local checks gate...")
        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after story development: {exc}")

        self.mark_story_review(story_key, story_path)
        self.state_set_story(Phase.COMMIT_SPLIT, story_key, story_path)
        self.log("✅ Story implementation complete; running commit split workflow next")

    def phase_qa_automation_test_story(self) -> None:
        self.log("🧪 PHASE: QA_AUTOMATION_TEST")
        story_key = self.state_current_story()
        if not story_key:
            self.log("❌ current_story missing")
            self.state_set(Phase.BLOCKED, None)
            return

        try:
            sprint_status = self.load_sprint_status()
            target = dict(sprint_status.story_entries()).get(story_key)
            if target is None:
                raise ValueError(f"Missing story entry in sprint status: {story_key}")
            story_path = self.story_file_for_key(sprint_status, story_key)
            if target == SprintStatusValue.BACKLOG:
                raise ValueError(f"Story {story_key} is still backlog and cannot run QA")
            if target == SprintStatusValue.DONE:
                self.log(f"⏯️ Story {story_key} is already done; selecting the next story")
                self.state_set(Phase.FIND_EPIC, None)
                return
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, story_key.split("-", 1)[0] if story_key else None)
            return

        self.log(f"📄 Sprint status source: {self.sprint_status_file}")
        self.log(f"📄 Story context: {story_path}")

        output_file = self.tmp_dir / "qa-story-output.txt"
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_story_qa_prompt(story_key, story_path),
            output_file=output_file,
            cwd=self.project_root,
            reasoning_effort=self.codex_reasoning_effort,
            max_attempts=2,
            phase_name="story-qa",
            contract=dedent(
                """
                Return YAML frontmatter only with:
                - review_status: pass | fail
                """
            ).strip(),
            validator=lambda output_text: None
            if self.review_status_from_output(output_text) in {"pass", "fail"}
            else ValidationFailure(
                error_code="missing_review_status",
                field="frontmatter.review_status",
                message="missing YAML frontmatter review_status",
                expected="review_status: pass | fail",
            ),
        )
        output_text = result.output_text
        return_code = result.return_code
        review_status = self.review_status_from_output(output_text)
        self.persist_review_artifact(
            "qa-review",
            phase_name=Phase.QA_AUTOMATION_TEST.value,
            source_output=output_file,
            return_code=return_code,
            output_text=output_text,
            context_lines=[
                f"Story: {story_key}",
                f"Story file: {story_path}",
                f"Sprint status: {self.sprint_status_file}",
            ],
            status_hint=None,
            root=self.project_root,
        )
        if result.validation_failure:
            self.log("❌ Codex reported QA validation blocked")
            self.log(f"   Validation error: {to_jsonable(result.validation_failure)}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=result.validation_failure.message,
            )
            return
        if review_status != "pass":
            self.log("❌ Codex reported QA blocked")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason="QA review_status was not pass",
            )
            return

        self.log("Running local checks gate...")
        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after QA automation: {exc}")

        self.state_set_story(Phase.CODE_REVIEW, story_key, story_path)
        self.play_sound("review_ready")
        self.log("✅ QA automation complete")

    def phase_code_review_story(self) -> None:
        self.log("🔍 PHASE: CODE_REVIEW")
        story_key = self.state_current_story()
        if not story_key:
            self.log("❌ current_story missing")
            self.state_set(Phase.BLOCKED, None)
            return

        try:
            sprint_status = self.load_sprint_status()
            target = dict(sprint_status.story_entries()).get(story_key)
            if target is None:
                raise ValueError(f"Missing story entry in sprint status: {story_key}")
            story_path = self.story_file_for_key(sprint_status, story_key)
            if target == SprintStatusValue.DONE:
                self.log(f"⏯️ Story {story_key} is already done; selecting the next story")
                self.state_set(Phase.FIND_EPIC, None)
                return
            if target == SprintStatusValue.BACKLOG:
                raise ValueError(f"Story {story_key} is still backlog and cannot be reviewed")
            if target != SprintStatusValue.BACKLOG and not story_path.exists():
                raise ValueError(f"Missing story file for story {story_key}: {story_path}")
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, story_key.split("-", 1)[0] if story_key else None)
            return

        self.log(f"Running BMAD code-review workflow for story {story_key}")
        source = self.collect_review_source_snapshot(self.project_root)
        if not source.has_reviewable_source:
            blocked_text = "No reviewable source found in the current workspace snapshot."
            self.log(f"❌ {blocked_text}")
            self.persist_review_artifact(
                "code-review",
                phase_name=Phase.CODE_REVIEW.value,
                source_output=self.tmp_dir / "code-review-output.txt",
                return_code=1,
                output_text=blocked_text,
                context_lines=[
                    f"Story: {story_key}",
                    f"Story file: {story_path}",
                    f"Sprint status: {self.sprint_status_file}",
                    f"Workspace root: {self.project_root}",
                ],
                status_hint="STATUS: CODE_REVIEW_BLOCKED",
                root=self.project_root,
            )
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=blocked_text,
            )
            return

        output_file = self.tmp_dir / "code-review-output.txt"
        expected_fingerprint = self.review_scope_fingerprint(source)
        valid_files = set(self.review_scope_file_names(source.branch_diff))
        valid_files.update(self.review_scope_file_names(source.staged_diff))
        valid_files.update(self.review_scope_file_names(source.unstaged_diff))
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_story_code_review_prompt(
                story_key,
                story_path,
                workspace_root=self.project_root,
            ),
            output_file=output_file,
            cwd=self.project_root,
            reasoning_effort=self.codex_reasoning_effort,
            max_attempts=2,
            phase_name="story-code-review",
            contract=dedent(
                """
                Return YAML frontmatter only with:
                - review_status: pass | fail
                - review_scope_fingerprint: exact fingerprint from the prompt
                - reviewed_files: list of reviewed file paths relative to the repository root
                """
            ).strip(),
            validator=lambda output_text: self.validate_review_output(
                output_text,
                expected_fingerprint=expected_fingerprint,
                valid_files=valid_files,
            ),
        )
        output_text = result.output_text
        return_code = result.return_code
        parsed_output, validation_failure = self.parse_review_output(
            output_text,
            expected_fingerprint=expected_fingerprint,
            valid_files=valid_files,
        )
        if result.return_code != 0:
            self.log("❌ Codex reported code review failed")
            if result.validation_failure:
                self.log(f"   Validation error: {to_jsonable(result.validation_failure)}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=result.validation_failure.message if result.validation_failure else "code review returned non-zero",
            )
            return
        if validation_failure or not parsed_output:
            self.log("❌ Codex did not produce a valid code-review response")
            if validation_failure:
                self.log(f"   Validation error: {to_jsonable(validation_failure)}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=validation_failure.message if validation_failure else "invalid code-review output",
            )
            return
        self.persist_review_artifact(
            "code-review",
            phase_name=Phase.CODE_REVIEW.value,
            source_output=output_file,
            return_code=return_code,
            output_text=output_text,
            context_lines=[
                f"Story: {story_key}",
                f"Story file: {story_path}",
                f"Sprint status: {self.sprint_status_file}",
                f"Workspace root: {self.project_root}",
            ],
            status_hint=None,
            root=self.project_root,
        )
        if parsed_output.review_status != "pass":
            self.log("❌ Codex reported code review blocked")
            self.mark_story_in_progress(story_key, story_path)
            self.state_set_story(Phase.DEVELOP_STORIES, story_key, story_path)
            return

        try:
            self.autopilot_checks()
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after code review: {exc}")
            self.reroute_to_development(
                epic_id=story_key.split("-", 1)[0] if story_key else "",
                story_key=story_key,
                story_path=story_path,
                reason=f"local checks failed after code review: {exc}",
            )
            return

        try:
            self.mark_story_done(story_key, story_path)
        except Exception as exc:
            self.log(f"❌ Failed to mark story done: {exc}")
            self.state_set(Phase.BLOCKED, story_key.split("-", 1)[0] if story_key else None)
            return

        self.play_sound("review_complete")
        self.state_set(Phase.FIND_EPIC, None)
        self.log("✅ Code review passed; story marked done")

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
            epic_id = open_epic_branch.removeprefix("feature/epic-")
            wt_path = self.worktree_path(epic_id)
            if not wt_path.exists():
                try:
                    self.worktree_create(
                        epic_id,
                        open_epic_branch,
                        start_point=f"origin/{open_epic_branch}",
                        prefer_existing_branch=True,
                    )
                except Exception as exc:
                    self.log(f"⚠️ Could not create worktree for open PR #{pr_number_str}: {exc}")
            if wt_path.exists():
                self.set_active_worktree(wt_path)
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
        wt_path = self.worktree_create(epic_id, branch_name, start_point=f"origin/{self.base_branch}")
        self.set_active_worktree(wt_path)
        self.run_process(["git", "push", "-u", "origin", branch_name], cwd=wt_path, check=False)
        self.state_set(Phase.DEVELOP_STORIES, epic_id)
        self.log(f"✅ Branch ready: {branch_name}")

    def phase_develop_stories(self) -> None:
        if self.is_story_flow():
            self.phase_develop_story()
            return
        self.log("💻 PHASE: DEVELOP_STORIES")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        workspace_root = self.epic_workspace_root(epic_id)
        sprint_status_file = self.sprint_status_path(workspace_root)

        if self.run_text(["git", "status", "--porcelain"], cwd=workspace_root, check=False).strip():
            self.log("⚠️ Git working tree not clean - committing pending changes first")
            self.run_process(["git", "add", "-A"], cwd=workspace_root, check=False)
            self.run_process(["git", "commit", "-m", "chore: auto-commit before story development"], cwd=workspace_root, check=False)

        try:
            sprint_status = self.load_sprint_status(root=workspace_root)
            story_files = sprint_status.story_files_for_epic(workspace_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.reroute_to_development(epic_id=epic_id, reason=str(exc))
            return
        self.log(f"📄 Sprint status source: {sprint_status_file}")
        for story_file in story_files:
            self.log(f"📄 Story context: {story_file}")

        output_file = self.tmp_dir / "develop-stories-output.txt"
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_dev_story_prompt(
                epic_id,
                sprint_status,
                story_files,
                sprint_status_file=sprint_status_file,
                workspace_root=workspace_root,
            ),
            output_file=output_file,
            cwd=workspace_root,
            reasoning_effort="high",
            max_attempts=2,
            phase_name="epic-development",
            contract=dedent(
                """
                Return YAML frontmatter only with:
                - workflow_status: stories_complete | stories_blocked
                - epic_id: exact epic id
                - story_status: review | in-progress
                - blocking_reason: required only when blocked
                - If blocked, leave the story/status context in-progress and let the orchestrator retry later.
                """
            ).strip(),
            validator=lambda output_text: self.validate_epic_progress(
                output_text=output_text,
                expected_epic_id=epic_id,
                story_files=story_files,
            ),
        )
        output_text = result.output_text
        parsed_output, validation_failure = self.parse_epic_dev_output(output_text, expected_epic_id=epic_id)
        if parsed_output and parsed_output.workflow_status == "stories_blocked":
            self.reroute_development_after_blocked(
                epic_id=epic_id,
                reason=parsed_output.blocking_reason or "stories development blocked",
            )
            return
        if result.return_code != 0:
            self.log("❌ Codex reported stories blocked")
            if result.validation_failure:
                self.log(f"   Validation error: {to_jsonable(result.validation_failure)}")
            self.reroute_to_development(epic_id=epic_id, reason=result.validation_failure.message if result.validation_failure else "epic development returned non-zero")
            return
        if validation_failure or not parsed_output:
            self.log("❌ Codex did not produce a valid stories-development response")
            if validation_failure:
                self.log(f"   Validation error: {to_jsonable(validation_failure)}")
            self.reroute_to_development(epic_id=epic_id, reason=validation_failure.message if validation_failure else "invalid stories-development output")
            return
        if parsed_output.workflow_status != "stories_complete":
            self.log("❌ Codex did not report stories_complete")
            self.reroute_to_development(epic_id=epic_id, reason=f"unexpected workflow_status: {parsed_output.workflow_status}")
            return

        self.log("Running local checks gate...")
        try:
            self.autopilot_checks(workspace_root)
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after story development: {exc}")

        self.state_set(Phase.COMMIT_SPLIT, epic_id)
        self.log("✅ Stories phase complete; running commit split workflow next")

    def phase_commit_split(self) -> None:
        self.log("🪓 PHASE: COMMIT_SPLIT")

        output_file = self.tmp_dir / "commit-split-output.txt"

        if self.is_story_flow():
            story_key = self.state_current_story()
            if not story_key:
                self.log("❌ current_story missing")
                self.state_set(Phase.BLOCKED, None)
                return

            try:
                sprint_status = self.load_sprint_status()
                target = dict(sprint_status.story_entries()).get(story_key)
                if target is None:
                    raise ValueError(f"Missing story entry in sprint status: {story_key}")
                story_path = self.story_file_for_key(sprint_status, story_key)
            except ValueError as exc:
                self.log(f"❌ {exc}")
                self.state_set(Phase.BLOCKED, story_key.split("-", 1)[0] if story_key else None)
                return

            prompt = self.build_commit_split_prompt(story_key=story_key, story_path=story_path)
            after_split_state = (Phase.QA_AUTOMATION_TEST, story_key, story_path)
        else:
            epic_id = self.state_current_epic()
            if not epic_id:
                self.log("❌ current_epic missing")
                self.state_set(Phase.BLOCKED, None)
                return

            workspace_root = self.epic_workspace_root(epic_id)
            try:
                sprint_status = self.load_sprint_status(root=workspace_root)
                story_files = sprint_status.story_files_for_epic(workspace_root, epic_id)
            except ValueError as exc:
                self.log(f"❌ {exc}")
                self.state_set(Phase.BLOCKED, epic_id)
                return

            prompt = self.build_commit_split_prompt(epic_id=epic_id, story_files=story_files, repo_root=workspace_root)
            after_split_state = (Phase.QA_AUTOMATION_TEST, epic_id, None)

        commit_root = workspace_root if not self.is_story_flow() else self.project_root
        return_code = self.run_codex_exec(
            prompt,
            output_file,
            cwd=commit_root,
            reasoning_effort=self.commit_split_reasoning_effort,
        )
        output_text = read_text(output_file)
        if return_code != 0:
            self.log("❌ Codex reported commit split failed")
            if output_text.strip():
                self.verbose(output_text.strip())
            if self.is_story_flow():
                self.state_set(Phase.BLOCKED, after_split_state[1])
            else:
                self.state_set(Phase.BLOCKED, after_split_state[1])
            return

        self.log("✅ Commit split workflow complete")
        next_phase, entity_id, story_path = after_split_state
        if self.is_story_flow():
            self.state_set_story(next_phase, entity_id, story_path)
        else:
            self.state_set(next_phase, entity_id)

    def phase_qa_automation_test(self) -> None:
        if self.is_story_flow():
            self.phase_qa_automation_test_story()
            return
        self.log("🧪 PHASE: QA_AUTOMATION_TEST")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        workspace_root = self.epic_workspace_root(epic_id)
        try:
            sprint_status = self.load_sprint_status(root=workspace_root)
            story_files = sprint_status.story_files_for_epic(workspace_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return
        sprint_status_file = self.sprint_status_path(workspace_root)
        self.log(f"📄 Sprint status source: {sprint_status_file}")
        for story_file in story_files:
            self.log(f"📄 Story context: {story_file}")

        output_file = self.tmp_dir / "qa-automation-output.txt"
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_qa_prompt(epic_id, sprint_status, story_files, repo_root=workspace_root),
            output_file=output_file,
            cwd=workspace_root,
            reasoning_effort=self.codex_reasoning_effort,
            max_attempts=2,
            phase_name="epic-qa",
            contract=dedent(
                """
                Return YAML frontmatter only with:
                - review_status: pass | fail
                """
            ).strip(),
            validator=lambda output_text: None
            if self.review_status_from_output(output_text) in {"pass", "fail"}
            else ValidationFailure(
                error_code="missing_review_status",
                field="frontmatter.review_status",
                message="missing YAML frontmatter review_status",
                expected="review_status: pass | fail",
            ),
        )
        output_text = result.output_text
        return_code = result.return_code
        review_status = self.review_status_from_output(output_text)
        self.persist_review_artifact(
            "qa-review",
            phase_name=Phase.QA_AUTOMATION_TEST.value,
            source_output=output_file,
            return_code=return_code,
            output_text=output_text,
            context_lines=[
                f"Epic: {epic_id}",
                f"Sprint status: {sprint_status_file}",
                "Story files:",
                *[f"{path}" for path in story_files],
            ],
            status_hint=None,
            root=workspace_root,
        )
        if result.validation_failure:
            self.log("❌ Codex reported QA validation blocked")
            self.log(f"   Validation error: {to_jsonable(result.validation_failure)}")
            self.reroute_to_development(
                epic_id=epic_id,
                reason=result.validation_failure.message,
            )
            return
        if review_status != "pass":
            self.log("❌ Codex reported QA blocked")
            self.reroute_to_development(
                epic_id=epic_id,
                reason="QA review_status was not pass",
            )
            return

        self.log("Running local checks gate...")
        try:
            self.autopilot_checks(workspace_root)
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after QA automation: {exc}")

        self.state_set(Phase.CODE_REVIEW, epic_id)
        self.play_sound("review_ready")
        self.log("✅ QA automation complete")

    def phase_code_review(self) -> None:
        if self.is_story_flow():
            self.phase_code_review_story()
            return
        self.log("🔍 PHASE: CODE_REVIEW")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        workspace_root = self.epic_workspace_root(epic_id)
        try:
            sprint_status = self.load_sprint_status(root=workspace_root)
            story_files = sprint_status.story_files_for_epic(workspace_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        self.log(f"Running BMAD code-review workflow for epic {epic_id}")
        source = self.collect_review_source_snapshot(workspace_root)
        if not source.has_reviewable_source:
            blocked_text = "No reviewable source found in the current workspace snapshot."
            self.log(f"❌ {blocked_text}")
            self.persist_review_artifact(
                "code-review",
                phase_name=Phase.CODE_REVIEW.value,
                source_output=self.tmp_dir / "code-review-output.txt",
                return_code=1,
                output_text=blocked_text,
                context_lines=[
                    f"Epic: {epic_id}",
                    f"Base branch: origin/{self.base_branch}",
                    f"Workspace root: {workspace_root}",
                    "Story files:",
                    *[f"{path}" for path in story_files],
                ],
                status_hint="STATUS: CODE_REVIEW_BLOCKED",
                root=workspace_root,
            )
            self.reroute_to_development(epic_id=epic_id, reason=blocked_text)
            return

        output_file = self.tmp_dir / "code-review-output.txt"
        expected_fingerprint = self.review_scope_fingerprint(source)
        valid_files = set(self.review_scope_file_names(source.branch_diff))
        valid_files.update(self.review_scope_file_names(source.staged_diff))
        valid_files.update(self.review_scope_file_names(source.unstaged_diff))
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_code_review_prompt(epic_id, repo_root=workspace_root),
            output_file=output_file,
            cwd=workspace_root,
            reasoning_effort=self.codex_reasoning_effort,
            max_attempts=2,
            phase_name="epic-code-review",
            contract=dedent(
                """
                Return YAML frontmatter only with:
                - review_status: pass | fail
                - review_scope_fingerprint: exact fingerprint from the prompt
                - reviewed_files: list of reviewed file paths relative to the repository root
                """
            ).strip(),
            validator=lambda output_text: self.validate_review_output(
                output_text,
                expected_fingerprint=expected_fingerprint,
                valid_files=valid_files,
            ),
        )
        output_text = result.output_text
        return_code = result.return_code
        parsed_output, validation_failure = self.parse_review_output(
            output_text,
            expected_fingerprint=expected_fingerprint,
            valid_files=valid_files,
        )
        if result.return_code != 0:
            self.log("❌ Codex reported code review failed")
            if result.validation_failure:
                self.log(f"   Validation error: {to_jsonable(result.validation_failure)}")
            self.reroute_to_development(epic_id=epic_id, reason=result.validation_failure.message if result.validation_failure else "code review returned non-zero")
            return
        if validation_failure or not parsed_output:
            self.log("❌ Codex did not produce a valid code-review response")
            if validation_failure:
                self.log(f"   Validation error: {to_jsonable(validation_failure)}")
            self.reroute_to_development(epic_id=epic_id, reason=validation_failure.message if validation_failure else "invalid code-review output")
            return
        self.persist_review_artifact(
            "code-review",
            phase_name=Phase.CODE_REVIEW.value,
            source_output=output_file,
            return_code=return_code,
            output_text=output_text,
            context_lines=[
                f"Epic: {epic_id}",
                f"Base branch: origin/{self.base_branch}",
                f"Workspace root: {workspace_root}",
                "Story files:",
                *[f"{path}" for path in story_files],
            ],
            status_hint=None,
            root=workspace_root,
        )
        if parsed_output.review_status != "pass":
            self.log("⚠️ Codex did not report CODE_REVIEW_DONE cleanly")
            self.reroute_to_development(epic_id=epic_id, reason="code review review_status was not pass")
            return

        try:
            self.autopilot_checks(workspace_root)
        except Exception as exc:
            self.log(f"⚠️ Local checks failed after code review: {exc}")
            self.reroute_to_development(epic_id=epic_id, reason=f"local checks failed after code review: {exc}")
            return

        self.play_sound("review_complete")
        self.run_process(["git", "push"], cwd=workspace_root, check=False)
        self.state_set(Phase.CREATE_PR, epic_id)
        self.log("✅ Code review passed")

    def phase_create_pr(self) -> None:
        self.log("📝 PHASE: CREATE_PR")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        workspace_root = self.epic_workspace_root(epic_id)
        pr_number = 0
        pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=workspace_root, check=False)
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
            create_result = self.run_process(create_command, cwd=workspace_root, check=False)
            if create_result.returncode != 0:
                self.run_process(["gh", "pr", "create", "--fill"], cwd=workspace_root, check=False)
            pr_view = self.run_json(["gh", "pr", "view", "--json", "number"], cwd=workspace_root, check=False) or {}
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
        if self.is_story_flow():
            if phase == Phase.FIND_EPIC:
                self.phase_find_story()
            elif phase == Phase.CREATE_STORY:
                self.phase_create_story()
            elif phase == Phase.DEVELOP_STORIES:
                self.phase_develop_stories()
            elif phase == Phase.COMMIT_SPLIT:
                self.phase_commit_split()
            elif phase == Phase.QA_AUTOMATION_TEST:
                self.phase_qa_automation_test()
            elif phase == Phase.CODE_REVIEW:
                self.phase_code_review()
            elif phase == Phase.BLOCKED:
                self.log("⚠️ BLOCKED - manual intervention needed")
                raise SystemExit(1)
            elif phase == Phase.DONE:
                self.log("🎉 ALL STORIES COMPLETED!")
                raise SystemExit(0)
            else:
                self.log(f"❌ Unknown phase: {phase}")
                raise SystemExit(1)
            return

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
            self.log(
                f"Fix manually then rerun the launcher: {Path(sys.argv[0]).name} \"{self.config.epic_pattern}\" "
                "(resumes by default; use --no-continue to force a fresh start)"
            )
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

    def run_story_flow(self) -> None:
        if self.config.verbose_mode:
            self.log("📋 Configuration:")
            self.log(f"   ROOT_DIR: {self.project_root}")
            self.log(f"   WORKTREE_DIR: {self.worktree_dir}")
            self.log(f"   FLOW_MODE: {self.flow_mode}")
            self.log(f"   MAX_TURNS: {self.config.max_turns}")
            self.log(f"   CHECK_INTERVAL: {self.config.check_interval}s")
            self.log(f"   MAX_CHECK_WAIT: {self.config.max_check_wait} iterations")
            self.log(f"   MAX_COPILOT_WAIT: {self.config.max_copilot_wait} iterations")
            self.log(f"   DEBUG_MODE: {int(self.config.debug_mode)}")
            self.log("")

        if not self.config.epic_pattern:
            self.log("ℹ️ No epic pattern provided - will process active stories from sprint-status.yaml in order")
        if self.config.start_from:
            self.log(f"ℹ️ Start-from selector provided: {self.config.start_from}")

        stale_legacy_state = (
            self.config.continue_run
            and self.state_file.exists()
            and not self.state.current_story
            and self.state.phase not in {Phase.FIND_EPIC, Phase.CREATE_STORY, Phase.DEVELOP_STORIES, Phase.QA_AUTOMATION_TEST, Phase.CODE_REVIEW, Phase.DONE}
        )

        if not self.config.continue_run or not self.state_file.exists() or stale_legacy_state:
            if stale_legacy_state:
                self.log("ℹ️ Detected stale legacy state; resetting into story flow")
            self.log("🚀 BMAD Autopilot starting story flow (fresh; use --no-continue to force this)")
            self.state = AutopilotState.initial(self.config.parallel_mode >= 1)
            self.state_set(Phase.FIND_EPIC, None)
        else:
            self.log("🚀 BMAD Autopilot resuming story flow")

        while True:
            phase = self.state_phase()
            self.log(f"━━━ Current phase: {phase.value} ━━━")
            self.phase_dispatch()
            time.sleep(2)

    def run(self) -> None:
        self.require_tooling()
        self.confirm_dirty_worktree(self.project_root, context="story flow" if self.is_story_flow() else "legacy flow")
        self.ensure_state_file()
        self.codex_switcher.maybe_switch(self.config.cockpit_data_dir)

        if self.is_story_flow():
            self.run_story_flow()
            return

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

        if not self.config.epic_pattern:
            self.log("ℹ️ No epic pattern provided - will process ALL active epics from sprint-status.yaml in order")
        if self.config.start_from:
            self.log(f"ℹ️ Start-from selector provided: {self.config.start_from}")

        if self.config.parallel_mode >= 1:
            self.log(f"🔀 PARALLEL MODE enabled (max {self.config.max_pending_prs} concurrent PRs)")

        if not self.config.continue_run or not self.state_file.exists():
            self.log("🚀 BMAD Autopilot starting (fresh; use --no-continue to force this)")
            self.state = AutopilotState.initial(self.config.parallel_mode >= 1)
            self.state_set(Phase.CHECK_PENDING_PR, None)
        else:
            self.log("🚀 BMAD Autopilot resuming")

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
            try:
                self.worktree_create(
                    epic_id,
                    branch_name,
                    start_point=f"origin/{branch_name}",
                    prefer_existing_branch=True,
                )
            except Exception:
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
