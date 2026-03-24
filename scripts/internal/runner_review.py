#!/usr/bin/env python3
"""Review, validation, and prompt helpers for BMAD Autopilot."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

import yaml
from pydantic import ValidationError

from .models import EpicDevOutput, ReviewDecisionOutput, ReviewSourceSnapshot, StoryDevOutput, ValidationFailure
from .utils import read_text, to_jsonable, write_text


class RunnerReviewMixin:
    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
        stripped = text.lstrip()
        if not stripped.startswith("---"):
            return None, text

        lines = stripped.splitlines()
        if not lines or lines[0].strip() != "---":
            return None, text

        end_index = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_index = index
                break
        if end_index is None:
            return None, text

        frontmatter_text = "\n".join(lines[1:end_index]).strip()
        body = "\n".join(lines[end_index + 1 :])
        if not frontmatter_text:
            return {}, body

        data = yaml.safe_load(frontmatter_text)
        return data if isinstance(data, dict) else {}, body

    def review_status_from_output(self, output_text: str) -> str | None:
        frontmatter, _body = self._split_frontmatter(output_text)
        if not frontmatter:
            return None
        status = str(frontmatter.get("review_status", "")).strip()
        return status or None

    def review_scope_file_names(self, text: str) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if " -> " in line:
                line = line.rsplit(" -> ", 1)[-1]
            parts = line.split()
            if len(parts) > 1 and parts[0] in {"M", "A", "D", "R", "C", "U", "??"}:
                line = parts[-1]
            line = line.replace("\\", "/")
            if line not in seen:
                seen.add(line)
                names.append(line)
        return names

    def review_scope_fingerprint(self, source: ReviewSourceSnapshot) -> str:
        payload = {
            "branch_diff": self.review_scope_file_names(source.branch_diff),
            "current_branch": source.current_branch,
            "has_reviewable_source": source.has_reviewable_source,
            "staged_diff": self.review_scope_file_names(source.staged_diff),
            "unstaged_diff": self.review_scope_file_names(source.unstaged_diff),
            "working_tree_status": self.review_scope_file_names(source.working_tree_status),
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return digest

    def latest_review_artifacts(self, root: Path | None = None) -> dict[str, Path]:
        artifact_dir = (root or self.project_root) / "_bmad-outputs" / "review-artifacts"
        if not artifact_dir.exists():
            return {}

        latest: dict[str, tuple[float, Path]] = {}
        for path in artifact_dir.glob("*.md"):
            stem = path.stem
            kind = stem.rsplit("-", 1)[0] if "-" in stem else stem
            try:
                stamp = path.stat().st_mtime
            except FileNotFoundError:
                continue
            current = latest.get(kind)
            if current is None or stamp >= current[0]:
                latest[kind] = (stamp, path)
        return {kind: path for kind, (_stamp, path) in latest.items()}

    def review_status_from_artifact(self, kind: str, root: Path | None = None) -> str | None:
        artifact = self.latest_review_artifacts(root=root).get(kind)
        if not artifact or not artifact.exists():
            return None
        frontmatter, _body = self._split_frontmatter(read_text(artifact))
        if not frontmatter:
            return None
        status = str(frontmatter.get("review_status", "")).strip()
        return status or None

    def latest_review_artifact_for_story(
        self,
        story_key: str,
        root: Path | None = None,
    ) -> tuple[str, Path] | None:
        artifact_dir = (root or self.project_root) / "_bmad-outputs" / "review-artifacts"
        if not artifact_dir.exists():
            return None

        markers = (f"Story: {story_key}", f"Story key: {story_key}")
        matches: list[tuple[float, str, Path]] = []
        for path in artifact_dir.glob("*.md"):
            try:
                artifact_text = read_text(path)
            except OSError:
                continue
            if not any(marker in artifact_text for marker in markers):
                continue
            try:
                stamp = path.stat().st_mtime
            except FileNotFoundError:
                continue
            kind = path.stem.rsplit("-", 1)[0] if "-" in path.stem else path.stem
            matches.append((stamp, kind, path))

        if not matches:
            return None

        matches.sort(key=lambda item: item[0], reverse=True)
        _stamp, kind, path = matches[0]
        return kind, path

    def persist_review_artifact(
        self,
        kind: str,
        *,
        phase_name: str,
        source_output: Path,
        return_code: int,
        output_text: str,
        context_lines: list[str] | tuple[str, ...] | None = None,
        status_hint: str | None = None,
        root: Path | None = None,
    ) -> Path:
        workspace_root = root or self.project_root
        artifact_dir = workspace_root / "_bmad-outputs" / "review-artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        review_status = self.review_status_from_output(output_text)
        if review_status is None and status_hint:
            review_status = "fail" if "blocked" in status_hint.lower() or "fail" in status_hint.lower() else "pass"
        if review_status is None:
            review_status = "fail"

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        artifact_path = artifact_dir / f"{kind}-{stamp}.md"
        lines = [
            "---",
            f"review_status: {review_status}",
            "---",
            f"phase_name: {phase_name}",
            f"source_output: {source_output}",
            f"return_code: {return_code}",
        ]
        if status_hint:
            lines.append(f"status_hint: {status_hint}")
        if context_lines:
            lines.append("")
            lines.extend(str(line) for line in context_lines)
        lines.append("")
        lines.append("output:")
        lines.append(output_text.rstrip())
        lines.append("")

        write_text(artifact_path, "\n".join(lines))
        return artifact_path

    def _parse_review_output(
        self,
        output_text: str,
        *,
        expected_fingerprint: str,
        valid_files: set[str],
    ) -> tuple[ReviewDecisionOutput | None, ValidationFailure | None]:
        frontmatter, _body = self._split_frontmatter(output_text)
        if not frontmatter:
            return None, ValidationFailure(
                error_code="missing_review_frontmatter",
                field="frontmatter",
                message="missing YAML frontmatter",
                expected="review_status, review_scope_fingerprint, reviewed_files",
            )

        try:
            parsed = ReviewDecisionOutput.model_validate(frontmatter)
        except ValidationError as exc:
            return None, ValidationFailure(
                error_code="invalid_review_output",
                field="frontmatter",
                message=str(exc),
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
                message="reviewed_files contains paths outside the review scope",
                expected=", ".join(sorted(valid_files)) if valid_files else "(no reviewable files)",
            )

        return parsed, None

    def validate_review_output(
        self,
        output_text: str,
        *,
        expected_fingerprint: str,
        valid_files: set[str],
    ) -> ValidationFailure | None:
        _parsed, failure = self._parse_review_output(
            output_text,
            expected_fingerprint=expected_fingerprint,
            valid_files=valid_files,
        )
        return failure

    def parse_review_output(
        self,
        output_text: str,
        *,
        expected_fingerprint: str,
        valid_files: set[str],
    ) -> tuple[ReviewDecisionOutput | None, ValidationFailure | None]:
        return self._parse_review_output(
            output_text,
            expected_fingerprint=expected_fingerprint,
            valid_files=valid_files,
        )

    def _parse_story_or_epic_output(
        self,
        output_text: str,
        *,
        expected_key: str,
        model: type[StoryDevOutput] | type[EpicDevOutput],
        key_field: str,
    ) -> tuple[StoryDevOutput | EpicDevOutput | None, ValidationFailure | None]:
        frontmatter, _body = self._split_frontmatter(output_text)
        if not frontmatter:
            return None, ValidationFailure(
                error_code="missing_dev_frontmatter",
                field="frontmatter",
                message="missing YAML frontmatter",
                expected=f"{key_field}, workflow_status, story_status",
            )

        try:
            parsed = model.model_validate(frontmatter)
        except ValidationError as exc:
            return None, ValidationFailure(
                error_code="invalid_dev_output",
                field="frontmatter",
                message=str(exc),
                expected=f"{key_field}, workflow_status, story_status",
            )

        actual_key = getattr(parsed, key_field)
        if actual_key != expected_key:
            return None, ValidationFailure(
                error_code=f"mismatched_{key_field}",
                field=key_field,
                message=f"{key_field} does not match the current workspace snapshot",
                expected=expected_key,
            )

        return parsed, None

    def validate_story_progress(
        self,
        *,
        output_text: str,
        expected_story_key: str,
        story_path: Path | None,
        sprint_status_root: Path,
    ) -> ValidationFailure | None:
        _parsed, failure = self.parse_story_dev_output(
            output_text,
            expected_story_key=expected_story_key,
        )
        return failure

    def parse_story_dev_output(
        self,
        output_text: str,
        *,
        expected_story_key: str,
    ) -> tuple[StoryDevOutput | None, ValidationFailure | None]:
        return self._parse_story_or_epic_output(
            output_text,
            expected_key=expected_story_key,
            model=StoryDevOutput,
            key_field="story_key",
        )

    def validate_epic_progress(
        self,
        *,
        output_text: str,
        expected_epic_id: str,
        story_files: list[Path],
    ) -> ValidationFailure | None:
        _parsed, failure = self.parse_epic_dev_output(
            output_text,
            expected_epic_id=expected_epic_id,
        )
        return failure

    def parse_epic_dev_output(
        self,
        output_text: str,
        *,
        expected_epic_id: str,
    ) -> tuple[EpicDevOutput | None, ValidationFailure | None]:
        return self._parse_story_or_epic_output(
            output_text,
            expected_key=expected_epic_id,
            model=EpicDevOutput,
            key_field="epic_id",
        )

    def _render_context_block(self, heading: str, lines: list[str]) -> str:
        rendered = [heading]
        rendered.extend(f"- {line}" for line in lines if str(line).strip())
        return "\n".join(rendered)

    def build_story_create_prompt(self, story_key: str, story_path: Path) -> str:
        return dedent(
            f"""
            Create or update the story document for {story_key}.

            Story path:
            {story_path}

            Keep the output focused on the story content and preserve the repository
            conventions in the story file.
            """
        ).strip() + "\n"

    def build_story_dev_prompt(
        self,
        story_key: str,
        story_path: Path,
        sprint_status_file: Path | None = None,
        *,
        workspace_root: Path | None = None,
        review_kind: str | None = None,
        review_artifact_path: Path | None = None,
    ) -> str:
        lines = [
            f"Story key: {story_key}",
            f"Story path: {story_path}",
        ]
        if sprint_status_file:
            lines.append(f"Sprint status: {sprint_status_file}")
        if workspace_root:
            lines.append(f"Workspace root: {workspace_root}")
        if review_artifact_path is not None:
            review_label = (review_kind or "review").replace("-", " ")
            lines.extend(
                [
                    "",
                    "Prior review detected:",
                    f"- There was a {review_label} at: {review_artifact_path}",
                    "- Read that review before continuing.",
                ]
            )
        return dedent(
            "\n".join(
                [
                    "Develop the following story to completion.",
                    "",
                    *lines,
                    "",
                    "Return YAML frontmatter only with:",
                    "- workflow_status: stories_complete | stories_blocked",
                    "- story_key: exact story key",
                    "- story_status: review | in-progress",
                    "- blocking_reason: required only when blocked",
                ]
            )
        ).strip() + "\n"

    def build_dev_story_prompt(self, *args: Any, **kwargs: Any) -> str:
        sprint_status = kwargs.get("sprint_status")
        if len(args) >= 3 and hasattr(args[1], "story_entries"):
            epic_id = str(args[0])
            sprint_status = args[1]
            story_files = list(args[2])
            sprint_status_file = kwargs.get("sprint_status_file")
            workspace_root = kwargs.get("workspace_root")
        elif sprint_status is not None and hasattr(sprint_status, "story_entries"):
            epic_id = str(kwargs.get("epic_id", args[0] if args else ""))
            story_files = list(kwargs.get("story_files", []))
            sprint_status_file = kwargs.get("sprint_status_file")
            workspace_root = kwargs.get("workspace_root")
            lines = [
                f"Epic id: {epic_id}",
                f"Sprint status source: {sprint_status_file}" if sprint_status_file else None,
                f"Workspace root: {workspace_root}" if workspace_root else None,
                "",
                "Story files:",
            ]
            lines.extend(f"- {path}" for path in story_files)
            lines.extend(
                [
                    "",
                    "Return YAML frontmatter only with:",
                    "- workflow_status: stories_complete | stories_blocked",
                    "- epic_id: exact epic id",
                    "- story_status: review | in-progress",
                    "- blocking_reason: required only when blocked",
                ]
            )
            return dedent("\n".join(str(line) for line in lines if line is not None)).strip() + "\n"

        return self.build_story_dev_prompt(*args, **kwargs)

    def build_story_qa_prompt(self, story_key: str, story_path: Path) -> str:
        return dedent(
            f"""
            Review the implementation for story {story_key} and decide whether QA passes.

            Story path:
            {story_path}

            Return YAML frontmatter only with:
            - review_status: pass | fail
            """
        ).strip() + "\n"

    def build_qa_prompt(
        self,
        epic_id: str,
        sprint_status: Any,
        story_files: list[Path],
        *,
        repo_root: Path | None = None,
    ) -> str:
        lines = [
            f"Epic id: {epic_id}",
            f"Repository root: {repo_root}" if repo_root else None,
            "",
            "Story files:",
        ]
        lines.extend(f"- {path}" for path in story_files)
        lines.extend(
            [
                "",
                "Return YAML frontmatter only with:",
                "- review_status: pass | fail",
            ]
        )
        return dedent("\n".join(str(line) for line in lines if line is not None)).strip() + "\n"

    def build_story_code_review_prompt(
        self,
        story_key: str,
        story_path: Path,
        *,
        workspace_root: Path | None = None,
    ) -> str:
        lines = [
            f"Story key: {story_key}",
            f"Story path: {story_path}",
            f"Workspace root: {workspace_root}" if workspace_root else None,
            "",
            "Review the current workspace snapshot for this story.",
            "Return YAML frontmatter only with:",
            "- review_status: pass | fail",
            "- review_scope_fingerprint: exact fingerprint from the prompt",
            "- reviewed_files: list of reviewed file paths relative to the repository root",
        ]
        return dedent("\n".join(str(line) for line in lines if line is not None)).strip() + "\n"

    def build_code_review_prompt(
        self,
        epic_id: str,
        *,
        repo_root: Path | None = None,
    ) -> str:
        source = self.collect_review_source_snapshot(repo_root or self.project_root)
        reviewed_lines = self.review_scope_file_names(source.branch_diff)
        reviewed_lines.extend(self.review_scope_file_names(source.staged_diff))
        reviewed_lines.extend(self.review_scope_file_names(source.unstaged_diff))
        lines = [
            f"Epic id: {epic_id}",
            f"Repository root: {repo_root or self.project_root}",
            f"Current branch: {source.current_branch}",
            "",
            "Reviewable files:",
        ]
        lines.extend(f"- {path}" for path in reviewed_lines or ["(none)"])
        lines.extend(
            [
                "",
                "Return YAML frontmatter only with:",
                "- review_status: pass | fail",
                "- review_scope_fingerprint: exact fingerprint from the prompt",
                "- reviewed_files: list of reviewed file paths relative to the repository root",
            ]
        )
        return dedent("\n".join(str(line) for line in lines if line is not None)).strip() + "\n"

    def build_commit_split_prompt(
        self,
        *,
        story_key: str | None = None,
        story_path: Path | None = None,
        epic_id: str | None = None,
        story_files: list[Path] | None = None,
        repo_root: Path | None = None,
    ) -> str:
        lines = ["Split the current changes into a clean commit."]
        if story_key and story_path:
            lines.extend([f"Story key: {story_key}", f"Story path: {story_path}"])
        if epic_id and story_files is not None:
            lines.append(f"Epic id: {epic_id}")
            lines.append("Story files:")
            lines.extend(f"- {path}" for path in story_files)
        if repo_root:
            lines.append(f"Repository root: {repo_root}")
        return dedent("\n".join(lines)).strip() + "\n"

    def build_retrospective_prompt(
        self,
        epic_id: str,
        story_files: list[Path],
        retro_file: Path,
        sprint_status_file: Path,
    ) -> str:
        lines = [
            f"Epic id: {epic_id}",
            f"Retro file: {retro_file}",
            f"Sprint status file: {sprint_status_file}",
            "",
            "Story files:",
        ]
        lines.extend(f"- {path}" for path in story_files)
        lines.extend(
            [
                "",
                "Document the retrospective and return STATUS: RETROSPECTIVE_COMPLETE when done.",
            ]
        )
        return dedent("\n".join(lines)).strip() + "\n"

    def build_story_code_review_prompt_alias(self, *args: Any, **kwargs: Any) -> str:
        return self.build_story_code_review_prompt(*args, **kwargs)
