# BMAD Autopilot Legacy Notes

This file is kept for compatibility with older Claude Code installs.
The active runtime is the Codex-backed launcher in `scripts/bmad-autopilot.py`.

## What the installer places

1. `.autopilot/bmad-autopilot.sh`
2. `.autopilot/scripts/bmad-autopilot.py`
3. `.autopilot/config.example`
4. Optional legacy command templates in `.claude/commands` and `.claude/skills`

## Current workflow

The active default flow is:

`FIND_EPIC -> CREATE_STORY -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> FIND_EPIC`

When code review succeeds, the runner writes `done` to the story file and to `sprint-status.yaml`, then moves on to the next story.

Set `AUTOPILOT_FLOW=legacy` only if you need the old epic/PR flow:

`CHECK_PENDING_PR -> FIND_EPIC -> CREATE_BRANCH -> DEVELOP_STORIES -> QA_AUTOMATION_TEST -> CODE_REVIEW -> CREATE_PR`

## Runtime notes

- Codex exec handles implementation, QA automation, code review, fix-up, and retrospective synthesis.
- `gh` is only required for the legacy flow.
- `install.sh` is the supported bootstrap path.
- See `README.md` for the user-facing workflow and `docs/ARCHITECTURE.md` for the detailed state machine.
