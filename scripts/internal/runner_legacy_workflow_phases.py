#!/usr/bin/env python3
"""Legacy epic workflow phase handlers."""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

from .models import Phase, SprintStatusValue, ValidationFailure
from .utils import read_text, to_jsonable


class LegacyWorkflowPhasesMixin:
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
            self.reroute_to_development(
                epic_id=epic_id,
                reason=result.validation_failure.message if result.validation_failure else "epic development returned non-zero",
            )
            return
        if validation_failure or not parsed_output:
            self.log("❌ Codex did not produce a valid stories-development response")
            if validation_failure:
                self.log(f"   Validation error: {to_jsonable(validation_failure)}")
            self.reroute_to_development(
                epic_id=epic_id,
                reason=validation_failure.message if validation_failure else "invalid stories-development output",
            )
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
            commit_root = self.project_root
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
            commit_root = workspace_root

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
        review_status = self.review_status_from_output(output_text)
        self.persist_review_artifact(
            "qa-review",
            phase_name=Phase.QA_AUTOMATION_TEST.value,
            source_output=output_file,
            return_code=result.return_code,
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
            self.reroute_to_development(epic_id=epic_id, reason=result.validation_failure.message)
            return
        if review_status != "pass":
            self.log("❌ Codex reported QA blocked")
            self.reroute_to_development(epic_id=epic_id, reason="QA review_status was not pass")
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
                epic_id=epic_id,
                reason=result.validation_failure.message if result.validation_failure else "code review returned non-zero",
            )
            return
        if validation_failure or not parsed_output:
            self.log("❌ Codex did not produce a valid code-review response")
            if validation_failure:
                self.log(f"   Validation error: {to_jsonable(validation_failure)}")
            self.reroute_to_development(
                epic_id=epic_id,
                reason=validation_failure.message if validation_failure else "invalid code-review output",
            )
            return

        self.persist_review_artifact(
            "code-review",
            phase_name=Phase.CODE_REVIEW.value,
            source_output=output_file,
            return_code=result.return_code,
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

    def phase_dispatch(self) -> None:
        phase = self.state_phase()
        if self.is_story_flow():
            if phase == Phase.FIND_EPIC:
                self.phase_find_story()
            elif phase == Phase.CREATE_STORY:
                self.phase_create_story()
            elif phase == Phase.DEVELOP_STORIES:
                self.phase_develop_story()
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
