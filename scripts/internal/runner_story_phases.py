from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional, Sequence

from .models import Phase, SprintStatusValue, ValidationFailure
from .utils import read_text, to_jsonable, utc_now, write_text


class StoryFlowPhasesMixin:
    def next_completed_epic_for_review(self, sprint_status) -> str | None:
        for epic_id in sprint_status.epics_pending_retrospective():
            return epic_id
        return None

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
            next_epic = self.next_completed_epic_for_review(sprint_status)
            if next_epic:
                self.log(f"🪞 Found completed epic pending retrospective: {next_epic}")
                self.state_set(Phase.EPIC_REVIEW, next_epic)
                return

            self.log("🎉 No more active stories or pending epic retrospectives in sprint-status.yaml")
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

    def phase_review_epic(self) -> None:
        self.log("🪞 PHASE: EPIC_REVIEW")
        epic_id = self.state_current_epic()
        if not epic_id:
            self.log("❌ current_epic missing")
            self.state_set(Phase.BLOCKED, None)
            return

        try:
            sprint_status = self.load_sprint_status()
            sprint_status.story_files_for_epic(self.project_root, epic_id)
            if not sprint_status.epic_is_complete(epic_id):
                raise ValueError(f"Epic {epic_id} is not complete yet")
        except ValueError as exc:
            self.log(f"❌ {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        if sprint_status.epic_status(epic_id) != SprintStatusValue.DONE:
            try:
                self.mark_epic_done(epic_id)
            except Exception as exc:
                self.log(f"❌ Failed to mark epic done: {exc}")
                self.state_set(Phase.BLOCKED, epic_id)
                return
            self.log(f"📝 Updated epic {epic_id} status to done")

        if not self.run_retrospective_for_epic(epic_id):
            self.state_set(Phase.BLOCKED, epic_id)
            return

        try:
            self.mark_epic_retrospective_done(epic_id)
            self.state_mark_completed(epic_id)
        except Exception as exc:
            self.log(f"❌ Failed to mark epic retrospective done: {exc}")
            self.state_set(Phase.BLOCKED, epic_id)
            return

        self.state_set(Phase.FIND_EPIC, None)
        self.log(f"✅ Epic {epic_id} complete; retrospective recorded")

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

        review_context = self.latest_review_artifact_for_story(story_key, root=self.project_root)
        review_kind: str | None = None
        review_artifact_path: Path | None = None
        if review_context is not None:
            review_kind, review_artifact_path = review_context

        output_file = self.tmp_dir / "develop-story-output.txt"
        result = self.run_codex_session_with_retry(
            initial_prompt=self.build_story_dev_prompt(
                story_key,
                story_path,
                self.sprint_status_file,
                workspace_root=self.project_root,
                review_kind=review_kind,
                review_artifact_path=review_artifact_path,
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
                Return YAML frontmatter first, then a freeform Markdown review body.
                Use the body for findings, rationale, and follow-up notes; do not stop at file names.
                If there are findings, write them as Markdown bullets or a `## Review Findings` section.
                If the review is clean, still include a short Markdown note explaining that no issues were found.

                Frontmatter fields:
                - review_status: pass | fail
                - review_scope_fingerprint: exact fingerprint from the prompt
                - reviewed_files: list of reviewed file paths relative to the repository root
                  Additional repo-relative files you consulted are allowed.
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
