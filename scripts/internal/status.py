from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .models import AutopilotState
from .utils import read_text

LOG_ENTRY_RE = re.compile(r"^\[(?P<timestamp>[^\]]+)\]\s*(?P<message>.*)$")
RUN_START_RE = re.compile(
    r"BMAD Autopilot (?P<action>starting|resuming) (?P<flow>story|legacy) flow"
)
CURRENT_PHASE_RE = re.compile(r"Current phase:\s*(?P<phase>[A-Z_]+)")
PHASE_RE = re.compile(r"PHASE:\s*(?P<phase>[A-Z_]+)")
FOUND_STORY_RE = re.compile(
    r"✅ Found story:\s*(?P<story>[^\s]+)\s*\[(?P<status>[^\]]+)\]"
)
STORY_KEY_RE = re.compile(r"Story key:\s*(?P<story>[^\s]+)")
STORY_CONTEXT_RE = re.compile(r"📄 Story context:\s*(?P<path>.+)$")
CODE_REVIEW_WORKFLOW_RE = re.compile(
    r"Running BMAD code-review workflow for story (?P<story>[^\s]+)"
)
STORY_ALREADY_DONE_RE = re.compile(
    r"⏯️ Story (?P<story>[^\s]+) is already done; selecting the next story"
)
STORY_DONE_RE = re.compile(r"📝 Updated story (?P<story>[^\s]+) status to done")
REROUTE_RE = re.compile(r"↩️ Rerouting to (?P<target>.+)$")
VALIDATION_ERROR_RE = re.compile(r"Validation error:\s*(?P<detail>.+)$")


@dataclass
class LogEntry:
    timestamp: str
    lines: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class RunEvent:
    timestamp: str
    kind: str
    message: str
    story: str | None = None
    phase: str | None = None


@dataclass(frozen=True)
class RunSummary:
    root: Path
    log_path: Path
    state_path: Path
    run_action: str | None
    run_flow: str | None
    run_start_timestamp: str | None
    events: list[RunEvent]
    completed_stories: list[str]
    reviewed_stories: list[str]
    state: AutopilotState | None


