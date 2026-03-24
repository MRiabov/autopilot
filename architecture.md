# BMAD Autopilot Architecture

## Purpose

This document defines the technical architecture for BMAD Autopilot, the standalone unattended orchestration product that drives BMAD-style development work through Codex, git, QA, and review loops.

The product-level goals live in [prd.md](prd.md). This document records the implementation decisions that keep the runner consistent, recoverable, and difficult to stop accidentally.

## Architecture Summary

BMAD Autopilot is a local, file-backed state machine with three hard requirements:

1. Keep moving forward whenever the workspace and account pool still allow progress.
2. Validate every Codex response structurally before accepting it.
3. Keep runtime state, logs, and review artifacts tied to the active workspace.

The external launcher remains thin. The actual runtime is split into internal modules under `.autopilot/scripts/internal/` so that responsibilities are separated by concern instead of being packed into one oversized script.

## Runtime Topology

Current launcher chain:

1. [`.autopilot/scripts/bmad-autopilot.sh`](scripts/bmad-autopilot.sh) resolves the script directory and launches Python.
2. [`.autopilot/scripts/bmad-autopilot.py`](scripts/bmad-autopilot.py) is a compatibility wrapper.
3. [`.autopilot/scripts/bmad_autopilot_runner.py`](scripts/bmad_autopilot_runner.py) is the import-compatible wrapper used by older entrypoints.
4. [`.autopilot/scripts/internal/runner_core.py`](scripts/internal/runner_core.py) owns the orchestration runtime.

Supporting modules:

| Path | Responsibility |
| --- | --- |
| [`.autopilot/scripts/internal/models.py`](scripts/internal/models.py) | State models, enums, Pydantic contracts, and runtime dataclasses |
| [`.autopilot/scripts/internal/utils.py`](scripts/internal/utils.py) | Timestamp, serialization, and text IO helpers |
| [`.autopilot/scripts/internal/cockpit.py`](scripts/internal/cockpit.py) | Cockpit/Codex account store parsing and account switching |
| [`.autopilot/scripts/internal/status.py`](scripts/internal/status.py) | Last-run status summarization and log slicing helpers |
| [`.autopilot/scripts/internal/runner_story_phases.py`](scripts/internal/runner_story_phases.py) | Story-flow phase handlers, reroute behavior, and story-specific orchestration |
| [`.autopilot/scripts/internal/runner_legacy_phases.py`](scripts/internal/runner_legacy_phases.py) | Legacy epic/PR phase handlers, waiting loops, and backfill support |

Design target:

- Keep the wrapper scripts thin.
- Keep each implementation module focused on one concern.
- Prefer files that are easy to scan and review over one monolithic runner.

## Execution Model

The runtime is a deterministic loop:

1. Resolve the repository root and workspace root.
2. Confirm dirty worktree policy.
3. Load or initialize on-disk state.
4. Select the current workflow phase.
5. Route to the correct BMAD action.
6. Validate structured output.
7. Persist artifacts and update state.
8. Retry or reroute when the output or workspace is still recoverable.
9. Stop only when the run is truly unrecoverable.

The runtime is intentionally stateful. It is not a one-shot command wrapper.

## Entry Point Decisions

### Shell launcher

`bmad-autopilot.sh` is only a launcher. It should not own product logic.

### Python wrappers

`bmad-autopilot.py` and `bmad_autopilot_runner.py` remain for compatibility with older paths and existing commands. They should stay small and stable.

### Internal implementation

Implementation lives under `.autopilot/scripts/internal/`. New logic should go there instead of growing the launcher scripts.

## File and Module Boundaries

### Wrapper boundary

The top-level entry scripts do not own business logic. They import and delegate.

### Core boundary

The core runtime owns:

- CLI argument handling
- workspace detection
- state file loading/saving
- phase dispatch
- retry coordination
- session continuation
- account switching coordination

### Shared model boundary

Shared models belong in `internal/models.py` instead of ad hoc dicts. This includes:

- `Phase`
- `AutopilotState`
- `RuntimeConfig`
- `SprintStatus`
- `SprintStatusValue`
- `ValidationFailure`
- `CodexAttemptResult`
- `ReviewSourceSnapshot`
- account store models

### Account switching boundary

All Cockpit/Codex account-store logic belongs in `internal/cockpit.py`.

That module owns:

- store discovery
- account parsing
- quota metric extraction
- account selection
- auth file writes
- keychain writes on macOS
- index updates

### Utility boundary

Simple, generic helpers belong in `internal/utils.py`.

That module owns:

- `read_text`
- `write_text`
- `timestamp`
- `utc_now`
- `to_jsonable`

## State Model

The orchestration state is persisted in `.autopilot/state.json`.

The state must track:

- current phase
- current epic
- current story
- current story file
- completed epics
- pending PRs
- paused context
- active worktree
- active phase when parallel mode is enabled

Decision rule:

- State is append/rewrite local metadata, not a source of truth for business content.
- Business truth still comes from the sprint status file and the workspace files.

