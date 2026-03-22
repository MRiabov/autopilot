# BMAD Autopilot

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](VERSION)

**Autonomous Development Orchestrator for Codex**

BMAD Autopilot is a Python-driven state machine that automates the development cycle from epic selection to PR merge. It uses Codex for implementation, QA automation, code review, fix-up, and retrospectives, while GitHub CLI handles PRs, checks, and review state.

## Features

- 🤖 Fully autonomous execution
- 🔄 Resumable state machine
- 📋 Multi-epic support with pattern filtering
- 🧪 Dedicated QA automation phase for integration/E2E tests
- 🔍 GitHub Copilot review handling and fix-up loops
- ✅ CI integration and background merge monitoring
- 🪞 Retrospective generation after merge
- 📝 Detailed logging in `.autopilot/autopilot.log`
- 🔀 Parallel mode with worktrees
- 🔒 Safe config parsing with whitelisted keys

## Prerequisites

Required tools:

- `python3` - runs the Python orchestration core
- `codex` - OpenAI Codex CLI
- `gh` - [GitHub CLI](https://cli.github.com/)
- `git` - Git version control

Optional compatibility:

- Legacy BMAD command templates in `.claude/` if you still use a command-based frontend

This checkout is self-contained. The launcher already lives under `.autopilot/`.

## Usage

```bash
# Process all active epics from _bmad-output/implementation-artifacts/sprint-status.yaml
./.autopilot/bmad-autopilot.sh

# Process only selected epics
./.autopilot/bmad-autopilot.sh "1 2 3"

# Resume after interruption
./.autopilot/bmad-autopilot.sh --continue

# Enable verbose logging
./.autopilot/bmad-autopilot.sh --verbose
```

## Workflow

The orchestrator runs these phases:

`CHECK_PENDING_PR -> FIND_EPIC -> CREATE_BRANCH -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> CREATE_PR`

Background work then monitors PRs:

`WAIT_COPILOT -> FIX_ISSUES -> WAIT_COPILOT -> MERGE_PR -> RETROSPECTIVE -> CHECK_PENDING_PR`

The runner stays alive until the pending PR queue is empty, even after the last new epic has been discovered.

### Phase Summary

| Phase | Description |
|-------|-------------|
| `CHECK_PENDING_PR` | Resume unfinished PRs before starting new work |
| `FIND_EPIC` | Find the next active epic from `_bmad-output/implementation-artifacts/sprint-status.yaml` |
| `CREATE_BRANCH` | Create `feature/epic-{ID}` |
| `DEVELOP_STORIES` | Run BMAD dev-story workflow via Codex |
| `QA_AUTOMATION_TEST` | Add or update automated API/E2E tests, following the integration-test workflow |
| `CODE_REVIEW` | Run BMAD code-review workflow via Codex |
| `CREATE_PR` | Create PR, add it to the pending list, continue to the next epic |
| `WAIT_COPILOT` | Wait for Copilot review and route to fixes if needed |
| `FIX_ISSUES` | Fix CI/review issues, reply, and resolve threads |
| `MERGE_PR` | Merge approved PRs and run post-merge checks |
| `RETROSPECTIVE` | Generate the epic retrospective artifact after merge |
| `DONE` | All epics processed |
| `BLOCKED` | Manual intervention required |

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
| `MAX_TURNS` | `80` | Legacy prompt budget kept for compatibility |
| `CHECK_INTERVAL` | `30` | Seconds between CI/Copilot checks |
| `MAX_CHECK_WAIT` | `60` | Max iterations waiting for CI checks |
| `MAX_COPILOT_WAIT` | `60` | Max iterations waiting for Copilot review |
| `AUTOPILOT_RUN_MOBILE_NATIVE` | `0` | Set to `1` to run Gradle builds |
| `AUTOPILOT_BASE_BRANCH` | auto | Override base branch detection |

### Parallel Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `PARALLEL_MODE` | `0` | `0` = sequential, `1+` = use git worktrees |
| `PARALLEL_CHECK_INTERVAL` | `60` | Seconds between pending PR checks |
| `MAX_PENDING_PRS` | `2` | Max concurrent PRs before the orchestrator pauses new epic work |

## Project Structure

```
.autopilot/
├── bmad-autopilot.sh    # Shell launcher
├── bmad-autopilot.py    # Python orchestration core
├── state.json           # Current state
├── autopilot.log        # Execution log
└── tmp/                 # Temporary files
    ├── develop-stories-output.txt
    ├── qa-automation-output.txt
    ├── code-review-output.txt
    ├── fix-issues-output.txt
    └── retrospective-output.txt
```

## BMAD Workflows Used

- `$bmad-dev-story` - Story development
- `$integration-tests-workflow` - QA automation tests
- `$bmad-code-review` - Code review
- `$bmad-retrospective` - Epic retrospective

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
- `gh` not found: install GitHub CLI and authenticate with `gh auth login`
- Git tree dirty: commit or stash before starting the runner
- PRs not merging: check `.autopilot/tmp/` for the latest `code-review-output.txt`, `fix-issues-output.txt`, or `retrospective-output.txt`

## Notes

- The shell wrapper exists only to keep the entrypoint simple.
- The Python runner is the source of truth.
- BMAD command templates remain optional compatibility files for command-based users.

Built for use with [BMAD Method](https://github.com/bmad-method) and Codex CLI.
