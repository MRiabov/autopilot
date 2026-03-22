# BMAD Autopilot Architecture

## Overview

BMAD Autopilot is a state-machine orchestrator that automates epic processing from discovery to merge. The launcher shell script is only a wrapper. The Python runner owns the actual orchestration, file/state management, and Codex prompt execution.

## Core Components

### 1. Python State Machine

The runner advances through a deterministic set of phases:

```text
CHECK_PENDING_PR -> FIND_EPIC -> CREATE_BRANCH -> DEVELOP_STORIES
-> QA_AUTOMATION_TEST -> CODE_REVIEW -> CREATE_PR
```

Background PR handling then continues:

```text
WAIT_COPILOT -> FIX_ISSUES -> WAIT_COPILOT -> MERGE_PR
-> RETROSPECTIVE -> CHECK_PENDING_PR
```

If no new epics remain but the pending PR queue is still non-empty, the runner stays in `CHECK_PENDING_PR` mode until the queue drains.

The key rule is the same throughout the workflow:

- Git state transitions are deterministic.
- LLM work happens only inside phase-specific prompts.
- The shell wrapper never contains the real workflow logic.

### 2. State Persistence

State lives in `.autopilot/state.json` and includes the current phase, current epic, completed epic list, pending PR queue, and paused context for fix-up work.

This allows the runner to:

- Resume after interruption
- Continue in parallel mode with worktrees
- Restore the active epic after fix-up work pauses a branch

### 3. Codex Integration

The runner uses Codex exec for all fuzzy or judgment-heavy steps:

- `DEVELOP_STORIES` - invoke `$bmad-dev-story`
- `QA_AUTOMATION_TEST` - invoke `$integration-tests-workflow`
- `CODE_REVIEW` - invoke `$bmad-code-review`
- `FIX_ISSUES` - fix CI/review failures and reply to Copilot
- `RETROSPECTIVE` - invoke `$bmad-retrospective`

Each Codex invocation receives a skill token and writes a traceable output file under `.autopilot/tmp/`.

### 4. GitHub Integration

The runner uses `gh` for:

- Branch and PR creation
- Review polling
- CI status checks
- Copilot review detection
- PR merge and cleanup

The auto-approve workflow remains a separate GitHub Actions job that approves PRs only when CI passes, Copilot has reviewed, and all review threads are resolved.

## Phase Details

### CHECK_PENDING_PR

Before starting new work, the runner resumes any open `feature/epic-*` PR or in-flight feature branch.

### FIND_EPIC

The runner reads `_bmad-output/implementation-artifacts/sprint-status.yaml`, filters completed or backlog epics and epics already waiting on review, and selects the next active epic matching any user pattern. If no new epic is available but pending PRs still exist, it keeps monitoring instead of exiting.

### CREATE_BRANCH

Creates `feature/epic-{ID}`, pushes it, and prepares the branch for implementation.

### DEVELOP_STORIES

Runs the BMAD `dev-story` skill through Codex.

### QA_AUTOMATION_TEST

Runs the integration-test workflow through Codex.

### CODE_REVIEW

Runs the BMAD code-review skill through Codex, then runs local checks and pushes the review fixes.

### CREATE_PR

Creates or resumes the PR, records it in the pending queue, and immediately moves on to the next epic.

### WAIT_COPILOT

Polls the PR until Copilot posts review activity. If Copilot requests changes or leaves unresolved threads, the runner routes to `FIX_ISSUES`.

### FIX_ISSUES

The runner gathers unresolved review thread content, CI failures, and Copilot feedback, then asks Codex to fix only those issues, post a reply, and resolve threads.

### MERGE_PR

Once approved, the runner merges the PR, syncs the base branch, runs post-merge checks, and then generates the retrospective artifact.

### RETROSPECTIVE

The retrospective is generated immediately after merge. The Codex skill writes a markdown artifact in `_bmad-output/implementation-artifacts/`. The launcher reads sprint status, but does not mutate it.

## Data Flow

```text
_bmad-output/implementation-artifacts/sprint-status.yaml + story markdown -> Python state machine -> Codex prompts -> git/gh operations
     -> PRs and artifacts -> retrospective -> completed_epics
```

## Security Considerations

1. No secrets are stored in state.
2. GitHub auth is delegated to the existing `gh` CLI session.
3. Codex is invoked in non-interactive mode from the project root.
4. The launcher shell script contains no workflow logic.
5. Config parsing is whitelist-based.
6. Base branch detection falls back safely to `main`.
