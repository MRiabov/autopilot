---
description: Run BMAD Autopilot - Autonomous Story Development (story-first state machine)
allowed-tools: Bash,Read,Write,Edit,Grep,Glob,TodoWrite
user-invocable: true
---

# BMAD Autopilot - Autonomous Story Flow

**CRITICAL: Do NOT run `.autopilot/bmad-autopilot.sh`. Execute the phase logic directly.**

You are an autonomous development orchestrator.

**Story / epic pattern:** `$ARGUMENTS` (if empty, select the next active story from `_bmad-output/implementation-artifacts/sprint-status.yaml`)
**Start-from selector:** `--from` on the launcher, or a matching lower-bound story/epic when executing the flow directly

## Step 1: Load State

Read `.autopilot/state.json`. If it does not exist, start fresh.

## Step 2: Execute Current Phase

### Story Flow
1. Read `_bmad-output/implementation-artifacts/sprint-status.yaml`.
2. Select the next story with `review` stories first, then implementation stories in file order, then `backlog`.
3. If a start-from selector is provided, ignore stories earlier than that story key or epic.
4. If only backlog remains, run `$bmad-create-story`.
5. Run `$bmad-dev-story` on the selected story.
6. Run `$integration-tests-workflow` for the selected story.
7. Run `$bmad-code-review` on the selected story.
8. If the review is valid, mark the story `done` in the story file and in `sprint-status.yaml`.
9. If no active stories remain, review completed epics, mark each epic `done`, run `$bmad-retrospective`, and record the retrospective status.
10. Return to story selection and continue.

### Legacy Flow
1. Resume open `feature/epic-*` PRs before starting new work.
2. Read `_bmad-output/implementation-artifacts/sprint-status.yaml`.
3. Skip completed epics, backlog epics, and epics already in the pending queue.
4. Filter by `$ARGUMENTS` if provided.
5. If a start-from selector is provided, ignore epics earlier than that epic.
6. Move to `CREATE_BRANCH`.

### CREATE_BRANCH
1. Checkout the base branch.
2. Create `feature/epic-{ID}` or switch to it if it already exists.
3. Push the branch.
4. Move to `DEVELOP_STORIES`.

### DEVELOP_STORIES
1. Read the selected story file from the sprint-status `story_location` directory.
2. Invoke `$bmad-dev-story`.
3. Let the skill implement the story and commit incrementally.
4. Move to `QA_AUTOMATION_TEST`.

### QA_AUTOMATION_TEST
1. Invoke `$integration-tests-workflow`.
2. Read `.autopilot/specs/integration-tests.md` before adding anything.
3. Let the skill add or update automated tests using the repository's existing framework.
4. The skill must run tests with `./scripts/run_integration_tests.sh`.
5. Fix failures until the tests pass.
6. Move to `CODE_REVIEW`.

### CODE_REVIEW
1. Invoke `$bmad-code-review`.
2. Fix issues found during review.
3. Run local checks.
4. In story flow, mark the story `done` and advance.
5. In legacy flow, push fixes and move to `CREATE_PR`.

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
All stories are done in story flow, or all epics are processed and the pending PR queue is empty in legacy flow.

## Step 3: Update State

Persist `.autopilot/state.json` after each phase transition.

## Rules

1. Do not ask the user questions except for the explicit dirty-worktree confirmation gate.
2. Commit often.
3. Continue on errors where possible.
4. Update state after each phase.

## Start Now

Read `.autopilot/state.json` and execute the current phase for: `$ARGUMENTS`
