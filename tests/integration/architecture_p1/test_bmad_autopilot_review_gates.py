import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / ".autopilot" / "scripts" / "bmad-autopilot.py"


def _load_autopilot_module():
    spec = importlib.util.spec_from_file_location(
        "bmad_autopilot_review_gates", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )


def _git(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    result = _run(["git", *command], cwd=cwd, env=env)
    assert result.returncode == 0, result.stderr or result.stdout


def _init_repo(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Codex"
    env["GIT_AUTHOR_EMAIL"] = "codex@example.com"
    env["GIT_COMMITTER_NAME"] = "Codex"
    env["GIT_COMMITTER_EMAIL"] = "codex@example.com"
    _git(["init"], cwd=root, env=env)
    _git(["config", "user.name", "Codex"], cwd=root, env=env)
    _git(["config", "user.email", "codex@example.com"], cwd=root, env=env)
    return env


def _write_fake_codex(bin_dir: Path, marker_file: Path) -> None:
    script = bin_dir / "codex"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from __future__ import annotations",
                "",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                "args = sys.argv[1:]",
                "mode = 'exec'",
                "thread_id = os.environ.get('FAKE_CODEX_THREAD_ID', 'thread-1')",
                "out_file = ''",
                "repo_root = os.getcwd()",
                "i = 0",
                "while i < len(args):",
                "    arg = args[i]",
                "    if arg == 'exec':",
                "        i += 1",
                "        continue",
                "    if arg == 'resume':",
                "        mode = 'resume'",
                "        if i + 1 < len(args):",
                "            thread_id = args[i + 1]",
                "        i += 2",
                "        continue",
                "    if arg in {'-o', '--output-last-message', '--cd', '-c', '-m', '--model'}:",
                "        if i + 1 < len(args):",
                "            if arg in {'-o', '--output-last-message'}:",
                "                out_file = args[i + 1]",
                "            if arg == '--cd':",
                "                repo_root = args[i + 1]",
                "            i += 2",
                "            continue",
                "    if arg in {'--json', '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check', '--full-auto', '-', '--skip-git-repo-check'}:",
                "        i += 1",
                "        continue",
                "    i += 1",
                "",
                "state_file = os.environ.get('FAKE_CODEX_STATE_FILE')",
                "attempts = {}",
                "if state_file:",
                "    state_path = Path(state_file)",
                "    state_path.parent.mkdir(parents=True, exist_ok=True)",
                "    if state_path.exists():",
                "        try:",
                "            attempts = json.loads(state_path.read_text(encoding='utf-8') or '{}')",
                "            if not isinstance(attempts, dict):",
                "                attempts = {}",
                "        except Exception:",
                "            attempts = {}",
                "    attempt = int(attempts.get(Path(out_file).name if out_file else 'default', 0)) + 1",
                "    attempts[Path(out_file).name if out_file else 'default'] = attempt",
                "    state_path.write_text(json.dumps(attempts, sort_keys=True), encoding='utf-8')",
                "else:",
                "    attempt = 1",
                "",
                "mode = os.environ.get('FAKE_CODEX_MODE', '').strip().lower()",
                "",
                "marker_file = os.environ.get('FAKE_CODEX_MARKER')",
                "if marker_file:",
                "    Path(marker_file).parent.mkdir(parents=True, exist_ok=True)",
                "    with open(marker_file, 'a', encoding='utf-8') as fh:",
                "        fh.write(f'codex-called {mode} {thread_id}\\n')",
                "",
                "sys.stdin.read()",
                "",
                "print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}))",
                "print(json.dumps({'type': 'turn.started'}))",
                "if mode == 'logfmt':",
                "    if out_file:",
                "        Path(out_file).parent.mkdir(parents=True, exist_ok=True)",
                "        Path(out_file).write_text('---\\nreview_status: pass\\n---\\nlogfmt test\\n', encoding='utf-8')",
                "    print(json.dumps({'type': 'item.started', 'item': {'id': 'item_718', 'type': 'command_execution', 'command': '/bin/bash -lc echo hello', 'status': 'in_progress'}}))",
                "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_719', 'type': 'agent_message', 'text': 'done'}}))",
                "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_720', 'type': 'file_change', 'path': 'src/example.py'}}))",
                "    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}))",
                "    print(json.dumps({'type': 'session.completed', 'session_id': 'session-1', 'status': 'ok', 'payload': {'nested': 'value'}}))",
                "    sys.exit(0)",
                "if mode == 'quota_retry':",
                "    quota_state_path = Path(state_file) if state_file else None",
                "    quota_attempt = 0",
                "    if quota_state_path and quota_state_path.exists():",
                "        try:",
                "            quota_attempt = int(quota_state_path.read_text(encoding='utf-8').strip() or '0')",
                "        except Exception:",
                "            quota_attempt = 0",
                "    quota_attempt += 1",
                "    if quota_state_path:",
                "        quota_state_path.write_text(str(quota_attempt), encoding='utf-8')",
                "    if quota_attempt == 1:",
                "        message = 'quota exceeded: please retry later\\n'",
                "        rc = 1",
                "    else:",
                "        message = '---\\nreview_status: pass\\n---\\nquota recovered\\n'",
                "        rc = 0",
                "    if out_file:",
                "        Path(out_file).parent.mkdir(parents=True, exist_ok=True)",
                "        Path(out_file).write_text(message, encoding='utf-8')",
                "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'done'}}))",
                "    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}))",
                "    sys.exit(rc)",
                "base = Path(out_file).name if out_file else 'default'",
                "if base in {'develop-story-output.txt', 'develop-stories-output.txt'}:",
                "    import yaml",
                "    import re",
                "    status_path = Path(repo_root) / '_bmad-output' / 'implementation-artifacts' / 'sprint-status.yaml'",
                "    sprint_status = yaml.safe_load(status_path.read_text(encoding='utf-8'))",
                "    story_root = Path(repo_root) / sprint_status['story_location']",
                "    for story_key, story_status in list((sprint_status.get('development_status') or {}).items()):",
                "        if not re.fullmatch(r'\\d+-\\d+-.*', str(story_key)):",
                "            continue",
                "        story_path = story_root / f'{story_key}.md'",
                "        if story_path.exists():",
                "            story_path.write_text('Status: review\\n', encoding='utf-8')",
                "        sprint_status['development_status'][story_key] = 'review'",
                "    status_path.write_text(yaml.safe_dump(sprint_status, sort_keys=False), encoding='utf-8')",
                "    if base == 'develop-story-output.txt':",
                "        story_keys = [key for key in sprint_status['development_status'] if re.fullmatch(r'\\d+-\\d+-.*', str(key))]",
                "        story_key = story_keys[0] if story_keys else '1-1-story'",
                "        message = '\\n'.join(['---', 'workflow_status: stories_complete', f'story_key: {story_key}', 'story_status: review', '---', 'initial attempt', ''])",
                "    else:",
                "        epic_key = next((key for key in sprint_status['development_status'] if str(key).startswith('epic-')), 'epic-1')",
                "        message = '\\n'.join(['---', 'workflow_status: stories_complete', f'epic_id: {str(epic_key).removeprefix(\"epic-\")}', 'story_status: review', '---', 'initial attempt', ''])",
                "elif base == 'code-review-output.txt':",
                "    if attempt == 1:",
                "        message = '---\\nreview_status: pass\\n---\\ninitial attempt\\n'",
                "    else:",
                "        import hashlib",
                "        import subprocess",
                "",
                "        def git(*git_args):",
                "            return subprocess.run(['git', '-C', repo_root, *git_args], capture_output=True, text=True, check=False).stdout.strip()",
                "",
                "        def filter_internal(text):",
                "            ignored_prefixes = ('.autopilot/tmp/', '.autopilot/state.json', '.autopilot/autopilot.log', '_bmad-outputs/review-artifacts/')",
                "            lines = []",
                "            for raw_line in text.splitlines():",
                "                line = raw_line.strip()",
                "                if not line:",
                "                    continue",
                "                if any(prefix in line for prefix in ignored_prefixes):",
                "                    continue",
                "                lines.append(line)",
                "            return '\\n'.join(lines)",
                "",
                "        current_branch = git('branch', '--show-current')",
                "        branch_diff = filter_internal(git('diff', '--name-only', 'origin/main..HEAD'))",
                "        staged_diff = filter_internal(git('diff', '--name-only', '--cached'))",
                "        unstaged_diff = filter_internal(git('diff', '--name-only'))",
                "        working_tree_status = filter_internal(git('status', '--short'))",
                "        working_tree_status = '\\n'.join(line for line in working_tree_status.splitlines() if not line.startswith('?? .autopilot/') and not line.startswith('?? .autopilot'))",
                "        payload = {",
                "            'current_branch': current_branch,",
                "            'branch_diff': [line for line in branch_diff.splitlines() if line.strip()],",
                "            'staged_diff': [line for line in staged_diff.splitlines() if line.strip()],",
                "            'unstaged_diff': [line for line in unstaged_diff.splitlines() if line.strip()],",
                "            'working_tree_status': working_tree_status.splitlines(),",
                "        }",
                "        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()",
                "        reviewed = payload['branch_diff'] + payload['staged_diff'] + payload['unstaged_diff']",
                "        if not reviewed:",
                "            reviewed = ['_bmad-output/implementation-artifacts/sprint-status.yaml']",
                "        message = '\\n'.join(['---', 'review_status: pass', f'review_scope_fingerprint: {fingerprint}', 'reviewed_files:', *[f'  - {item}' for item in reviewed], '---', 'retried attempt', ''])",
                "else:",
                "    message = '---\\nreview_status: pass\\n---\\ninitial attempt\\n'",
                "if out_file:",
                "    Path(out_file).parent.mkdir(parents=True, exist_ok=True)",
                "    Path(out_file).write_text(message, encoding='utf-8')",
                "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'done'}}))",
                "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    marker_file.parent.mkdir(parents=True, exist_ok=True)


def _write_story_workspace(
    root: Path, *, story_status: str = "review"
) -> tuple[str, Path, Path]:
    story_root = root / "_bmad-output" / "implementation-artifacts"
    story_root.mkdir(parents=True, exist_ok=True)
    story_key = "1-1-autopilot-regression"
    story_path = story_root / f"{story_key}.md"
    story_path.write_text(f"Status: {story_status}\n", encoding="utf-8")
    sprint_status = {
        "generated": "2026-03-23T00:00:00Z",
        "last_updated": "2026-03-23T00:00:00Z",
        "project": "Problemologist-AI",
        "project_key": "NOKEY",
        "tracking_system": "file-system",
        "story_location": "_bmad-output/implementation-artifacts",
        "development_status": {
            "epic-1": "in-progress",
            story_key: story_status,
        },
    }
    status_path = story_root / "sprint-status.yaml"
    status_path.write_text(
        yaml.safe_dump(sprint_status, sort_keys=False), encoding="utf-8"
    )
    return story_key, story_path, status_path


@pytest.mark.integration_p2
def test_int_autopilot_requires_explicit_confirmation_on_dirty_workspace():
    """
    INT-206: BMAD autopilot must require an explicit yes/no confirmation before
    continuing on a dirty workspace.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        _write_story_workspace(root, story_status="review")
        codex_marker_present = False

        with tempfile.TemporaryDirectory() as tool_tmp:
            tool_root = Path(tool_tmp)
            marker_file = tool_root / "codex-marker.txt"
            _write_fake_codex(tool_root, marker_file)
            base_path = env.get("PATH", "")
            env["PATH"] = f"{tool_root}{os.pathsep}{base_path}"
            env["FAKE_CODEX_MARKER"] = str(marker_file)

            result = _run(
                ["python3", str(SCRIPT_PATH)],
                cwd=root,
                env=env,
                input_text="n\n",
            )
            codex_marker_present = marker_file.exists()

        assert result.returncode == 1, result.stdout + result.stderr
        assert "Continue anyway? [y/N]" in result.stdout
        assert "Aborted by user." in result.stdout
        assert not codex_marker_present, (
            "codex must not run when the user rejects the dirty-tree prompt"
        )


@pytest.mark.integration_p2
def test_int_autopilot_accept_dirty_worktree_skips_prompt(monkeypatch):
    """
    `--accept-dirty-worktree` must bypass the interactive prompt entirely.
    """
    mod = _load_autopilot_module()

    runner = object.__new__(mod.AutopilotRunner)
    runner.config = mod.RuntimeConfig(accept_dirty_worktree=True)

    messages: list[str] = []
    runner.log = lambda message: messages.append(str(message))
    runner.run_text = lambda *args, **kwargs: " M dirty-file.py\n"

    monkeypatch.setattr(
        "builtins.input",
        lambda *args, **kwargs: pytest.fail("dirty-tree prompt should be skipped"),
    )

    runner.confirm_dirty_worktree(Path("/tmp"), context="story flow")

    assert any("Dirty worktree accepted via --accept-dirty-worktree." in line for line in messages)
    assert not any("Continue anyway?" in line for line in messages)


@pytest.mark.integration_p2
def test_int_autopilot_review_allows_extra_repo_relative_files():
    mod = _load_autopilot_module()

    runner = object.__new__(mod.AutopilotRunner)
    output_text = "\n".join(
        [
            "---",
            "review_status: pass",
            "review_scope_fingerprint: fingerprint-1",
            "reviewed_files:",
            "  - src/core.py",
            "  - docs/notes.md",
            "  - _bmad-output/review-artifacts/code-review-round-1.md",
            "---",
            "reviewed additional repo files",
            "",
        ]
    )

    parsed, failure = runner.parse_review_output(
        output_text,
        expected_fingerprint="fingerprint-1",
        valid_files={"src/core.py"},
    )

    assert failure is None
    assert parsed is not None
    assert parsed.reviewed_files == [
        "src/core.py",
        "docs/notes.md",
        "_bmad-output/review-artifacts/code-review-round-1.md",
    ]


@pytest.mark.integration_p2
def test_int_autopilot_review_rejects_non_repo_relative_files():
    mod = _load_autopilot_module()

    runner = object.__new__(mod.AutopilotRunner)
    output_text = "\n".join(
        [
            "---",
            "review_status: pass",
            "review_scope_fingerprint: fingerprint-1",
            "reviewed_files:",
            "  - /tmp/outside.txt",
            "---",
            "bad path",
            "",
        ]
    )

    parsed, failure = runner.parse_review_output(
        output_text,
        expected_fingerprint="fingerprint-1",
        valid_files={"src/core.py"},
    )

    assert parsed is None
    assert failure is not None
    assert failure.error_code == "invalid_reviewed_files"


@pytest.mark.integration_p2
def test_int_autopilot_reroutes_empty_review_source_back_to_dev():
    """
    INT-207: BMAD autopilot must reroute empty review scope back to
    development without invoking the review tool.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        story_key, story_path, status_path = _write_story_workspace(
            root, story_status="review"
        )
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed review workspace"], cwd=root, env=env)

        mod = _load_autopilot_module()

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"

        runner.state_current_story = lambda: story_key
        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.story_file_for_key = lambda sprint_status, key, root=None: story_path
        runner.collect_review_source_snapshot = lambda repo_root: (
            mod.ReviewSourceSnapshot(
                current_branch="main",
                branch_diff="",
                staged_diff="",
                unstaged_diff="",
                working_tree_status="",
                has_reviewable_source=False,
            )
        )
        runner.persist_review_artifact = lambda *args, **kwargs: None
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None
        runner.log = lambda *args, **kwargs: None

        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )

        runner.phase_code_review_story()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: in-progress"
        assert sprint_status["development_status"][story_key] == "in-progress"
        assert transitions[-1] == ("DEVELOP_STORIES", story_key)
        assert not any(
            item[0] == "state_set" and item[1] == "BLOCKED" for item in transitions
        )


@pytest.mark.integration_p2
def test_int_autopilot_review_artifacts_are_workspace_scoped():
    """
    INT-208: review artifacts must persist under the active workspace root and
    be discoverable from that same root.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        (root / "note.txt").write_text("seed\n", encoding="utf-8")
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed repo"], cwd=root, env=env)

        worktree_root = root / "worktrees" / "epic-1"
        _git(
            ["worktree", "add", "-b", "feature/epic-1", str(worktree_root), "HEAD"],
            cwd=root,
            env=env,
        )

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.log = lambda *args, **kwargs: None

        source_output = worktree_root / ".autopilot" / "tmp" / "code-review-output.txt"
        source_output.parent.mkdir(parents=True, exist_ok=True)
        source_output.write_text(
            "---\nreview_status: pass\n---\nWorkspace review\n", encoding="utf-8"
        )

        artifact = runner.persist_review_artifact(
            "code-review",
            phase_name=mod.Phase.CODE_REVIEW.value,
            source_output=source_output,
            return_code=0,
            output_text=source_output.read_text(encoding="utf-8"),
            context_lines=["Workspace root: worktree"],
            root=worktree_root,
        )

        assert artifact.is_file()
        assert artifact.parent == worktree_root / "_bmad-outputs" / "review-artifacts"
        latest_workspace_artifacts = runner.latest_review_artifacts(root=worktree_root)
        assert latest_workspace_artifacts["code-review"] == artifact
        assert (
            runner.review_status_from_artifact("code-review", root=worktree_root)
            == "pass"
        )
        assert runner.latest_review_artifacts(root=root).get("code-review") is None


@pytest.mark.integration_p2
def test_int_autopilot_qa_fail_reroutes_story_back_to_dev():
    """
    INT-209: a failed QA pass must reroute the story back to development
    instead of hard-blocking the run.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        story_key, story_path, status_path = _write_story_workspace(
            root, story_status="review"
        )
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed qa reroute workspace"], cwd=root, env=env)

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"

        runner.state_current_story = lambda: story_key
        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.story_file_for_key = lambda sprint_status, key, root=None: story_path
        runner.persist_review_artifact = lambda *args, **kwargs: None
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None
        runner.log = lambda *args, **kwargs: None

        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )

        def run_codex_session(
            prompt, output_file, cwd=None, reasoning_effort=None, session_id=None
        ):
            output_file.write_text(
                "---\nreview_status: fail\n---\nQA blockers remain\n",
                encoding="utf-8",
            )
            return mod.CodexAttemptResult(
                return_code=0,
                thread_id="thread-qa-fail",
                output_text=output_file.read_text(encoding="utf-8"),
            )

        runner.run_codex_session = run_codex_session

        runner.phase_qa_automation_test_story()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: in-progress"
        assert sprint_status["development_status"][story_key] == "in-progress"
        assert transitions[-1] == ("DEVELOP_STORIES", story_key)
        assert not any(
            item[0] == "state_set" and item[1] == "BLOCKED" for item in transitions
        )


