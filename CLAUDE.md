# BMAD Autopilot Legacy Notes

This file is kept for compatibility with older Claude Code installs.
The active runtime is the Codex-backed launcher in `scripts/bmad-autopilot.py`.

## What the installer places

1. `.autopilot/bmad-autopilot.sh`
2. `.autopilot/bmad-autopilot.py`
3. `.autopilot/config.example`
4. Optional legacy command templates in `.claude/commands` and `.claude/skills`

## Current workflow

The active flow is:

`CHECK_PENDING_PR -> FIND_EPIC -> CREATE_BRANCH -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> CREATE_PR`

Pending PRs are monitored in the background until the queue is empty.
After a merge, the runner generates a retrospective artifact and returns to pending-PR checks.

## Runtime notes

- Codex exec handles implementation, QA automation, code review, fix-up, and retrospective synthesis.
- `gh` handles PRs, review polling, and merges.
- `install.sh` is the supported bootstrap path.
- See `README.md` for the user-facing workflow and `docs/ARCHITECTURE.md` for the detailed state machine.
