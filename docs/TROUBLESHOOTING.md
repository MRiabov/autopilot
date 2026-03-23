# Troubleshooting Guide

## Common Issues

### 1. Required command not found

If the launcher says a command is missing, install the missing prerequisite and retry.

Typical requirements:

- `python3`
- `codex`
- `git`

Legacy flow only:

- `gh`

### 2. Git working tree not clean

Story flow continues unattended even if the tree is dirty, but you should still review the state before launching.

If you want a clean start:

```bash
git add -A && git commit -m "wip"
# or
git stash
```

### 3. Story is already in `review` but nothing happens

Check the current story row in sprint status and the matching story file:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml
data = yaml.safe_load(Path('_bmad-output/implementation-artifacts/sprint-status.yaml').read_text())
print({k: v for k, v in data['development_status'].items() if v in {'in-progress', 'review', 'ready-for-dev', 'backlog'}})
PY
```

If the story is still `review`, the next phase should be `code-review`. If the runner is stuck in an old legacy state, delete `.autopilot/state.json` and rerun the launcher.

### 4. Legacy state file blocks story flow

Symptoms:

- The old `BLOCKED` or PR-centric state remains in `.autopilot/state.json`
- Story flow starts but immediately fails to dispatch

Fix:

```bash
rm .autopilot/state.json
./.autopilot/bmad-autopilot.sh
```

The story flow will also reset stale legacy state automatically on a fresh launch.

### 5. No active stories too early

If the runner exits with `No more active stories in sprint-status.yaml`, check the sprint status file:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml
data = yaml.safe_load(Path('_bmad-output/implementation-artifacts/sprint-status.yaml').read_text())
print([key for key, value in data['development_status'].items() if key and not key.startswith('epic-') and not key.endswith('-retrospective') and value != 'done'])
PY
```

If all stories are done, that exit is correct.

### 6. Story status updated, but sprint status did not change

Inspect the live story file and sprint file. Story flow updates both files on review success. If only the story file changed, the runner likely hit a file-write error while updating `_bmad-output/implementation-artifacts/sprint-status.yaml`.

### 7. Codex stops before finishing a phase

If a Codex phase finishes without the expected output, inspect the matching file:

- `create-story-output.txt`
- `develop-story-output.txt`
- `qa-story-output.txt`
- `code-review-output.txt`
- `fix-issues-output.txt`
- `retrospective-output.txt`

Then rerun the launcher with `--continue`.

### 8. Permission denied on script

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
echo '{"mode":"sequential","phase":"FIND_EPIC","current_epic":null,"current_story":null,"current_story_file":null,"completed_epics":[],"pending_prs":[],"paused_context":null,"active_phase":null,"active_epic":null,"active_worktree":null}' > .autopilot/state.json
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
