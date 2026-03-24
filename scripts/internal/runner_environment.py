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
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable, Sequence

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
    CodexAttemptResult,
    CockpitCodexAccount,
    CockpitCodexQuota,
    CockpitCodexStoreSnapshot,
    CockpitCodexSwitchCandidate,
    CockpitCodexSwitchSettings,
    CockpitCodexTokens,
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
    AutopilotState,
)
from internal.utils import read_text, timestamp, to_jsonable, utc_now, write_text


class RunnerEnvironmentMixin:
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

        if getattr(self.state, "active_worktree", None):
            active_root = Path(self.state.active_worktree)
            if active_root.exists():
                return active_root

        return self.project_root

    def confirm_dirty_worktree(self, root: Path, *, context: str) -> None:
        dirty = self.run_text(["git", "status", "--short"], cwd=root, check=False, capture_output=True).strip()
        if not dirty:
            return
        self.log("⚠️ WARNING: Git working tree has uncommitted changes")
        self.log(f"   Context: {context}")
        self.log(f"   Root: {root}")
        if self.config.accept_dirty_worktree:
            self.log("   Dirty worktree accepted via --accept-dirty-worktree.")
            return

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
        branch_diff = filter_internal_paths(self.run_text(["git", "diff", "--name-only", f"origin/{base_branch}..HEAD"], cwd=repo_root, check=False, capture_output=True))
        staged_diff = filter_internal_paths(self.run_text(["git", "diff", "--name-only", "--cached"], cwd=repo_root, check=False, capture_output=True))
        unstaged_diff = filter_internal_paths(self.run_text(["git", "diff", "--name-only"], cwd=repo_root, check=False, capture_output=True))
        working_tree_status = filter_internal_paths(self.run_text(["git", "status", "--short"], cwd=repo_root, check=False, capture_output=True))
        working_tree_status = "\n".join(
            line for line in working_tree_status.splitlines()
            if not line.startswith("?? .autopilot/") and not line.startswith("?? .autopilot")
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
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
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
            base_branch=env_or_file("AUTOPILOT_BASE_BRANCH", ""),
            codex_switch_mode=env_or_file("AUTOPILOT_CODEX_SWITCH_MODE", "auto").strip().lower() or "auto",
            codex_switch_primary_threshold=self.to_int(env_or_file("AUTOPILOT_CODEX_SWITCH_PRIMARY_THRESHOLD", "20"), 20),
            codex_switch_secondary_threshold=self.to_int(env_or_file("AUTOPILOT_CODEX_SWITCH_SECONDARY_THRESHOLD", "20"), 20),
            cockpit_data_dir=env_or_file("AUTOPILOT_COCKPIT_DATA_DIR", ""),
            accept_dirty_worktree=bool(self.args.accept_dirty_worktree),
            quota_retry_seconds=self.to_int(
                env_or_file("AUTOPILOT_QUOTA_RETRY_SECONDS", env_or_file("AUTOPILOT_DEVELOPMENT_BLOCKED_RETRY_SECONDS", "1800")),
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
        self.debug_log.parent.mkdir(parents=True, exist_ok=True)
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
            raise RuntimeError(stderr or stdout or "command failed")
        return result

    def run_json(self, command: Sequence[str], *, cwd: Path | None = None, check: bool = True) -> Any:
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
                    if line:
                        print(line, end="")
                        out_fh.write(line)
                        out_fh.flush()
                        continue
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)

            return proc.wait()

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

                sample = int(32767 * amplitude * envelope * math.sin(2.0 * math.pi * frequency * (sample_index / sample_rate)))
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
        patterns = (r"\bquota\b", r"out of credits", r"insufficient credits?", r"rate limit exceeded", r"billing")
        return any(re.search(pattern, text) for pattern in patterns)

    def run_codex_exec(
        self,
        prompt: str,
        output_file: Path | None = None,
        *,
        cwd: Path | None = None,
        reasoning_effort: str | None = None,
    ) -> int:
        return self.run_codex_session(prompt, output_file=output_file, cwd=cwd, reasoning_effort=reasoning_effort).return_code

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
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
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
