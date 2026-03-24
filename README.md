# BMAD Autopilot

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](VERSION)

**Autonomous Development Orchestrator for Codex**

BMAD Autopilot is a Python-driven state machine for unattended story implementation. In the default `story` flow it selects the next story from `_bmad-output/implementation-artifacts/sprint-status.yaml`, runs `bmad-dev-story`, runs QA automation, runs code review, marks the story `done`, and advances to the next story.

The legacy epic/PR flow still exists behind `AUTOPILOT_FLOW=legacy`, but it is not the default.

## Features

- Story-first autonomous execution
- Resumable state machine
- Start-from selector for skipping ahead to a later story or epic
- Multi-story filtering
- QA automation phase for integration/E2E tests
- Automatic `done` promotion in story status and sprint status after a valid review
- Detailed logging in `.autopilot/autopilot.log`
- Legacy PR handling when explicitly enabled
- Safe config parsing with whitelisted keys

## Prerequisites

Story flow:

- `python3`
- `codex`
- `git`

Legacy flow only:

- `gh` - [GitHub CLI](https://cli.github.com/)

Optional compatibility:

- Legacy BMAD command templates in `.claude/` if you still use a command-based frontend

This checkout is self-contained. The launcher already lives under `.autopilot/`.

## Usage

```bash
# Default story flow
./.autopilot/bmad-autopilot.sh

# Force a specific flow
AUTOPILOT_FLOW=story ./.autopilot/bmad-autopilot.sh
AUTOPILOT_FLOW=legacy ./.autopilot/bmad-autopilot.sh

# Filter stories or epics by pattern
./.autopilot/bmad-autopilot.sh "1-1 1-2 2-1"

# Start from a later story or epic
./.autopilot/bmad-autopilot.sh --from 3-1
./.autopilot/bmad-autopilot.sh --from 3.1

# Resume after interruption
./.autopilot/bmad-autopilot.sh

# Force a fresh start
./.autopilot/bmad-autopilot.sh --no-continue

# Enable verbose logging
./.autopilot/bmad-autopilot.sh --verbose
```

## Workflow

### Story Flow

`FIND_EPIC -> CREATE_STORY -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> FIND_EPIC`

When code review passes, BMAD Autopilot writes `done` to the story file and to `sprint-status.yaml`, then selects the next story.
If the workspace is dirty at launch, the runner now requires explicit `y`/`yes` confirmation before proceeding.
Use `--accept-dirty-worktree` if you want to skip that prompt and continue immediately.
Code review evaluates the current workspace snapshot, not only the committed branch diff, and persists review artifacts under that workspace root.
If a dev pass reports `stories_blocked`, the runner keeps the story `in-progress` and reroutes back to development immediately instead of treating the story as terminally blocked.
If Codex reports quota exhaustion, the runner switches to a healthier account when possible or waits and retries when no account can be rotated in.

### Legacy Flow

`CHECK_PENDING_PR -> FIND_EPIC -> CREATE_BRANCH -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> CREATE_PR`

The legacy flow keeps the previous PR, Copilot, and merge handling in place for repositories that still need it.

### QA Automation Contract

The QA phase is intentionally strict:

- Read `.codex/skills/integration-tests-workflow/SKILL.md` first.
- Read `specs/integration-tests.md` before adding anything.
- Use only `./scripts/run_integration_tests.sh` to execute integration tests.
- Keep tests at HTTP/system boundaries.
- Do not mock or patch project internals.
- Update `specs/integration-tests.md` if the catalog changes.

## Configuration

Copy `config.example` to `.autopilot/config` and edit as needed:

```bash
cp .autopilot/config.example .autopilot/config
```

Settings can be provided in this order:

1. Command line flags
2. Environment variables
3. `.autopilot/config`
4. Built-in defaults

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOPILOT_DEBUG` | `0` | Enable debug logging to `.autopilot/tmp/debug.log` |
| `AUTOPILOT_VERBOSE` | `0` | Show more progress details in the console |
| `AUTOPILOT_FLOW` | `auto` | `auto` selects story flow when sprint status has stories; `story` forces story flow; `legacy` keeps the old epic/PR flow |
| `MAX_TURNS` | `80` | Legacy prompt budget kept for compatibility |
| `CHECK_INTERVAL` | `30` | Seconds between CI/Copilot checks |
| `MAX_CHECK_WAIT` | `60` | Max iterations waiting for CI checks |
| `MAX_COPILOT_WAIT` | `60` | Max iterations waiting for Copilot review |
| `AUTOPILOT_RUN_MOBILE_NATIVE` | `0` | Set to `1` to run Gradle builds |
| `AUTOPILOT_BASE_BRANCH` | auto | Override base branch detection |
| `AUTOPILOT_QUOTA_RETRY_SECONDS` | `1800` | Wait before retrying Codex after quota exhaustion when no healthier account is available |

### Parallel Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `PARALLEL_MODE` | `0` | `0` = sequential, `1+` = use git worktrees |
| `PARALLEL_CHECK_INTERVAL` | `60` | Seconds between pending PR checks |
| `MAX_PENDING_PRS` | `2` | Max concurrent PRs before the legacy orchestrator pauses new epic work |

## Project Structure

```
.autopilot/
├── bmad-autopilot.sh    # Shell launcher
├── bmad-autopilot.py        # Thin CLI wrapper
├── bmad_autopilot_runner.py  # Python orchestration core
├── state.json           # Current state
├── autopilot.log        # Execution log
└── tmp/                 # Temporary files
    ├── create-story-output.txt
    ├── develop-story-output.txt
    ├── qa-story-output.txt
    ├── code-review-output.txt
    ├── fix-issues-output.txt
    └── retrospective-output.txt
```

## BMAD Workflows Used

- `$bmad-create-story` - Create the next story when only backlog items remain
- `$bmad-dev-story` - Story development
- `$integration-tests-workflow` - QA automation tests
- `$bmad-code-review` - Code review
- `$bmad-retrospective` - Epic retrospective for the legacy flow

## Troubleshooting

### View Logs

```bash
tail -f .autopilot/autopilot.log
```

### Check State

```bash
python3 -m json.tool .autopilot/state.json
```

### Common Issues

- `codex` not found: install the Codex CLI and ensure it is on `PATH`
- `gh` not found: only required for `AUTOPILOT_FLOW=legacy`
- Git tree dirty: the launcher prompts for explicit confirmation before continuing
- Continuation is on by default; use `--no-continue` only when you want to force a fresh state
- Story never advances to `done`: check `.autopilot/tmp/code-review-output.txt` and the matching story file/sprint status entry
- PRs not merging: check `.autopilot/tmp/` for the latest `code-review-output.txt`, `fix-issues-output.txt`, or `retrospective-output.txt`

## Notes

- The shell wrapper exists only to keep the entrypoint simple.
- The Python runner is the source of truth.
- BMAD command templates remain optional compatibility files for command-based users.

Built for use with [BMAD Method](https://github.com/bmad-method) and Codex CLI.
