---
name: bmad-autopilot-skill
description: Autonomous development orchestrator for sprint queue processing. Use when the user wants to run the autopilot, process active stories automatically, or automate the BMAD development workflow.
allowed-tools: Bash, Read
---

# BMAD Autopilot

This skill runs the launcher in `./.autopilot/bmad-autopilot.sh`, which delegates to the Python orchestration core.

## When to Use

Activate this skill when the user:

- Wants to process active stories automatically
- Wants to automate story creation, implementation, review, and status promotion
- Mentions the BMAD development workflow
- Wants Codex-driven implementation, QA automation, review, and retrospective generation

## Workflow States

The orchestrator defaults to the story-first flow:

1. `FIND_EPIC`
2. `CREATE_STORY`
3. `DEVELOP_STORIES`
4. `QA_AUTOMATION_TEST`
5. `CODE_REVIEW`
6. `EPIC_REVIEW`
7. Loop until every story is `done` and each completed epic has been finalized with a retrospective

Legacy epic/PR flow remains available when `AUTOPILOT_FLOW=legacy`:

1. `CHECK_PENDING_PR`
2. `FIND_EPIC`
3. `CREATE_BRANCH`
4. `DEVELOP_STORIES`
5. `QA_AUTOMATION_TEST`
6. `CODE_REVIEW`
7. `CREATE_PR`
8. Background monitoring of pending PRs
9. `WAIT_COPILOT`
10. `FIX_ISSUES`
11. `MERGE_PR`
12. `RETROSPECTIVE`
13. Loop until all active epics are `DONE` and the pending PR queue is empty

## What the Runner Uses Codex For

- Story creation
- Story implementation
- Automated test generation
- Code review and fix-up
- Copilot issue repair in legacy flow
- Retrospective synthesis

## Logs and State

- Log: `.autopilot/autopilot.log`
- State: `.autopilot/state.json`
- Debug: `.autopilot/tmp/debug.log`

## Prerequisites

Story flow requires: `python3`, `codex`, `git`

Legacy flow also requires: `gh`
