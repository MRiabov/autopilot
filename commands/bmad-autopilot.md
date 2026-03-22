---
description: Run BMAD Autopilot - Autonomous Development Flow (multi-epic state machine)
allowed-tools: Bash,Read,Write,Edit,Grep,Glob,TodoWrite
user-invocable: true
---

# BMAD Autopilot - Autonomous Development Flow

**CRITICAL: Do NOT run `.autopilot/bmad-autopilot.sh`. Execute the phase logic directly.**

You are an autonomous development orchestrator.

**Epic pattern:** $ARGUMENTS (if empty, find the next active epic from `_bmad-output/implementation-artifacts/sprint-status.yaml`)

## Step 1: Load State

Read `.autopilot/state.json`. If it does not exist, start fresh.

## Step 2: Execute Current Phase

### FIND_EPIC / CHECK_PENDING_PR
1. Resume open `feature/epic-*` PRs before starting new work.
2. Read `_bmad-output/implementation-artifacts/sprint-status.yaml`.
3. Skip completed epics, backlog epics, and epics already in the pending queue.
4. Filter by `$ARGUMENTS` if provided.
5. Move to `CREATE_BRANCH`.

### CREATE_BRANCH
1. Checkout the base branch.
2. Create `feature/epic-{ID}` or switch to it if it already exists.
3. Push the branch.
4. Move to `DEVELOP_STORIES`.

### DEVELOP_STORIES
1. Read the epic's story files from the sprint-status `story_location` directory.
2. Invoke `$bmad-dev-story`.
3. Let the skill implement each story and commit incrementally.
4. Move to `QA_AUTOMATION_TEST`.

### QA_AUTOMATION_TEST
1. Invoke `$integration-tests-workflow`.
2. Read `specs/integration-tests.md` before adding anything.
3. Let the skill add or update automated tests using the repository's existing framework.
4. The skill must run tests with `./scripts/run_integration_tests.sh`.
5. Fix failures until the tests pass.
6. Move to `CODE_REVIEW`.

### CODE_REVIEW
1. Invoke `$bmad-code-review`.
2. Fix issues found during review.
3. Run local checks.
4. Push fixes.
5. Move to `CREATE_PR`.

### CREATE_PR
1. Create the PR.
2. Add it to the pending list.
3. Move back to `FIND_EPIC`.

### WAIT_COPILOT / FIX_ISSUES
1. Wait for Copilot activity.
2. Fix unresolved review threads and CI issues.
3. Reply to the PR.
4. Resolve threads.
5. Return to waiting.

### MERGE_PR / RETROSPECTIVE
1. Merge approved PRs.
2. Sync the base branch.
3. Invoke `$bmad-retrospective` to generate the retrospective artifact.
4. Mark the epic complete.

### DONE
All epics processed and the pending PR queue is empty.

## Step 3: Update State

Persist `.autopilot/state.json` after each phase transition.

## Rules

1. Never ask the user questions.
2. Commit often.
3. Continue on errors where possible.
4. Update state after each phase.

## Start Now

Read `.autopilot/state.json` and execute the current phase for: $ARGUMENTS