def detect_repo_root(start: Path | None = None) -> Path:
    cwd = (start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return cwd

    resolved = result.stdout.strip()
    return Path(resolved).resolve() if resolved else cwd


def parse_log_entries(log_text: str) -> list[LogEntry]:
    entries: list[LogEntry] = []
    current: LogEntry | None = None

    for raw_line in log_text.splitlines():
        match = LOG_ENTRY_RE.match(raw_line)
        if match:
            if current is not None:
                entries.append(current)
            current = LogEntry(
                timestamp=match.group("timestamp"), lines=[match.group("message")]
            )
            continue

        if current is not None:
            current.lines.append(raw_line)

    if current is not None:
        entries.append(current)

    return entries


def load_state(state_path: Path) -> AutopilotState | None:
    if not state_path.exists():
        return None

    try:
        raw = json.loads(read_text(state_path, "").strip() or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(raw, dict):
        return None

    try:
        return AutopilotState.from_dict(raw, raw.get("mode") == "parallel")
    except Exception:
        return None


def _clean_story_path(value: str) -> str:
    try:
        return Path(value.strip()).stem
    except Exception:
        return value.strip()


def _parse_structured_message(message: str) -> dict[str, str] | None:
    if "event=" not in message:
        return None

    try:
        tokens = shlex.split(message)
    except ValueError:
        return None

    fields: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        fields[key] = value

    return fields if fields.get("event") else None


def _render_structured_fields(fields: dict[str, str]) -> str:
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        rendered: object = int(text) if text.isdigit() else text
        parts.append(f"{key}={json.dumps(rendered, ensure_ascii=False)}")
    return " ".join(parts)


def _slice_last_run(entries: list[LogEntry]) -> tuple[list[LogEntry], LogEntry | None]:
    if not entries:
        return [], None

    start_index = 0
    run_start: LogEntry | None = None
    for index, entry in enumerate(entries):
        if RUN_START_RE.search(entry.message):
            start_index = index
            run_start = entry

    return entries[start_index:], run_start


def summarize_run(root: Path) -> RunSummary:
    autopilot_dir = root / ".autopilot"
    log_path = autopilot_dir / "autopilot.log"
    state_path = autopilot_dir / "state.json"

    entries = parse_log_entries(read_text(log_path, ""))
    run_entries, run_start = _slice_last_run(entries)
    state = load_state(state_path)

    events: list[RunEvent] = []
    completed_stories: list[str] = []
    reviewed_stories: list[str] = []
    seen_completed: set[str] = set()
    seen_reviewed: set[str] = set()
    active_story: str | None = None
    last_event_key: tuple[str, str | None, str | None, str] | None = None

    def emit(
        timestamp: str,
        kind: str,
        message: str,
        *,
        story: str | None = None,
        phase: str | None = None,
    ) -> None:
        nonlocal last_event_key
        key = (kind, story, phase, message)
        if key == last_event_key:
            return
        events.append(
            RunEvent(
                timestamp=timestamp,
                kind=kind,
                message=message,
                story=story,
                phase=phase,
            )
        )
        last_event_key = key

    for entry in run_entries:
        message = entry.message

        structured = _parse_structured_message(message)
        if structured is not None:
            event_type = structured.get("event", "")
            structured_message = _render_structured_fields(structured)
            emit(
                entry.timestamp,
                "codex_event",
                structured_message,
                story=active_story,
                phase=structured.get("phase"),
            )
            if (
                event_type == "item.completed"
                and structured.get("item_type") == "agent_message"
            ):
                content = structured.get("content")
                if content and "workflow_status:" in content:
                    if "story_key:" in content:
                        match = STORY_KEY_RE.search(content)
                        if match:
                            active_story = match.group("story")
                    if "review_status:" in content and active_story:
                        if active_story not in seen_reviewed:
                            reviewed_stories.append(active_story)
                            seen_reviewed.add(active_story)
            continue

        if match := RUN_START_RE.search(message):
            emit(
                entry.timestamp,
                "run_start",
                f"{match.group('action')} {match.group('flow')} flow",
            )

        if match := CURRENT_PHASE_RE.search(message):
            emit(
                entry.timestamp,
                "phase",
                f"current phase {match.group('phase')}",
                phase=match.group("phase"),
            )

        if match := PHASE_RE.search(message):
            emit(
                entry.timestamp,
                "phase_action",
                f"step {match.group('phase')}",
                phase=match.group("phase"),
            )

        if match := FOUND_STORY_RE.search(message):
            active_story = match.group("story")
            status = match.group("status").strip().lower()
            if status == "review" and active_story not in seen_reviewed:
                reviewed_stories.append(active_story)
                seen_reviewed.add(active_story)
            emit(
                entry.timestamp,
                "story_selected",
                f"found story {active_story} [{status}]",
                story=active_story,
            )

        if match := STORY_KEY_RE.search(message):
            active_story = match.group("story")

        if match := STORY_CONTEXT_RE.search(message):
            active_story = _clean_story_path(match.group("path"))

        if match := CODE_REVIEW_WORKFLOW_RE.search(message):
            active_story = match.group("story")
            if active_story not in seen_reviewed:
                reviewed_stories.append(active_story)
                seen_reviewed.add(active_story)
            emit(
                entry.timestamp,
                "code_review",
                f"code review started for {active_story}",
                story=active_story,
            )

        if match := STORY_ALREADY_DONE_RE.search(message):
            emit(
                entry.timestamp,
                "story_skipped",
                f"story already done: {match.group('story')}",
                story=match.group("story"),
            )

        if match := STORY_DONE_RE.search(message):
            story = match.group("story")
            if story not in seen_completed:
                completed_stories.append(story)
                seen_completed.add(story)
            emit(
                entry.timestamp,
                "story_completed",
                f"story marked done: {story}",
                story=story,
            )

        if "✅ Code review passed; story marked done" in message:
            story = active_story
            if story and story not in seen_completed:
                completed_stories.append(story)
                seen_completed.add(story)
            emit(
                entry.timestamp,
                "story_completed",
                f"code review passed; story marked done{f': {story}' if story else ''}",
                story=story,
            )

        if match := REROUTE_RE.search(message):
            emit(
                entry.timestamp,
                "reroute",
                f"rerouted to {match.group('target').strip()}",
                story=active_story,
            )

        if match := VALIDATION_ERROR_RE.search(message):
            emit(
                entry.timestamp,
                "validation_error",
                f"validation error: {match.group('detail').strip()}",
                story=active_story,
            )

        if (
            "❌ Codex reported" in message
            or "❌ Codex did not produce" in message
            or "Aborted by user." in message
        ):
            emit(
                entry.timestamp,
                "blocked",
                message.strip().splitlines()[0],
                story=active_story,
            )

    run_action = None
    run_flow = None
    run_start_timestamp = None
    if run_start is not None:
        run_start_timestamp = run_start.timestamp
        match = RUN_START_RE.search(run_start.message)
        if match:
            run_action = match.group("action")
            run_flow = match.group("flow")

    return RunSummary(
        root=root,
        log_path=log_path,
        state_path=state_path,
        run_action=run_action,
        run_flow=run_flow,
        run_start_timestamp=run_start_timestamp,
        events=events,
        completed_stories=completed_stories,
        reviewed_stories=reviewed_stories,
        state=state,
    )


def _format_state_lines(summary: RunSummary) -> list[str]:
    lines: list[str] = []
    state = summary.state
    if state is None:
        lines.append("State: unavailable")
        return lines

    lines.append("State:")
    lines.append(f"- phase: {state.phase.value}")
    lines.append(f"- epic: {state.current_epic or '(none)'}")
    lines.append(f"- story: {state.current_story or '(none)'}")
    if state.current_story_file:
        lines.append(f"- story file: {state.current_story_file}")
    if state.completed_epics:
        lines.append(f"- completed epics: {', '.join(state.completed_epics)}")
    else:
        lines.append("- completed epics: (none)")
    if state.pending_prs:
        pending = ", ".join(
            f"{pr.epic}#{pr.pr_number}:{pr.status}" for pr in state.pending_prs
        )
        lines.append(f"- pending PRs: {pending}")
    else:
        lines.append("- pending PRs: (none)")
    return lines


def render_summary(summary: RunSummary) -> str:
    lines: list[str] = []

    if summary.run_start_timestamp:
        action = summary.run_action or "started"
        flow = summary.run_flow or "story"
        lines.append(f"Last run: {summary.run_start_timestamp} ({action} {flow} flow)")
    else:
        lines.append("Last run: unavailable")

    lines.extend(_format_state_lines(summary))
    lines.append("")

    lines.append("Completed stories:")
    if summary.completed_stories:
        lines.extend(f"- {story}" for story in summary.completed_stories)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Reviewed stories:")
    if summary.reviewed_stories:
        lines.extend(f"- {story}" for story in summary.reviewed_stories)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Major events:")
    if summary.events:
        for event in summary.events:
            details = event.message
            if event.story and event.story not in details:
                details = f"{details} ({event.story})"
            if event.phase and event.phase not in details and event.kind == "phase":
                details = f"{details}"
            lines.append(f"- {event.timestamp} {details}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append(f"Log: {summary.log_path}")
    lines.append(f"State: {summary.state_path}")
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize the last BMAD Autopilot run"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root to inspect (defaults to the current git root or cwd)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = (args.root or detect_repo_root()).resolve()
    summary = summarize_run(root)
    print(render_summary(summary))
    return 0
