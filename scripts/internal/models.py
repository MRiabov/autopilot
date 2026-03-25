from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Phase(str, Enum):
    CHECK_PENDING_PR = "CHECK_PENDING_PR"
    FIND_EPIC = "FIND_EPIC"
    CREATE_BRANCH = "CREATE_BRANCH"
    CREATE_STORY = "CREATE_STORY"
    DEVELOP_STORIES = "DEVELOP_STORIES"
    COMMIT_SPLIT = "COMMIT_SPLIT"
    QA_AUTOMATION_TEST = "QA_AUTOMATION_TEST"
    CODE_REVIEW = "CODE_REVIEW"
    EPIC_REVIEW = "EPIC_REVIEW"
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
    last_copilot_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingPR:
        return cls(
            epic=str(data.get("epic", "")),
            pr_number=int(data.get("pr_number", 0) or 0),
            worktree=str(data.get("worktree", "")),
            status=str(data.get("status", "WAIT_REVIEW")),
            last_check=str(data.get("last_check", "")),
            last_copilot_id=data.get("last_copilot_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "pr_number": self.pr_number,
            "worktree": self.worktree,
            "status": self.status,
            "last_check": self.last_check,
            "last_copilot_id": self.last_copilot_id,
        }


@dataclass
class PausedContext:
    epic: str
    phase: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PausedContext:
        return cls(epic=str(data.get("epic", "")), phase=str(data.get("phase", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"epic": self.epic, "phase": self.phase}


@dataclass(frozen=True)
class ReviewSourceSnapshot:
    current_branch: str
    branch_diff: str
    staged_diff: str
    unstaged_diff: str
    working_tree_status: str
    has_reviewable_source: bool


@dataclass(frozen=True)
class ValidationFailure:
    error_code: str
    field: str | None
    message: str
    expected: str | None = None


@dataclass(frozen=True)
class CodexAttemptResult:
    return_code: int
    thread_id: str | None
    output_text: str
    validation_failure: ValidationFailure | None = None


@dataclass(frozen=True)
class StoryTarget:
    key: str
    path: Path
    status: SprintStatusValue


class SprintStatusValue(str, Enum):
    BACKLOG = "backlog"
    IN_PROGRESS = "in-progress"
    DONE = "done"
    READY_FOR_DEV = "ready-for-dev"
    REVIEW = "review"
    OPTIONAL = "optional"
    BLOCKED = "blocked"


class StoryDevOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_status: Literal["stories_complete", "stories_blocked"]
    story_key: str = Field(min_length=1)
    story_status: Literal["review", "in-progress"]
    blocking_reason: str | None = None

    @model_validator(mode="after")
    def validate_story_contract(self) -> StoryDevOutput:
        if self.workflow_status == "stories_complete":
            if self.story_status != "review":
                raise ValueError(
                    "story_status must be review when workflow_status is stories_complete"
                )
            if self.blocking_reason is not None:
                raise ValueError(
                    "blocking_reason must be omitted when workflow_status is stories_complete"
                )
        elif self.workflow_status == "stories_blocked":
            if self.story_status != "in-progress":
                raise ValueError(
                    "story_status must be in-progress when workflow_status is stories_blocked"
                )
            if not (self.blocking_reason or "").strip():
                raise ValueError(
                    "blocking_reason is required when workflow_status is stories_blocked"
                )
        return self


class EpicDevOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_status: Literal["stories_complete", "stories_blocked"]
    epic_id: str = Field(min_length=1)
    story_status: Literal["review", "in-progress"]
    blocking_reason: str | None = None

    @model_validator(mode="after")
    def validate_epic_contract(self) -> EpicDevOutput:
        if self.workflow_status == "stories_complete":
            if self.story_status != "review":
                raise ValueError(
                    "story_status must be review when workflow_status is stories_complete"
                )
            if self.blocking_reason is not None:
                raise ValueError(
                    "blocking_reason must be omitted when workflow_status is stories_complete"
                )
        elif self.workflow_status == "stories_blocked":
            if self.story_status != "in-progress":
                raise ValueError(
                    "story_status must be in-progress when workflow_status is stories_blocked"
                )
            if not (self.blocking_reason or "").strip():
                raise ValueError(
                    "blocking_reason is required when workflow_status is stories_blocked"
                )
        return self


class ReviewDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_status: Literal["pass", "fail"]
    review_scope_fingerprint: str = Field(min_length=1)
    reviewed_files: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_contract(self) -> ReviewDecisionOutput:
        if not self.reviewed_files:
            raise ValueError("reviewed_files must not be empty")
        if any(not str(path).strip() for path in self.reviewed_files):
            raise ValueError("reviewed_files entries must be non-empty strings")
        return self


class SprintStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated: Any
    last_updated: Any
    project: str
    project_key: str
    tracking_system: str
    story_location: Path
    development_status: dict[str, SprintStatusValue]

    def story_entries(self) -> list[tuple[str, SprintStatusValue]]:
        return [
            (key, status)
            for key, status in self.development_status.items()
            if re.fullmatch(r"\d+-\d+-.*", key)
        ]

    def normalized_story_root(self, project_root: Path) -> Path:
        root = (
            self.story_location
            if self.story_location.is_absolute()
            else project_root / self.story_location
        )
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

    def epic_ids(self) -> list[str]:
        epic_ids: list[str] = []
        for key in self.development_status.keys():
            match = re.fullmatch(r"epic-(\d+)", key)
            if match:
                epic_ids.append(match.group(1))
        return epic_ids

    def epic_is_complete(self, epic_id: str) -> bool:
        entries = self.epic_story_entries(epic_id)
        return bool(entries) and all(
            status == SprintStatusValue.DONE for _key, status in entries
        )

    def epics_pending_retrospective(self) -> list[str]:
        pending: list[str] = []
        for epic_id in self.epic_ids():
            if not self.epic_is_complete(epic_id):
                continue
            retrospective_status = self.development_status.get(
                self.retrospective_key(epic_id)
            )
            if retrospective_status == SprintStatusValue.DONE:
                continue
            pending.append(epic_id)
        return pending

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
            raise ValueError(
                f"No story entries found in sprint status for epic {epic_id}"
            )

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
            raise ValueError(
                f"Missing story file(s) for epic {epic_id}: {missing_list}"
            )

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
    current_epic: str | None = None
    current_story: str | None = None
    current_story_file: str | None = None
    completed_epics: list[str] = field(default_factory=list)
    pending_prs: list[PendingPR] = field(default_factory=list)
    paused_context: PausedContext | None = None
    active_phase: Phase | None = None
    active_epic: str | None = None
    active_worktree: str | None = None

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
        active_phase = (
            Phase.from_value(data.get("active_phase"), default=None)
            if data.get("active_phase")
            else None
        )
        if parallel_mode and active_phase is None:
            active_phase = phase

        pending_prs = []
        for item in data.get("pending_prs", []) or []:
            if isinstance(item, dict):
                pending_prs.append(PendingPR.from_dict(item))

        paused_context = None
        if isinstance(data.get("paused_context"), dict):
            paused_context = PausedContext.from_dict(data["paused_context"])

        completed = [
            str(epic)
            for epic in data.get("completed_epics", []) or []
            if epic is not None
        ]

        return cls(
            mode=mode,
            phase=phase,
            current_epic=data.get("current_epic"),
            current_story=data.get("current_story"),
            current_story_file=data.get("current_story_file"),
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
            "current_story": self.current_story,
            "current_story_file": self.current_story_file,
            "completed_epics": list(self.completed_epics),
            "pending_prs": [pr.to_dict() for pr in self.pending_prs],
            "paused_context": self.paused_context.to_dict()
            if self.paused_context
            else None,
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
    def effective_epic(self) -> str | None:
        if self.is_parallel and self.active_epic:
            return self.active_epic
        return self.current_epic


@dataclass
class RuntimeConfig:
    epic_pattern: str = ""
    start_from: str = ""
    flow_mode: str = "auto"
    continue_run: bool = True
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
    codex_switch_mode: str = "auto"
    codex_switch_primary_threshold: int = 20
    codex_switch_secondary_threshold: int = 20
    cockpit_data_dir: str = ""
    accept_dirty_worktree: bool = False
    quota_retry_seconds: int = 1800


@dataclass(frozen=True)
class CockpitCodexQuota:
    hourly_percentage: int = 0
    weekly_percentage: int = 0
    hourly_window_minutes: int | None = None
    weekly_window_minutes: int | None = None
    hourly_window_present: bool | None = None
    weekly_window_present: bool | None = None


@dataclass(frozen=True)
class CockpitCodexTokens:
    id_token: str
    access_token: str
    refresh_token: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class CockpitCodexAccount:
    id: str
    email: str
    auth_mode: str = "oauth"
    openai_api_key: str | None = None
    api_base_url: str | None = None
    account_id: str | None = None
    organization_id: str | None = None
    plan_type: str | None = None
    quota: CockpitCodexQuota | None = None
    tokens: CockpitCodexTokens | None = None
    created_at: int = 0
    last_used: int = 0

    def is_api_key_auth(self) -> bool:
        return self.auth_mode == "apikey" or self.openai_api_key is not None

    def is_switchable(self) -> bool:
        if self.is_api_key_auth():
            return bool(self.openai_api_key and self.openai_api_key.strip())
        if not self.tokens:
            return False
        return bool(self.tokens.id_token.strip() and self.tokens.access_token.strip())


@dataclass(frozen=True)
class CockpitCodexStoreSnapshot:
    data_dir: Path
    index_path: Path
    accounts_dir: Path
    current_account_id: str | None
    index_payload: dict[str, Any] | None
    auth_payload: dict[str, Any] | None
    accounts: list[CockpitCodexAccount]


@dataclass(frozen=True)
class CockpitCodexSwitchSettings:
    mode: str = "auto"
    primary_threshold: int = 20
    secondary_threshold: int = 20


@dataclass(frozen=True)
class CockpitCodexSwitchCandidate:
    account: CockpitCodexAccount
    min_margin: int
    min_percentage: int
    average_percentage: float
