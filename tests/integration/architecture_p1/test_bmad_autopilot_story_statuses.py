import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml


def _load_autopilot_module():
    script_path = Path(
        "/home/maksym/Work/proj/Problemologist/Problemologist-AI/.autopilot/scripts/bmad-autopilot.py"
    )
    spec = importlib.util.spec_from_file_location("bmad_autopilot", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
def test_int_autopilot_story_status_lifecycle():
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        story_key = "1-2-test-story"
        story_path = (
            root / "_bmad-output" / "implementation-artifacts" / f"{story_key}.md"
        )
        status_path = (
            root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        )
        story_path.parent.mkdir(parents=True, exist_ok=True)
        story_path.write_text("Status: ready-for-dev\n", encoding="utf-8")
        status_path.write_text(
            "\n".join(
                [
                    "generated: 2026-03-23T00:00:00Z",
                    "last_updated: 2026-03-23T00:00:00Z",
                    "project: Problemologist-AI",
                    "project_key: NOKEY",
                    "tracking_system: file-system",
                    f'story_location: "{root / "_bmad-output" / "implementation-artifacts"}"',
                    "development_status:",
                    "  epic-1: in-progress",
                    f"  {story_key}: ready-for-dev",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _init_git_repo(root)

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
        runner.select_next_story = lambda sprint_status: mod.StoryTarget(
            key=story_key,
            path=story_path,
            status=mod.SprintStatusValue.READY_FOR_DEV,
        )
        runner.build_story_dev_prompt = lambda *args, **kwargs: "prompt"
        runner.build_story_qa_prompt = lambda *args, **kwargs: "prompt"
        runner.build_story_code_review_prompt = lambda *args, **kwargs: "prompt"

        def run_codex_session(
            prompt, output_file, cwd=None, reasoning_effort=None, session_id=None
        ):
            if output_file.name == "develop-story-output.txt":
                story_path.write_text("Status: review\n", encoding="utf-8")
                sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
                sprint_status["development_status"][story_key] = "review"
                status_path.write_text(
                    yaml.safe_dump(sprint_status, sort_keys=False), encoding="utf-8"
                )
                output_file.write_text(
                    "\n".join(
                        [
                            "---",
                            "workflow_status: stories_complete",
                            f"story_key: {story_key}",
                            "story_status: review",
                            "---",
                            "Implementation complete",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif output_file.name == "qa-story-output.txt":
                output_file.write_text(
                    "---\nreview_status: pass\n---\nQA complete\n", encoding="utf-8"
                )
            elif output_file.name == "code-review-output.txt":
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
                            "Review complete",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
            else:
                output_file.write_text(
                    "---\nreview_status: pass\n---\nOK\n", encoding="utf-8"
                )
            return mod.CodexAttemptResult(
                return_code=0,
                thread_id="thread-1",
                output_text=output_file.read_text(encoding="utf-8"),
            )

        runner.run_codex_session = run_codex_session
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.persist_review_artifact = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None
        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )
        runner.log = lambda *args, **kwargs: None

        runner.phase_find_story()
        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: in-progress"
        assert sprint_status["development_status"][story_key] == "in-progress"

        runner.phase_develop_story()
        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: review"
        assert sprint_status["development_status"][story_key] == "review"

        runner.phase_qa_automation_test_story()
        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: review"
        assert sprint_status["development_status"][story_key] == "review"

        runner.phase_code_review_story()
        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: done"
        assert sprint_status["development_status"][story_key] == "done"

        assert transitions[0] == ("DEVELOP_STORIES", story_key)
        assert transitions[1] == ("COMMIT_SPLIT", story_key)
        assert transitions[2] == ("CODE_REVIEW", story_key)
        assert transitions[-1] == ("state_set", "FIND_EPIC", None)


@pytest.mark.integration_p1
def test_int_autopilot_story_status_fallbacks():
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        story_key = "1-3-test-story"
        story_path = (
            root / "_bmad-output" / "implementation-artifacts" / f"{story_key}.md"
        )
        status_path = (
            root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        )
        story_path.parent.mkdir(parents=True, exist_ok=True)
        story_path.write_text("Status: ready-for-dev\n", encoding="utf-8")
        status_path.write_text(
            "\n".join(
                [
                    "generated: 2026-03-23T00:00:00Z",
                    "last_updated: 2026-03-23T00:00:00Z",
                    "project: Problemologist-AI",
                    "project_key: NOKEY",
                    "tracking_system: file-system",
                    f'story_location: "{root / "_bmad-output" / "implementation-artifacts"}"',
                    "development_status:",
                    "  epic-1: in-progress",
                    f"  {story_key}: ready-for-dev",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _init_git_repo(root)

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
        runner.select_next_story = lambda sprint_status: mod.StoryTarget(
            key=story_key,
            path=story_path,
            status=mod.SprintStatusValue.READY_FOR_DEV,
        )
        runner.build_story_dev_prompt = lambda *args, **kwargs: "prompt"
        runner.build_story_qa_prompt = lambda *args, **kwargs: "prompt"
        runner.build_story_code_review_prompt = lambda *args, **kwargs: "prompt"

        def run_codex_session(
            prompt, output_file, cwd=None, reasoning_effort=None, session_id=None
        ):
            if output_file.name == "develop-story-output.txt":
                story_path.write_text("Status: review\n", encoding="utf-8")
                sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
                sprint_status["development_status"][story_key] = "review"
                status_path.write_text(
                    yaml.safe_dump(sprint_status, sort_keys=False), encoding="utf-8"
                )
                output_file.write_text(
                    "\n".join(
                        [
                            "---",
                            "workflow_status: stories_complete",
                            f"story_key: {story_key}",
                            "story_status: review",
                            "---",
                            "Implementation complete",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif output_file.name == "qa-story-output.txt":
                output_file.write_text(
                    "---\nreview_status: pass\n---\nQA complete\n", encoding="utf-8"
                )
            elif output_file.name == "code-review-output.txt":
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
                            "Review complete",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
            else:
                output_file.write_text(
                    "---\nreview_status: pass\n---\nOK\n", encoding="utf-8"
                )
            return mod.CodexAttemptResult(
                return_code=0,
                thread_id="thread-1",
                output_text=output_file.read_text(encoding="utf-8"),
            )

        runner.run_codex_session = run_codex_session
        runner.autopilot_checks = lambda *args, **kwargs: None
        runner.persist_review_artifact = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None
        transitions = []
        runner.state_set_story = lambda phase, sk, sf=None: transitions.append(
            (phase.value if hasattr(phase, "value") else phase, sk)
        )
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )
        runner.log = lambda *args, **kwargs: None

        runner.phase_find_story()
        runner.phase_develop_story()
        runner.phase_qa_automation_test_story()
        runner.phase_code_review_story()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert story_path.read_text(encoding="utf-8").strip() == "Status: done"
        assert sprint_status["development_status"][story_key] == "done"
        assert transitions[0] == ("DEVELOP_STORIES", story_key)
        assert transitions[1] == ("COMMIT_SPLIT", story_key)
        assert transitions[2] == ("CODE_REVIEW", story_key)
        assert transitions[-1] == ("state_set", "FIND_EPIC", None)


@pytest.mark.integration_p1
def test_int_autopilot_review_frontmatter_round_trip():
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_output = root / ".autopilot" / "tmp" / "qa-story-output.txt"
        source_output.parent.mkdir(parents=True, exist_ok=True)
        source_output.write_text(
            "---\nreview_status: pass\n---\nQA passed cleanly.\n",
            encoding="utf-8",
        )

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.log = lambda *args, **kwargs: None

        artifact_path = runner.persist_review_artifact(
            "qa-review",
            phase_name=mod.Phase.QA_AUTOMATION_TEST.value,
            source_output=source_output,
            return_code=0,
            output_text=source_output.read_text(encoding="utf-8"),
            context_lines=["Story: 1-4-test-story"],
            status_hint=None,
        )

        artifact_text = artifact_path.read_text(encoding="utf-8")
        assert artifact_text.startswith("---\nreview_status: pass\n---\n")
        assert (
            runner.review_status_from_output(source_output.read_text(encoding="utf-8"))
            == "pass"
        )
        assert runner.review_status_from_artifact("qa-review") == "pass"
