# Configuration Guide

## Environment Variables

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOPILOT_DEBUG` | `0` | Enable debug logging to `.autopilot/tmp/debug.log` |
| `AUTOPILOT_VERBOSE` | `0` | Show more progress details in the console |
| `MAX_TURNS` | `80` | Legacy prompt budget kept for compatibility |
| `CHECK_INTERVAL` | `30` | Seconds between CI/Copilot checks |
| `MAX_CHECK_WAIT` | `60` | Maximum iterations waiting for CI checks |
| `MAX_COPILOT_WAIT` | `60` | Maximum iterations waiting for Copilot review |
| `AUTOPILOT_RUN_MOBILE_NATIVE` | `0` | Set to `1` to run Gradle builds |
| `AUTOPILOT_BASE_BRANCH` | auto | Override base branch detection |

### Parallel Mode Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PARALLEL_MODE` | `0` | Enable worktree-based parallel epic handling |
| `PARALLEL_CHECK_INTERVAL` | `60` | Seconds between pending PR checks |
| `MAX_PENDING_PRS` | `2` | Maximum concurrent PRs waiting for review |

### Example

```bash
MAX_TURNS=100 CHECK_INTERVAL=60 ./.autopilot/bmad-autopilot.sh
./.autopilot/bmad-autopilot.sh --debug
PARALLEL_MODE=1 ./.autopilot/bmad-autopilot.sh
```

## Configuration File

Settings can be configured in `.autopilot/config` using `key=value` format:

```bash
cp .autopilot/config.example .autopilot/config
```

### Config Priority

Later sources override earlier ones:

1. Built-in defaults
2. `.autopilot/config`
3. Environment variables
4. Command line flags

### Security

The config parser uses a whitelist. Unknown keys are ignored and logged.

Allowed keys:

```text
AUTOPILOT_DEBUG, AUTOPILOT_VERBOSE, MAX_TURNS, CHECK_INTERVAL,
MAX_CHECK_WAIT, MAX_COPILOT_WAIT, AUTOPILOT_RUN_MOBILE_NATIVE,
PARALLEL_MODE, PARALLEL_CHECK_INTERVAL, MAX_PENDING_PRS,
AUTOPILOT_BASE_BRANCH
```

## Base Branch Detection

The runner determines the default branch in this order:

1. `AUTOPILOT_BASE_BRANCH`
2. `origin/HEAD`
3. `main`
4. `master`

Override with:

```bash
AUTOPILOT_BASE_BRANCH=develop ./.autopilot/bmad-autopilot.sh
```

## Sprint Status Queue

The runner reads the sprint queue from:

- `_bmad-output/implementation-artifacts/sprint-status.yaml`

Active epic IDs come from `development_status` entries such as `epic-1`, `epic-2`, etc. The corresponding story files are read from the `story_location` directory declared in the same YAML file.

## Local Checks Configuration

The Python runner auto-detects common project types:

- Rust: `cargo fmt --check`, `cargo clippy`, `cargo test`
- Frontend: `pnpm run check`, `pnpm run typecheck`, `pnpm -r run test`
- Mobile native: `./gradlew build` when explicitly enabled

## BMAD Workflow Customization

The runner sends Codex prompts for these workflows:

- `dev-story`
- `qa-automate`
- `code-review`
- `retrospective`

The prompts live inside `bmad-autopilot.py` and `scripts/bmad-autopilot.py`. Edit them there if you need to change the automated behavior.

## Logging

- `.autopilot/autopilot.log` - main execution log
- `.autopilot/tmp/debug.log` - debug log when `--debug` is enabled
- `.autopilot/tmp/*.txt` - phase-specific Codex output captures

## State Management

The state file is `.autopilot/state.json`. It stores:

- current phase
- current epic
- completed epics
- pending PR queue
- paused context for fix-up work

You can reset the runner by deleting `.autopilot/state.json`.

## GitHub Integration

Branch naming:

- `feature/epic-{ID}`

PR labels:

- `epic`
- `automated`
- `epic-{ID}`

## Notes

- The Python runner is the source of truth.
- The shell wrapper is only a launcher.
- The QA phase is intentionally strict about the integration-test workflow and the `specs/integration-tests.md` catalog.
