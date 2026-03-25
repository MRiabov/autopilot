#!/usr/bin/env python3
"""Workspace update, reroute, and lightweight check helpers."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import Phase, SprintStatusValue
from .utils import read_text, write_text


class RunnerUpdateMixin:
    def _story_status_text(self, status: SprintStatusValue | str) -> str:
        if isinstance(status, SprintStatusValue):
            return status.value
        return str(status).strip() or SprintStatusValue.IN_PROGRESS.value

    def _rewrite_story_status(self, story_path: Path, status_text: str) -> None:
        if story_path.exists():
            original = read_text(story_path)
            pattern = re.compile(r"^Status:\s*.*$", re.MULTILINE)
            if pattern.search(original):
                updated = pattern.sub(f"Status: {status_text}", original, count=1)
            else:
                updated = f"Status: {status_text}\n{original.lstrip()}"
        else:
            updated = f"Status: {status_text}\n"
        write_text(story_path, updated if updated.endswith("\n") else updated + "\n")

    def _rewrite_sprint_status(
        self, story_path: Path, story_key: str, status_text: str
    ) -> None:
        sprint_status_file = story_path.parent / "sprint-status.yaml"
        if not sprint_status_file.exists():
            return

        data = yaml.safe_load(read_text(sprint_status_file, "{}"))
        if not isinstance(data, dict):
            return

        development_status = data.get("development_status")
        if not isinstance(development_status, dict):
            development_status = {}
            data["development_status"] = development_status
        development_status[story_key] = status_text
        write_text(sprint_status_file, yaml.safe_dump(data, sort_keys=False))

    def mark_story_in_progress(self, story_key: str, story_path: Path) -> None:
        status_text = SprintStatusValue.IN_PROGRESS.value
        self._rewrite_story_status(story_path, status_text)
        self._rewrite_sprint_status(story_path, story_key, status_text)

    def mark_story_review(self, story_key: str, story_path: Path) -> None:
        status_text = SprintStatusValue.REVIEW.value
        self._rewrite_story_status(story_path, status_text)
        self._rewrite_sprint_status(story_path, story_key, status_text)

    def mark_story_done(self, story_key: str, story_path: Path) -> None:
        status_text = SprintStatusValue.DONE.value
        self._rewrite_story_status(story_path, status_text)
        self._rewrite_sprint_status(story_path, story_key, status_text)

    def reroute_to_development(
        self,
        *,
        epic_id: str,
        reason: str,
        story_key: str | None = None,
        story_path: Path | None = None,
    ) -> None:
        self.log(f"↩️ Rerouting to development: {reason}")
        if story_key:
            if story_path is not None:
                self.mark_story_in_progress(story_key, story_path)
            self.state_set_story(Phase.DEVELOP_STORIES, story_key, story_path)
            return
        self.state_set(Phase.DEVELOP_STORIES, epic_id)

    def reroute_development_after_blocked(
        self,
        *,
        epic_id: str,
        reason: str,
        story_key: str | None = None,
        story_path: Path | None = None,
    ) -> None:
        self.log(f"⚠️ Development blocked: {reason}")
        self.reroute_to_development(
            epic_id=epic_id,
            reason=reason,
            story_key=story_key,
            story_path=story_path,
        )

    def autopilot_checks(self, root: Path | None = None) -> None:
        check_root = root or self.project_root
        candidates = [
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_core.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_story_phases.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_legacy_workflow_phases.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_legacy_pr_phases.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_review.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_state_worktree.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_update.py",
            self.project_root
            / ".autopilot"
            / "scripts"
            / "internal"
            / "runner_environment.py",
        ]
        existing = [str(path) for path in candidates if path.exists()]
        if not existing:
            self.verbose(
                f"Skipping autopilot checks for {check_root}: no local check targets found."
            )
            return
        self.run_process(
            ["python3", "-m", "py_compile", *existing], cwd=check_root, check=True
        )