@pytest.mark.integration_p2
def test_int_autopilot_story_dev_validation_failure_reroutes_to_dev():
    """
    INT-210: a story-development validation failure must reroute back to
    development instead of blocking the run.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        story_key, story_path, status_path = _write_story_workspace(
            root, story_status="ready-for-dev"
        )
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed dev reroute workspace"], cwd=root, env=env)

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"

        runner.state_current_story = lambda: story_key
        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.story_file_for_key = lambda sprint_status, key, root=None: story_path
        runner.build_story_dev_prompt = lambda *args, **kwargs: "prompt"
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.log = lambda *args, **kwargs: None

        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )

        def run_codex_session_with_retry(*args, **kwargs):
            return mod.CodexAttemptResult(
                return_code=1,
                thread_id="thread-dev-fail",
                output_text="broken output",
                validation_failure=mod.ValidationFailure(
                    error_code="missing_frontmatter",
                    field="frontmatter",
                    message="missing YAML frontmatter",
                    expected="YAML frontmatter only",
                ),
            )

        runner.run_codex_session_with_retry = run_codex_session_with_retry

        runner.phase_develop_story()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: in-progress"
        assert sprint_status["development_status"][story_key] == "in-progress"
        assert transitions[-1] == ("DEVELOP_STORIES", story_key)
        assert not any(
            item[0] == "state_set" and item[1] == "BLOCKED" for item in transitions
        )


@pytest.mark.integration_p2
def test_int_autopilot_code_review_validation_failure_reroutes_to_dev():
    """
    INT-211: a code-review validation failure must reroute the story back to
    development instead of hard-blocking the run.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        story_key, story_path, status_path = _write_story_workspace(
            root, story_status="review"
        )
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed code review reroute workspace"], cwd=root, env=env)

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"

        runner.state_current_story = lambda: story_key
        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.story_file_for_key = lambda sprint_status, key, root=None: story_path
        runner.collect_review_source_snapshot = lambda repo_root: (
            mod.ReviewSourceSnapshot(
                current_branch="main",
                branch_diff="",
                staged_diff="",
                unstaged_diff="",
                working_tree_status="",
                has_reviewable_source=True,
            )
        )
        runner.review_scope_fingerprint = lambda source: "fingerprint-1"
        runner.review_scope_file_names = lambda text: []
        runner.build_story_code_review_prompt = lambda *args, **kwargs: "prompt"
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None
        runner.log = lambda *args, **kwargs: None

        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )

        def run_codex_session_with_retry(*args, **kwargs):
            return mod.CodexAttemptResult(
                return_code=1,
                thread_id="thread-review-fail",
                output_text="broken output",
                validation_failure=mod.ValidationFailure(
                    error_code="mismatched_review_scope_fingerprint",
                    field="review_scope_fingerprint",
                    message="review_scope_fingerprint does not match the current workspace snapshot",
                    expected="fingerprint-1",
                ),
            )

        runner.run_codex_session_with_retry = run_codex_session_with_retry

        runner.phase_code_review_story()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: in-progress"
        assert sprint_status["development_status"][story_key] == "in-progress"
        assert transitions[-1] == ("DEVELOP_STORIES", story_key)
        assert not any(
            item[0] == "state_set" and item[1] == "BLOCKED" for item in transitions
        )


