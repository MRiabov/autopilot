# Troubleshooting Guide

## Common Issues

### 1. Required command not found

If the launcher says a command is missing, install the missing prerequisite and retry.

Typical requirements:

- `python3`
- `codex`
- `gh`
- `git`

### 2. Git working tree not clean

The runner warns before it starts if the repo has uncommitted changes.

Fix:

```bash
git add -A && git commit -m "wip"
# or
git stash
```

### 3. Autopilot gets stuck waiting for Copilot

Symptoms:

- The log shows repeated `waiting for Copilot` messages.
- No Copilot review appears on the PR.

Check what reviews and comments exist:

```bash
gh pr view --json comments,reviews
```

Possible causes:

1. Copilot is not enabled for the repository.
2. The PR review author login does not contain `copilot`.

### 4. Copilot reviewed but the runner does not see it

Inspect the latest Copilot payload:

```bash
cat .autopilot/tmp/copilot_latest.json
```

If the author login or review state differs from the repository's current Copilot behavior, update the review-detection logic in the Python runner.

### 5. No active epics too early

Symptoms:

- The runner exits with `No more active epics in sprint-status.yaml and no pending PRs - ALL DONE`
- Active epics still exist in `_bmad-output/implementation-artifacts/sprint-status.yaml`

Check sprint queue parsing:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml
data = yaml.safe_load(Path('_bmad-output/implementation-artifacts/sprint-status.yaml').read_text())
print([key for key, value in data['development_status'].items() if key.startswith('epic-') and value not in {'backlog', 'done'}])
PY
```

If the state file is stale, reset it:

```bash
rm .autopilot/state.json
./.autopilot/bmad-autopilot.sh
```

### 6. State corruption

If `.autopilot/state.json` becomes invalid or the phase machine looks wrong:

```bash
rm .autopilot/state.json
./.autopilot/bmad-autopilot.sh
```

### 7. Multiple open PRs accumulated

The runner is designed to resume open `feature/epic-*` PRs before starting new epics. If you interrupt it repeatedly, re-run the launcher and it should pick up the first open PR automatically.

### 8. CI checks never pass

Check the latest CI and review output:

```bash
gh pr checks
cat .autopilot/tmp/failed-checks.json
```

If the runner has already merged a PR but the repo looks stale locally, re-run the launcher so it can sync the base branch.

### 9. Codex stops before finishing a phase

If a Codex phase finishes without the expected status marker, inspect the matching output file:

- `develop-stories-output.txt`
- `qa-automation-output.txt`
- `code-review-output.txt`
- `fix-issues-output.txt`
- `retrospective-output.txt`

Then rerun the launcher with `--continue`.

### 10. Permission denied on script

```bash
chmod +x ./.autopilot/bmad-autopilot.sh
```

## Debugging Tips

### Enable verbose logging

```bash
./.autopilot/bmad-autopilot.sh --verbose
tail -f .autopilot/autopilot.log
```

### Manual state transitions

```bash
echo '{"mode":"sequential","phase":"CREATE_PR","current_epic":"1","completed_epics":[],"pending_prs":[],"paused_context":null,"active_phase":null,"active_epic":null,"active_worktree":null}' > .autopilot/state.json
./.autopilot/bmad-autopilot.sh --continue
```

### Run individual phases

The Python runner is easiest to inspect by calling it directly:

```bash
python3 .autopilot/bmad-autopilot.py --help
```

## Getting Help

1. Check `.autopilot/autopilot.log`
2. Check `.autopilot/tmp/`
3. Check `.autopilot/state.json`
4. Open an issue in the autopilot repository
