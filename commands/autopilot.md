---
description: Run BMAD Autopilot - Autonomous Story Development
allowed-tools: Bash,Read,Write,Edit,Grep,Glob,TodoWrite
user-invocable: true
---

# BMAD Autopilot - Autonomous Story Flow

**CRITICAL: Do NOT run `.autopilot/bmad-autopilot.sh` from inside this command. Execute the phase logic directly.**

You are an autonomous development orchestrator.

**Story / epic pattern:** `$ARGUMENTS` (if empty, select the next active story from `_bmad-output/implementation-artifacts/sprint-status.yaml`)
**Start-from selector:** `--from` when using the launcher, or a matching lower-bound story/epic when executing the flow directly

## Step 1: Load State

Read `.autopilot/state.json`. If it does not exist, start fresh.

## Step 2: Execute the Current Phase

### Story Flow
1. Read `_bmad-output/implementation-artifacts/sprint-status.yaml`.
2. Select the next story in this order: `in-progress`, `review`, `ready-for-dev`, `backlog`.
3. If a start-from selector is provided, ignore stories earlier than that story key or epic.
4. If only backlog remains, run `$bmad-create-story`.
5. Run `$bmad-dev-story` on the selected story.
6. Run `$integration-tests-workflow` for the selected story.
7. Run `$bmad-code-review` on the selected story.
8. If the review is valid, mark the story `done` in the story file and in `sprint-status.yaml`.
9. Return to story selection and continue.

### Legacy Flow
1. Resume open `feature/epic-*` PRs before starting new work.
2. Read `_bmad-output/implementation-artifacts/sprint-status.yaml`.
3. Skip completed epics, backlog epics, and epics already in the pending PR queue.
4. Apply `$ARGUMENTS` filtering if provided.
5. If a start-from selector is provided, ignore epics earlier than that epic.
6. Select the next active epic and move to `CREATE_BRANCH`.

### CREATE_BRANCH
1. Checkout the base branch and create `feature/epic-{ID}`.
2. Push the branch.
3. Move to `DEVELOP_STORIES`.

### DEVELOP_STORIES
1. Read the selected story file from the sprint-status `story_location` directory.
2. Invoke `$bmad-dev-story`.
3. Let the skill implement the story and commit incrementally.
4. Move to `QA_AUTOMATION_TEST`.

### QA_AUTOMATION_TEST
1. Invoke `$integration-tests-workflow`.
2. Read `specs/integration-tests.md` before adding anything.
3. Let the skill add or update automated tests using the repository's existing test framework.
4. The skill must run tests with `./scripts/run_integration_tests.sh`.
5. Fix failures until the tests pass.
6. Move to `CODE_REVIEW`.

### CODE_REVIEW
1. Invoke `$bmad-code-review`.
2. Fix any issues found.
3. Run local checks.
4. In story flow, mark the story `done` and advance.
5. In legacy flow, push the fixes and move to `CREATE_PR`.

### CREATE_PR
1. Create the PR.
2. Add it to the pending list.
3. Move back to `FIND_EPIC`.

### WAIT_COPILOT / FIX_ISSUES
1. Wait for Copilot review activity.
2. If Copilot requests changes or leaves unresolved threads, fix them.
3. Reply to the review.
4. Resolve threads.
5. Return to waiting.

### MERGE_PR / RETROSPECTIVE
1. Merge approved PRs.
2. Sync the base branch.
3. Invoke `$bmad-retrospective` to generate the retrospective artifact.
4. Mark the epic complete.

### DONE
All stories are done in story flow, or all epics are processed and the pending PR queue is empty in legacy flow.

## Rules

1. Never ask the user questions during automation.
2. Commit often.
3. Continue on errors where possible and mark the work blocked if you cannot proceed.
4. Persist state after each phase transition.

## Start Now

Read state and execute the current phase for: `$ARGUMENTS`