@pytest.mark.integration_p2
def test_int_autopilot_story_dev_blocked_reroutes_immediately():
    """
    INT-212: a transient story-development blocked response must reroute
    immediately without marking the story blocked.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        story_key, story_path, status_path = _write_story_workspace(
            root, story_status="in-progress"
        )
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed blocked-dev workspace"], cwd=root, env=env)

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"

        runner.state_current_story = lambda: story_key
        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.story_file_for_key = lambda sprint_status, key, root=None: story_path
        runner.build_story_dev_prompt = lambda *args, **kwargs: "prompt"
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.log = lambda *args, **kwargs: None

        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )

        def run_codex_session_with_retry(*args, **kwargs):
            return mod.CodexAttemptResult(
                return_code=0,
                thread_id="thread-dev-blocked",
                output_text="\n".join(
                    [
                        "---",
                        "workflow_status: stories_blocked",
                        f"story_key: {story_key}",
                        "story_status: in-progress",
                        "blocking_reason: out of quota, retry later",
                        "---",
                        "Retry later",
                        "",
                    ]
                ),
            )

        runner.run_codex_session_with_retry = run_codex_session_with_retry

        runner.phase_develop_story()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert "Status: in-progress" in story_path.read_text(encoding="utf-8")
        assert story_path.read_text(encoding="utf-8").strip() == "Status: in-progress"
        assert sprint_status["development_status"][story_key] == "in-progress"
        assert transitions[-1] == ("DEVELOP_STORIES", story_key)
        assert not any(
            item[0] == "state_set" and item[1] == "BLOCKED" for item in transitions
        )


@pytest.mark.integration_p2
def test_int_autopilot_retries_code_review_in_same_session():
    """
    The retry loop must continue the same Codex session instead of starting a
    fresh conversation when validation fails.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = _init_repo(root)
        story_key, story_path, status_path = _write_story_workspace(
            root, story_status="review"
        )
        _git(["add", "-A"], cwd=root, env=env)
        _git(["commit", "-m", "seed review workspace"], cwd=root, env=env)

        story_path.write_text(
            "Status: review\nUpdated after commit\n", encoding="utf-8"
        )
        status_path.write_text(
            status_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"

        runner.state_current_story = lambda: story_key
        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.story_file_for_key = lambda sprint_status, key, root=None: story_path
        runner.persist_review_artifact = lambda *args, **kwargs: None
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None
        runner.log = lambda *args, **kwargs: None

        calls: list[dict[str, str | None]] = []

        def run_codex_session(
            prompt, output_file, cwd=None, reasoning_effort=None, session_id=None
        ):
            calls.append(
                {"session_id": session_id, "prompt": prompt, "output": output_file.name}
            )
            if len(calls) == 1:
                output_file.write_text(
                    "---\nreview_status: pass\n---\nfirst attempt\n", encoding="utf-8"
                )
                return mod.CodexAttemptResult(
                    return_code=0,
                    thread_id="thread-abc",
                    output_text=output_file.read_text(encoding="utf-8"),
                )

            assert session_id == "thread-abc"
            source = runner.collect_review_source_snapshot(root)
            fingerprint = runner.review_scope_fingerprint(source)
            reviewed_files = (
                runner.review_scope_file_names(source.branch_diff)
                + runner.review_scope_file_names(source.staged_diff)
                + runner.review_scope_file_names(source.unstaged_diff)
            )
            output_file.write_text(
                "\n".join(
                    [
                        "---",
                        "review_status: pass",
                        f"review_scope_fingerprint: {fingerprint}",
                        "reviewed_files:",
                        *[f"  - {path}" for path in reviewed_files],
                        "---",
                        "second attempt",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            return mod.CodexAttemptResult(
                return_code=0,
                thread_id="thread-abc",
                output_text=output_file.read_text(encoding="utf-8"),
            )

        runner.run_codex_session = run_codex_session

        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )

        runner.phase_code_review_story()

        assert len(calls) == 2
        assert calls[0]["session_id"] is None
        assert calls[1]["session_id"] == "thread-abc"
        assert "review_scope_fingerprint" in calls[1]["prompt"]
        assert transitions[-1] == ("state_set", "FIND_EPIC", None)


@pytest.mark.integration_p2
def test_int_autopilot_codex_json_events_are_logged_as_structlog():
    """
    INT-213: Codex JSON item events must be rendered into structlog-style
    key/value lines that include sender, step, and content.
    """
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        with tempfile.TemporaryDirectory() as tool_tmp:
            tool_root = Path(tool_tmp)
            marker_file = tool_root / "codex-marker.txt"
            _write_fake_codex(tool_root, marker_file)

            original_path = os.environ.get("PATH", "")
            original_marker = os.environ.get("FAKE_CODEX_MARKER")
            original_mode = os.environ.get("FAKE_CODEX_MODE")
            os.environ["PATH"] = f"{tool_root}{os.pathsep}{original_path}"
            os.environ["FAKE_CODEX_MARKER"] = str(marker_file)
            os.environ["FAKE_CODEX_MODE"] = "logfmt"

            try:
                runner = object.__new__(mod.AutopilotRunner)
                runner.project_root = root
                runner.tmp_dir = root / ".autopilot" / "tmp"
                runner.tmp_dir.mkdir(parents=True, exist_ok=True)
                runner.config = mod.RuntimeConfig()
                runner.codex_reasoning_effort = "high"
                runner.codex_switcher = SimpleNamespace(maybe_switch=lambda *_args, **_kwargs: None)

                messages: list[str] = []
                runner.log = lambda message: messages.append(str(message))

                result = runner.run_codex_session(
                    "prompt", output_file=runner.tmp_dir / "codex-output.txt", cwd=root
                )

                assert result.return_code == 0
                assert any("event=\"item.started\"" in line for line in messages)
                assert any(
                    "event=\"item.started\"" in line
                    and "sender=\"tool\"" in line
                    and "step=718" in line
                    and "item_type=\"command_execution\"" in line
                    and "content=\"/bin/bash -lc" in line
                    for line in messages
                )
                assert any(
                    "event=\"item.completed\"" in line
                    and "sender=\"assistant\"" in line
                    and "step=719" in line
                    and "item_type=\"agent_message\"" in line
                    and "content=\"done\"" in line
                    for line in messages
                )
                assert any(
                    "event=\"item.completed\"" in line
                    and "sender=\"tool\"" in line
                    and "step=720" in line
                    and "item_type=\"file_change\"" in line
                    and "content=\"src/example.py\"" in line
                    for line in messages
                )
                assert any(
                    "event=\"session.completed\"" in line
                    and "session_id=\"session-1\"" in line
                    and "status=\"ok\"" in line
                    and "payload=" in line
                    for line in messages
                )
            finally:
                os.environ["PATH"] = original_path
                if original_marker is None:
                    os.environ.pop("FAKE_CODEX_MARKER", None)
                else:
                    os.environ["FAKE_CODEX_MARKER"] = original_marker
                if original_mode is None:
                    os.environ.pop("FAKE_CODEX_MODE", None)
                else:
                    os.environ["FAKE_CODEX_MODE"] = original_mode
