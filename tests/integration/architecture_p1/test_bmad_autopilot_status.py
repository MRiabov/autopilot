import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
STATUS_SCRIPT = REPO_ROOT / ".autopilot" / "scripts" / "status.py"


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(root: Path) -> None:
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.email", "codex@example.com")
    _run_git(root, "config", "user.name", "Codex")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-m", "init")


@pytest.mark.integration_p1
def test_int_autopilot_status_reports_only_the_last_run_slice():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        autopilot_dir = root / ".autopilot"
        autopilot_dir.mkdir(parents=True, exist_ok=True)

        state_payload = {
            "mode": "sequential",
            "phase": "DEVELOP_STORIES",
            "current_epic": "2",
            "current_story": "2-4-review-peer-solutions-for-stability",
            "current_story_file": str(
                root
                / "_bmad-output"
                / "implementation-artifacts"
                / "2-4-review-peer-solutions-for-stability.md"
            ),
            "completed_epics": [],
            "pending_prs": [],
            "paused_context": None,
            "active_phase": None,
            "active_epic": None,
            "active_worktree": None,
        }
        (autopilot_dir / "state.json").write_text(
            json.dumps(state_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        (autopilot_dir / "autopilot.log").write_text(
            "\n".join(
                [
                    "[2026-03-24 05:18:19] 🚀 BMAD Autopilot starting story flow (fresh; use --no-continue to force this)",
                    "[2026-03-24 05:18:19] ━━━ Current phase: FIND_EPIC ━━━",
                    "[2026-03-24 05:18:20] 📋 PHASE: FIND_STORY",
                    "[2026-03-24 05:18:21] ✅ Found story: 5-2-visualize-cad-and-simulation-evidence [review]",
                    "[2026-03-24 05:18:22] 📄 Story context: /tmp/older/5-2-visualize-cad-and-simulation-evidence.md",
                    "[2026-03-24 05:18:23] 🔍 PHASE: CODE_REVIEW",
                    "[2026-03-24 05:18:23] Running BMAD code-review workflow for story 5-2-visualize-cad-and-simulation-evidence",
                    "[2026-03-24 05:18:24] ✅ Code review passed; story marked done",
                    "[2026-03-24 09:22:46] 🚀 BMAD Autopilot resuming story flow",
                    "[2026-03-24 09:22:46] ━━━ Current phase: DEVELOP_STORIES ━━━",
                    "[2026-03-24 09:22:47] 📋 PHASE: FIND_STORY",
                    "[2026-03-24 09:22:48] ✅ Found story: 2-4-review-peer-solutions-for-stability [review]",
                    "[2026-03-24 09:22:49] 📄 Story context: /tmp/current/2-4-review-peer-solutions-for-stability.md",
                    "[2026-03-24 09:22:50] 🔍 PHASE: CODE_REVIEW",
                    "[2026-03-24 09:22:50] Running BMAD code-review workflow for story 2-4-review-peer-solutions-for-stability",
                    "[2026-03-24 09:26:49] ❌ Codex reported code review blocked",
                    "[2026-03-24 09:26:51] ↩️ Rerouting to development: Latest persisted code review round 10 is fail",
                    "[2026-03-24 09:26:52] 💻 PHASE: DEVELOP_STORY",
                    '[2026-03-24 09:26:53] event="item.completed" sender="assistant" step=719 item_type="agent_message" item_id="item_719" content="done"',
                    '[2026-03-24 09:26:54] event="session.completed" session_id="session-1" status="ok"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        _init_git_repo(root)

        result = subprocess.run(
            [sys.executable, str(STATUS_SCRIPT)],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )

        output = result.stdout
        assert "Last run: 2026-03-24 09:22:46" in output
        assert "Completed stories:\n- none" in output
        assert "Reviewed stories:\n- 2-4-review-peer-solutions-for-stability" in output
        assert "5-2-visualize-cad-and-simulation-evidence" not in output
        assert "current phase DEVELOP_STORIES" in output
        assert "step FIND_STORY" in output
        assert "step CODE_REVIEW" in output
        assert "code review blocked" in output
        assert "step DEVELOP_STORY" in output
        assert (
            'event="item.completed" sender="assistant" step=719 '
            'item_type="agent_message" item_id="item_719" content="done" '
            '(2-4-review-peer-solutions-for-stability)'
        ) in output
        assert (
            'event="session.completed" session_id="session-1" status="ok" '
            '(2-4-review-peer-solutions-for-stability)'
        ) in output
