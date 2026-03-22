---
name: bmad-autopilot-skill
description: Autonomous development orchestrator for sprint queue processing. Use when the user wants to run the autopilot, process active epics automatically, or automate the BMAD development workflow.
allowed-tools: Bash, Read
---

# BMAD Autopilot

This skill runs the launcher in `./.autopilot/bmad-autopilot.sh`, which delegates to the Python orchestration core.

## When to Use

Activate this skill when the user:

- Wants to process active epics automatically
- Wants to automate PR creation and review cycles
- Mentions the BMAD development workflow
- Wants Codex-driven implementation, QA automation, review, and retrospective generation

## Workflow States

The orchestrator runs through these phases:

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

- Story implementation
- Automated test generation
- Code review and fix-up
- Copilot issue repair
- Retrospective synthesis

## Auto-Approve Integration

The `auto-approve.yml` workflow still gates PR approval on:

1. Copilot review exists
2. All review threads are resolved
3. All CI checks pass
4. Enough time has elapsed since the last push

## Logs and State

- Log: `.autopilot/autopilot.log`
- State: `.autopilot/state.json`
- Debug: `.autopilot/tmp/debug.log`

## Prerequisites

The launcher requires: `python3`, `codex`, `gh`, `git`
