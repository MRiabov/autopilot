# BMAD Autopilot Architecture

## Overview

BMAD Autopilot is a state-machine orchestrator that now defaults to a story-first flow. The shell wrapper is only a launcher. The Python runner owns orchestration, state persistence, Codex prompt execution, and file updates.

The story flow is the default when sprint status contains story rows:

```text
FIND_EPIC -> CREATE_STORY -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> EPIC_REVIEW -> FIND_EPIC
```

When code review succeeds, the runner writes `done` back to the story file and to `sprint-status.yaml`, then selects the next story. When no active stories remain, the story flow finalizes each completed epic, runs `$bmad-retrospective`, and marks the epic retrospective status in `sprint-status.yaml`.

The legacy epic/PR flow still exists behind `AUTOPILOT_FLOW=legacy`:

```text
CHECK_PENDING_PR -> FIND_EPIC -> CREATE_BRANCH -> DEVELOP_STORIES
-> QA_AUTOMATION_TEST -> CODE_REVIEW -> CREATE_PR
```

## Core Components

### 1. Python State Machine

The runner selects story work from `_bmad-output/implementation-artifacts/sprint-status.yaml` in file order. In the story flow it prioritizes story statuses in this order:

1. `in-progress`
2. `review`
3. `ready-for-dev`
4. `backlog`

The legacy flow retains epic selection, branch creation, PR handling, Copilot review loops, and merge monitoring.

### 2. State Persistence

State lives in `.autopilot/state.json` and includes the current phase plus the active epic/story context. The story flow stores the current story key so it can resume after interruption.

This allows the runner to:

- Resume after interruption
- Continue in parallel mode with worktrees in legacy mode
- Restore the active story after a pause

### 3. Codex Integration

The runner uses Codex exec for all fuzzy or judgment-heavy steps:

- `CREATE_STORY` - invoke `$bmad-create-story`
- `DEVELOP_STORIES` - invoke `$bmad-dev-story`
- `QA_AUTOMATION_TEST` - invoke `$integration-tests-workflow`
- `CODE_REVIEW` - invoke `$bmad-code-review`
- `FIX_ISSUES` - legacy PR fix-up loop
- `RETROSPECTIVE` - invoke `$bmad-retrospective`

Each Codex invocation writes a traceable output file under `.autopilot/tmp/`.

### 4. GitHub Integration

`gh` is only required for the legacy epic/PR flow. The story flow does not need GitHub CLI access unless you explicitly opt back into the legacy flow.

## Phase Details

### FIND_EPIC

In story flow, this phase means "pick the next story". In legacy flow, it means "pick the next epic".

### CREATE_STORY

Runs the BMAD `create-story` workflow for the next backlog story when no ready or review story is available.

### DEVELOP_STORIES

Runs the BMAD `dev-story` workflow on the selected story file.

### QA_AUTOMATION_TEST

Runs the integration-test workflow for the selected story.

### CODE_REVIEW

Runs the BMAD code-review workflow. In story flow, a successful review marks the story `done` in both the story file and sprint status.

### EPIC_REVIEW

Runs the BMAD retrospective workflow for a completed epic after all stories are done. The runner marks the epic `done`, writes the retrospective artifact, and records the epic retrospective status in sprint status.

### Legacy PR Phases

`CHECK_PENDING_PR`, `CREATE_BRANCH`, `CREATE_PR`, `WAIT_COPILOT`, `FIX_ISSUES`, `MERGE_PR`, and `RETROSPECTIVE` are preserved for the legacy flow.

## Data Flow

```text
_bmad-output/implementation-artifacts/sprint-status.yaml + story markdown -> Python state machine -> Codex prompts -> file edits
```

## Security Considerations

1. No secrets are stored in state.
2. Codex is invoked in non-interactive mode from the project root.
3. The launcher shell script contains no workflow logic.
4. Config parsing is whitelist-based.
5. Base branch detection falls back safely to `main`.
