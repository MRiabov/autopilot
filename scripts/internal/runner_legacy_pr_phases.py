#!/usr/bin/env python3
"""Legacy PR and orchestration entrypoint phases."""

from __future__ import annotations

import re
import time

from .models import AutopilotState, Phase


class LegacyPrPhasesMixin:
    def phase_find_epic(self) -> None:
        self.log("📋 PHASE: FIND_EPIC")
        try:
            sprint_status = self.load_sprint_status()
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, None)
            return

        if (
            self.config.parallel_mode >= 1
            and self.state_count_pending_prs() >= self.config.max_pending_prs
        ):
            self.log(
                "⏸️ Pending PR cap reached - waiting for review/merge before starting a new epic"
            )
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
            self.log(
                "🕒 No new epics found yet, but pending PRs still need review/merge"
            )
            self.state_set(Phase.CHECK_PENDING_PR, None)
            return

        self.log(
            "🎉 No more active epics in sprint-status.yaml and no pending PRs - ALL DONE!"
        )
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
        wt_path = self.worktree_create(
            epic_id, branch_name, start_point=f"origin/{self.base_branch}"
        )
        self.set_active_worktree(wt_path)
        self.run_process(
            ["git", "push", "-u", "origin", branch_name], cwd=wt_path, check=False
        )
        self.state_set(Phase.DEVELOP_STORIES, epic_id)
        self.log(f"✅ Branch ready: {branch_name}")

    def phase_check_pending_pr(self) -> None:
        self.log("🔎 PHASE: CHECK_PENDING_PR")
        pr_to_fix = self.check_all_pending_prs()
        if pr_to_fix:
            self.state_set(Phase.FIX_ISSUES, pr_to_fix)
            return
        if self.state_count_pending_prs() > 0:
            self.state_set(Phase.CHECK_PENDING_PR, None)
            return
        self.state_set(Phase.FIND_EPIC, None)

    def phase_create_pr(self) -> None:
        self.log("📦 PHASE: CREATE_PR")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        workspace_root = self.epic_workspace_root(epic_id)
        branch_name = f"feature/epic-{epic_id}"
        result = self.run_process(
            [
                "gh",
                "pr",
                "create",
                "--fill",
                "--title",
                f"Epic {epic_id}",
                "--head",
                branch_name,
            ],
            cwd=workspace_root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            self.log("❌ Failed to create PR")
            if (result.stderr or result.stdout).strip():
                self.verbose((result.stderr or result.stdout).strip())
            self.state_set(Phase.BLOCKED, epic_id)
            return

        match = re.search(r"#?(\d+)", (result.stdout or result.stderr or ""))
        pr_number = int(match.group(1)) if match else 0
        if pr_number <= 0:
            self.log("⚠️ PR created but the number could not be parsed cleanly")
            self.state_set(Phase.CHECK_PENDING_PR, epic_id)
            return

        self.state_add_pending_pr(epic_id, pr_number, str(workspace_root))
        self.state_set(Phase.CHECK_PENDING_PR, epic_id)
        self.log(f"✅ PR created for epic {epic_id}")

    def phase_wait_copilot(self) -> None:
        self.log("⏳ PHASE: WAIT_COPILOT")
        pr_to_fix = self.check_all_pending_prs()
        if pr_to_fix:
            self.state_set(Phase.FIX_ISSUES, pr_to_fix)
            return
        if self.state_count_pending_prs() == 0:
            self.state_set(Phase.FIND_EPIC, None)
            return
        self.state_set(Phase.WAIT_CHECKS, self.state_current_epic())

    def phase_wait_checks(self) -> None:
        self.log("⏳ PHASE: WAIT_CHECKS")
        pr_to_fix = self.check_all_pending_prs()
        if pr_to_fix:
            self.state_set(Phase.FIX_ISSUES, pr_to_fix)
            return
        if self.state_count_pending_prs() == 0:
            self.state_set(Phase.FIND_EPIC, None)
            return
        self.state_set(Phase.WAIT_CHECKS, self.state_current_epic())

    def phase_fix_issues(self) -> None:
        self.log("🛠️ PHASE: FIX_ISSUES")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        workspace_root = self.epic_workspace_root(epic_id)
        try:
            self.autopilot_checks(workspace_root)
        except Exception as exc:
            self.log(f"⚠️ Local checks failed during fix-issues: {exc}")
        self.state_set(Phase.CODE_REVIEW, epic_id)

    def phase_merge_pr(self) -> None:
        self.log("🔀 PHASE: MERGE_PR")
        pr_to_fix = self.check_all_pending_prs()
        if pr_to_fix:
            self.state_set(Phase.FIX_ISSUES, pr_to_fix)
            return
        if self.state_count_pending_prs() == 0:
            self.state_set(Phase.FIND_EPIC, None)
            return
        self.state_set(Phase.MERGE_PR, self.state_current_epic())

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
            self.log(
                "ℹ️ No epic pattern provided - will process active stories from sprint-status.yaml in order"
            )
        if self.config.start_from:
            self.log(f"ℹ️ Start-from selector provided: {self.config.start_from}")

        stale_legacy_state = (
            self.config.continue_run
            and self.state_file.exists()
            and not self.state.current_story
            and self.state.phase
            not in {
                Phase.FIND_EPIC,
                Phase.CREATE_STORY,
                Phase.DEVELOP_STORIES,
                Phase.QA_AUTOMATION_TEST,
                Phase.CODE_REVIEW,
                Phase.EPIC_REVIEW,
                Phase.DONE,
            }
        )
        if (
            not self.config.continue_run
            or not self.state_file.exists()
            or stale_legacy_state
        ):
            if stale_legacy_state:
                self.log("ℹ️ Detected stale legacy state; resetting into story flow")
            self.log(
                "🚀 BMAD Autopilot starting story flow (fresh; use --no-continue to force this)"
            )
            self.state = AutopilotState.initial(self.config.parallel_mode >= 1)
            self.state_set(Phase.FIND_EPIC, None)
        else:
            self.log("🚀 BMAD Autopilot resuming story flow")

        while True:
            phase = self.state_phase()
            self.log(f"━━━ Current phase: {phase.value} ━━━")
            self.phase_dispatch()
            time.sleep(2)

    def run_legacy_flow(self) -> None:
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

        if not self.config.continue_run or not self.state_file.exists():
            self.log(
                "🚀 BMAD Autopilot starting legacy flow (fresh; use --no-continue to force this)"
            )
            self.state = AutopilotState.initial(self.config.parallel_mode >= 1)
            self.state_set(
                Phase.CHECK_PENDING_PR
                if self.state_count_pending_prs() > 0
                else Phase.FIND_EPIC,
                None,
            )
        else:
            self.log("🚀 BMAD Autopilot resuming legacy flow")

        while True:
            phase = self.state_phase()
            self.log(f"━━━ Current phase: {phase.value} ━━━")
            self.phase_dispatch()
            time.sleep(2)

    def run(self) -> None:
        if self.config.verbose_mode:
            self.log("📋 Starting BMAD Autopilot")
        self.require_tooling()
        self.confirm_dirty_worktree(self.project_root, context=f"{self.flow_mode} flow")
        if self.is_story_flow():
            self.run_story_flow()
        else:
            self.run_legacy_flow()