## Workspace Model

All runtime artifacts are workspace-scoped.

The product must not treat another workspace’s artifacts as current state.

Workspace-bound locations include:

- `.autopilot/state.json`
- `.autopilot/autopilot.log`
- `.autopilot/tmp/`
- `.autopilot` review artifacts
- story and sprint status files under the active workspace root

The active workspace root is resolved from the current worktree or the project root depending on the flow.

## Prompt and Output Contracts

BMAD Autopilot must not infer success from prose.

Every Codex-facing phase uses structured validation:

- YAML frontmatter only for accepted machine output
- required fields for the phase
- strict rejection of malformed or missing fields
- workspace scope fingerprints for review phases

The important contracts are:

- Dev output returns `workflow_status` plus the correct story or epic identifier.
- QA output returns `review_status`.
- Review output returns `review_status`, `review_scope_fingerprint`, and `reviewed_files`.

Decision rule:

- No structured contract, no success transition.
- A clean-sounding narrative is never enough.

## Retry and Resume Model

BMAD Autopilot retries the same task instead of restarting the workflow when validation fails and the task is still recoverable.

Key rules:

- Capture the resumable Codex thread ID when available.
- Reuse the same thread ID for retry attempts.
- Keep the original task context stable across retries.
- Limit retries per phase.
- Feed the previous validation failure back into the retry prompt.

Retry outcomes:

- Malformed dev/QA/review output -> retry or reroute back to development.
- `stories_blocked` -> immediate reroute back to development.
- Quota exhaustion -> switch account first, then back off only when no healthier account remains.

## Quota and Account Policy

Quota is treated as an availability signal, not as a hard stop on the first failure.

Account policy:

- Read account data from the local Cockpit store.
- Prefer a healthier account before launching a new Codex session.
- Switch at startup and before each Codex session when enabled.
- Preserve account-switch decisions in logs.

Terminal quota rule:

- Stop only when no usable account remains and the run cannot continue safely.

## Dirty Worktree Policy

Dirty worktrees are not an automatic stop.

The launcher behavior is:

- prompt for explicit confirmation by default
- accept `--accept-dirty-worktree` to bypass the prompt
- abort when confirmation is rejected or stdin is unavailable

Decision rule:

- The bypass flag is for automation and unattended runs.
- The confirmation prompt is for safety.

## Phase Routing Policy

BMAD Autopilot is a phase machine, not a linear script.

Routing rules:

- `FIND_EPIC` selects the next actionable story or epic.
- `CREATE_STORY` creates missing backlog stories.
- `DEVELOP_STORIES` writes or fixes implementation.
- `QA_AUTOMATION_TEST` runs integration coverage.
- `CODE_REVIEW` validates the current workspace snapshot.
- `CREATE_PR`, `WAIT_COPILOT`, `FIX_ISSUES`, and `MERGE_PR` exist for legacy epic/PR handling.

Recovery rules:

- QA failure reroutes to development.
- Invalid development output reroutes to development.
- Invalid review output reroutes to development.
- Transient `stories_blocked` reroutes immediately.
- The orchestrator should only set `BLOCKED` for genuinely unrecoverable conditions.

## Terminal State Policy

`BLOCKED` is a last resort.

Use it only when:

- no usable account remains
- the workspace cannot be continued safely
- the sprint/status files are structurally unusable
- a non-recoverable external tool failure prevents forward progress

Do not use it for normal dev/QA/review failures that can be retried.

## Observability Policy

Logs must explain:

- phase transitions
- validation failures
- retry reasons
- reroutes
- account switches
- workspace selection
- resume decisions

Artifacts must preserve enough detail to reconstruct why the run moved from one state to another.

## CLI and Config Policy

Configuration is layered:

1. command-line flags
2. environment variables
3. `.autopilot/config`
4. built-in defaults

Important runtime toggles include:

- continue vs fresh start
- dirty-worktree bypass
- quota retry interval
- account-switch thresholds
- flow mode
- verbose/debug output

## Test and Verification Policy

Autopilot behavior is verified with integration-style tests under:

- [`.autopilot/tests/integration/architecture_p1/`](tests/integration/architecture_p1/)

Rules:

- Use integration tests, not unit tests, for behavior changes.
- Assert on observable state, artifacts, and logs.
- Keep doc/spec entries aligned with the tests they describe.

## Refactor Policy

The codebase should keep moving toward smaller files.

Implementation target:

- wrappers stay tiny
- internal modules stay focused
- when a module grows too large, split by responsibility, not by arbitrary line count

Practical split order:

1. state and workspace helpers
2. Codex session/retry logic
3. story-flow phases
4. legacy-flow phases
5. CLI wiring

## Current Technical Decision Summary

BMAD Autopilot is intentionally:

- local first
- stateful
- retry first
- quota aware
- workspace scoped
- structurally validated
- recoverable after interruption

It should prefer writing code to stopping, but it must still fail closed when the workspace, account pool, or output contract is truly unrecoverable.
