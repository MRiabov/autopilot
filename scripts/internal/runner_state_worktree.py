from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

import yaml
from pydantic import ValidationError

from internal.models import (
    AutopilotState,
    PausedContext,
    PendingPR,
    Phase,
    SprintStatus,
    SprintStatusValue,
    StoryTarget,
)
from internal.utils import read_text, to_jsonable, utc_now, write_text


class RunnerStateWorktreeMixin:
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
        write_text(
            self.state_file, json.dumps(to_jsonable(self.state), indent=2) + "\n"
        )

    def state_phase(self) -> Phase:
        return self.state.effective_phase

    def state_current_epic(self) -> str | None:
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

    def state_current_story(self) -> str | None:
        return self.state.current_story

    def state_set_story(
        self, phase: Phase | str, story_key: str, story_file: Path | None = None
    ) -> None:
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

    def _rewrite_sprint_status_key(self, key: str, status_text: str) -> None:
        sprint_status_file = self.sprint_status_file
        if not sprint_status_file.exists():
            return
        raw = yaml.safe_load(read_text(sprint_status_file, "{}"))
        if not isinstance(raw, dict):
            return
        development_status = raw.get("development_status")
        if not isinstance(development_status, dict):
            development_status = {}
            raw["development_status"] = development_status
        development_status[key] = status_text
        write_text(sprint_status_file, yaml.safe_dump(raw, sort_keys=False))

    def mark_epic_done(self, epic_id: str) -> None:
        self._rewrite_sprint_status_key(f"epic-{epic_id}", SprintStatusValue.DONE.value)

    def mark_epic_retrospective_done(self, epic_id: str) -> None:
        self._rewrite_sprint_status_key(
            f"epic-{epic_id}-retrospective", SprintStatusValue.DONE.value
        )

    def state_add_pending_pr(self, epic_id: str, pr_number: int, wt_path: str) -> None:
        self.state.pending_prs = [
            pr for pr in self.state.pending_prs if pr.epic != epic_id
        ]
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

    def state_get_pending_pr(self, epic_id: str) -> PendingPR | None:
        for pr in self.state.pending_prs:
            if pr.epic == epic_id:
                return pr
        return None

    def state_update_pending_pr(
        self, epic_id: str, field_name: str, value: Any
    ) -> None:
        for pr in self.state.pending_prs:
            if pr.epic == epic_id and hasattr(pr, field_name):
                setattr(pr, field_name, value)
                break
        self.save_state()

    def state_remove_pending_pr(self, epic_id: str) -> None:
        self.state.pending_prs = [
            pr for pr in self.state.pending_prs if pr.epic != epic_id
        ]
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

    def _rewrite_story_status_line(self, story_path: Path, status_text: str) -> None:
        original = read_text(story_path, "")
        if original:
            pattern = re.compile(r"^Status:\s*.*$", re.MULTILINE)
            if pattern.search(original):
                updated = pattern.sub(f"Status: {status_text}", original, count=1)
            else:
                updated = f"Status: {status_text}\n{original.lstrip()}"
        else:
            updated = f"Status: {status_text}\n"
        write_text(story_path, updated if updated.endswith("\n") else updated + "\n")

    def _rewrite_sprint_status_story(
        self, story_path: Path, story_key: str, status_text: str
    ) -> None:
        sprint_status_file = story_path.parent / "sprint-status.yaml"
        if not sprint_status_file.exists():
            return
        raw = yaml.safe_load(read_text(sprint_status_file, "{}"))
        if not isinstance(raw, dict):
            return
        development_status = raw.get("development_status")
        if not isinstance(development_status, dict):
            development_status = {}
            raw["development_status"] = development_status
        development_status[story_key] = status_text
        write_text(sprint_status_file, yaml.safe_dump(raw, sort_keys=False))

    def mark_story_in_progress(self, story_key: str, story_path: Path) -> None:
        status_text = SprintStatusValue.IN_PROGRESS.value
        self._rewrite_story_status_line(story_path, status_text)
        self._rewrite_sprint_status_story(story_path, story_key, status_text)

    def mark_story_review(self, story_key: str, story_path: Path) -> None:
        status_text = SprintStatusValue.REVIEW.value
        self._rewrite_story_status_line(story_path, status_text)
        self._rewrite_sprint_status_story(story_path, story_key, status_text)

    def mark_story_done(self, story_key: str, story_path: Path) -> None:
        status_text = SprintStatusValue.DONE.value
        self._rewrite_story_status_line(story_path, status_text)
        self._rewrite_sprint_status_story(story_path, story_key, status_text)

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
        self.run_process(
            ["git", "fetch", "origin", self.base_branch],
            cwd=self.project_root,
            check=False,
        )

        if prefer_existing_branch:
            add_result = self.run_process(
                ["git", "worktree", "add", str(wt_path), branch_name],
                cwd=self.project_root,
                check=False,
            )
            if add_result.returncode != 0:
                start_ref = start_point or f"origin/{branch_name}"
                add_result = self.run_process(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        str(wt_path),
                        start_ref,
                    ],
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
            raise RuntimeError(
                f"Failed to create worktree for {branch_name} at {wt_path}"
            )

        self.mirror_worktree_support_dirs(wt_path)
        return wt_path

    def worktree_remove(self, epic_id: str) -> None:
        wt_path = self.worktree_path(epic_id)
        if not wt_path.exists():
            self.debug(f"Worktree does not exist: {wt_path}")
            return
        self.log(f"🗑️ Removing worktree for {epic_id}")
        self.run_process(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=self.project_root,
            check=False,
        )
        if self.state.active_worktree and Path(self.state.active_worktree) == wt_path:
            self.set_active_worktree(None)

    def worktree_prune(self) -> None:
        self.log("🧹 Pruning orphaned worktrees...")
        self.run_process(
            ["git", "worktree", "prune"], cwd=self.project_root, check=False
        )

    def sync_base_branch(self) -> None:
        self.run_process(
            ["git", "fetch", "origin", self.base_branch],
            cwd=self.project_root,
            check=False,
        )

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
            raise ValueError(
                f"Invalid sprint status YAML: {sprint_status_file}"
            ) from exc

        expected_story_root = (
            (root or self.project_root) / "_bmad-output" / "implementation-artifacts"
        )
        actual_story_root = sprint_status.normalized_story_root(
            root or self.project_root
        )
        if actual_story_root != expected_story_root:
            raise ValueError(
                "Sprint status story_location does not match the repository implementation-artifacts directory: "
                f"{actual_story_root} != {expected_story_root}"
            )

        return sprint_status

    def epic_matches_patterns(self, epic: str, sprint_status: SprintStatus) -> bool:
        if not self.config.epic_pattern:
            return True
        story_tokens = " ".join(
            key for key, _status in sprint_status.epic_story_entries(epic)
        )
        haystack = " ".join([f"epic-{epic}", epic, story_tokens])
        return any(
            re.search(pattern, haystack, re.IGNORECASE)
            for pattern in self.config.epic_pattern.split()
        )

    def story_matches_patterns(
        self, story_key: str, sprint_status: SprintStatus
    ) -> bool:
        if not self.config.epic_pattern:
            return True
        epic_id = story_key.split("-", 1)[0]
        haystack = " ".join([story_key, f"epic-{epic_id}", epic_id])
        return any(
            re.search(pattern, haystack, re.IGNORECASE)
            for pattern in self.config.epic_pattern.split()
        )

    def normalize_selection_reference(self, value: str) -> str:
        return value.strip().replace(".", "-")

    def selection_start_story_index(self, sprint_status: SprintStatus) -> int:
        start_from = self.normalize_selection_reference(self.config.start_from)
        if not start_from:
            return 0

        stories = sprint_status.story_entries()
        story_index_by_key = {
            story_key: index for index, (story_key, _status) in enumerate(stories)
        }
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

        raise ValueError(
            f"Start-from reference not found in sprint status: {self.config.start_from}"
        )

    def selection_start_epic_index(self, sprint_status: SprintStatus) -> int:
        start_from = self.normalize_selection_reference(self.config.start_from)
        if not start_from:
            return 0

        epic_match = re.fullmatch(r"(?:epic-)?(\d+)(?:-\d+)?", start_from)
        if not epic_match:
            raise ValueError(
                f"Start-from reference is not an epic selector: {self.config.start_from}"
            )

        epic_id = epic_match.group(1)
        active_epics = sprint_status.active_epic_ids()
        for index, active_epic in enumerate(active_epics):
            if active_epic == epic_id:
                return index

        raise ValueError(
            f"Start-from epic not found in active sprint epics: {self.config.start_from}"
        )

    def story_file_for_key(
        self, sprint_status: SprintStatus, story_key: str, root: Path | None = None
    ) -> Path:
        return (
            sprint_status.normalized_story_root(root or self.project_root)
            / f"{story_key}.md"
        )

    def select_next_story(self, sprint_status: SprintStatus) -> StoryTarget | None:
        stories = sprint_status.story_entries()
        start_index = self.selection_start_story_index(sprint_status)

        def select_first_matching_story(
            statuses: set[SprintStatusValue],
        ) -> StoryTarget | None:
            for story_key, story_status in stories[start_index:]:
                if story_status not in statuses or not self.story_matches_patterns(
                    story_key, sprint_status
                ):
                    continue
                story_path = self.story_file_for_key(sprint_status, story_key)
                if (
                    story_status != SprintStatusValue.BACKLOG
                    and not story_path.exists()
                ):
                    raise ValueError(
                        f"Missing story file for story {story_key}: {story_path}"
                    )
                return StoryTarget(key=story_key, path=story_path, status=story_status)
            return None

        # Review stories may preempt implementation, but implementation itself stays in file order.
        for status_group in (
            {SprintStatusValue.REVIEW},
            {SprintStatusValue.IN_PROGRESS, SprintStatusValue.READY_FOR_DEV},
            {SprintStatusValue.BACKLOG},
        ):
            target = select_first_matching_story(status_group)
            if target:
                return target
        return None

    def find_next_epic(self, sprint_status: SprintStatus) -> str | None:
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

    def gh_repo_info(self) -> tuple[str, str]:
        result = self.run_json(
            ["gh", "repo", "view", "--json", "owner,name"], cwd=self.project_root
        )
        if not result:
            raise RuntimeError("Could not determine repo info")
        return result["owner"]["login"], result["name"]

    def gh_pr_view(self, pr_number: int, fields_value: str) -> Any:
        return self.run_json(
            ["gh", "pr", "view", str(pr_number), "--json", fields_value],
            cwd=self.project_root,
            check=False,
        )

    def gh_pr_checks(self, pr_number: int) -> list[dict[str, Any]]:
        result = self.run_json(
            ["gh", "pr", "checks", str(pr_number), "--json", "name,conclusion,status"],
            cwd=self.project_root,
            check=False,
        )
        return result if isinstance(result, list) else []

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
                  reviewThreads(first: 100) { nodes { isResolved } }
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
            parts.append(
                f"📁 File: {file_path}:{line}\n" + "\n".join(comment_lines) + "\n---"
            )
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
                  reviewThreads(first: 100) { nodes { id isResolved } }
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
        unresolved = [
            node["id"]
            for node in nodes
            if not node.get("isResolved") and node.get("id")
        ]
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

    def check_pending_pr_status(
        self, epic_id: str, pr_number: int, worktree: str
    ) -> str:
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
        if any(
            str(check.get("conclusion", "")).lower() == "failure" for check in checks
        ):
            return "needs_fixes"

        ci_pending = any(
            str(check.get("status", "")).lower() != "completed"
            and str(check.get("conclusion", "")).lower() != "success"
            for check in checks
        )
        reviews = (pr_info or {}).get("reviews", []) or []
        approved = any(review.get("state") == "APPROVED" for review in reviews)
        copilot_reviews = [
            review
            for review in reviews
            if "copilot" in str(review.get("author", {}).get("login", "")).lower()
        ]
        copilot_reviews.sort(
            key=lambda review: (
                review.get("submittedAt") or review.get("createdAt") or ""
            )
        )
        if copilot_reviews and copilot_reviews[-1].get("state") == "CHANGES_REQUESTED":
            return "needs_fixes"

        if self.count_unresolved_threads(pr_number) > 0:
            return "needs_fixes"

        if approved and not ci_pending:
            return "approved"
        return "waiting"

    def check_all_pending_prs(self) -> str | None:
        pending_prs = self.state_get_all_pending_prs()
        if not pending_prs:
            self.debug("No pending PRs to check")
            return None

        self.log(f"🔍 Checking {len(pending_prs)} pending PR(s)...")
        pr_to_fix: str | None = None
        for pr in list(pending_prs):
            status = self.check_pending_pr_status(pr.epic, pr.pr_number, pr.worktree)
            if status == "approved":
                self.log(
                    f"✅ PR #{pr.pr_number} (epic {pr.epic}) is approved and ready to merge"
                )
                self.handle_approved_pr(pr.epic, pr.pr_number, pr.worktree)
            elif status == "merged":
                self.log(f"✅ PR #{pr.pr_number} (epic {pr.epic}) was already merged")
                self.handle_merged_pr(pr.epic, pr.worktree)
            elif status == "closed":
                self.log(
                    f"⚠️ PR #{pr.pr_number} (epic {pr.epic}) was closed without merge"
                )
                self.state_remove_pending_pr(pr.epic)
                self.worktree_remove(pr.epic)
            elif status == "needs_fixes":
                self.log(f"⚠️ PR #{pr.pr_number} (epic {pr.epic}) needs fixes")
                if pr_to_fix is None:
                    pr_to_fix = pr.epic
            else:
                self.debug(
                    f"PR #{pr.pr_number} (epic {pr.epic}) still waiting for review/CI"
                )
                self.state_update_pending_pr(pr.epic, "last_check", utc_now())
        return pr_to_fix

    def handle_approved_pr(self, epic_id: str, pr_number: int, wt_path: str) -> None:
        self.log(f"🔀 Merging approved PR #{pr_number} for epic {epic_id}")
        if (
            self.run_process(
                ["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"],
                cwd=self.project_root,
                check=False,
            ).returncode
            == 0
        ):
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

    def run_retrospective_for_epic(self, epic_id: str) -> bool:
        sprint_status_file = self.sprint_status_file
        retro_dir = self.project_root / "_bmad-output" / "implementation-artifacts"
        retro_dir.mkdir(parents=True, exist_ok=True)
        retro_file = (
            retro_dir
            / f"epic-{epic_id}-retro-{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )
        output_file = self.tmp_dir / "retrospective-output.txt"
        self.log(f"🪞 Running retrospective for epic {epic_id}")
        try:
            sprint_status = self.load_sprint_status()
            story_files = sprint_status.story_files_for_epic(self.project_root, epic_id)
        except ValueError as exc:
            self.log(f"❌ {exc}")
            return False
        return_code = self.run_codex_exec(
            self.build_retrospective_prompt(
                epic_id, story_files, retro_file, sprint_status_file
            ),
            output_file,
            cwd=self.project_root,
        )
        output_text = read_text(output_file)
        if return_code != 0 or "STATUS: RETROSPECTIVE_COMPLETE" not in output_text:
            self.log("⚠️ Codex did not report RETROSPECTIVE_COMPLETE cleanly")
            return False
        self.log(f"✅ Retrospective saved: {retro_file}")
        return True
