import importlib.util
import subprocess
import sys
import tempfile
from types import SimpleNamespace
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
def test_int_autopilot_story_selection_is_review_first_then_sequential():
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        story_root = root / "_bmad-output" / "implementation-artifacts"
        story_root.mkdir(parents=True, exist_ok=True)

        ready_story = "3-4-prefer-the-simpler-valid-solution"
        review_story = "5-2-visualize-cad-and-simulation-evidence"
        later_ready_story = "5-3-view-code-and-artifacts"
        for story_key, status in (
            (ready_story, "ready-for-dev"),
            (review_story, "review"),
            (later_ready_story, "ready-for-dev"),
        ):
            (story_root / f"{story_key}.md").write_text(
                f"Status: {status}\n",
                encoding="utf-8",
            )

        status_path = story_root / "sprint-status.yaml"
        status_path.write_text(
            "\n".join(
                [
                    "generated: 2026-03-23T00:00:00Z",
                    "last_updated: 2026-03-23T00:00:00Z",
                    "project: Problemologist-AI",
                    "project_key: NOKEY",
                    "tracking_system: file-system",
                    'story_location: "_bmad-output/implementation-artifacts"',
                    "development_status:",
                    "  epic-3: in-progress",
                    f"  {ready_story}: ready-for-dev",
                    "  epic-5: in-progress",
                    f"  {review_story}: review",
                    f"  {later_ready_story}: ready-for-dev",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.config = SimpleNamespace(start_from="", epic_pattern="")

        sprint_status_raw = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        sprint_status = mod.SprintStatus.model_validate(sprint_status_raw)

        target = runner.select_next_story(sprint_status)
        assert target is not None
        assert target.key == review_story
        assert target.status == mod.SprintStatusValue.REVIEW

        sprint_status_raw["development_status"][review_story] = "done"
        status_path.write_text(
            yaml.safe_dump(sprint_status_raw, sort_keys=False),
            encoding="utf-8",
        )
        refreshed = mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )

        next_target = runner.select_next_story(refreshed)
        assert next_target is not None
        assert next_target.key == ready_story
        assert next_target.status == mod.SprintStatusValue.READY_FOR_DEV


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
def test_int_autopilot_story_flow_finalizes_completed_epic_with_retrospective():
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        story_root = root / "_bmad-output" / "implementation-artifacts"
        story_root.mkdir(parents=True, exist_ok=True)

        epic_id = "1"
        story_keys = [
            "1-1-prepare-epic-review",
            "1-2-record-retrospective-inputs",
        ]
        for story_key in story_keys:
            (story_root / f"{story_key}.md").write_text("Status: done\n", encoding="utf-8")

        status_path = story_root / "sprint-status.yaml"
        status_path.write_text(
            "\n".join(
                [
                    "generated: 2026-03-23T00:00:00Z",
                    "last_updated: 2026-03-23T00:00:00Z",
                    "project: Problemologist-AI",
                    "project_key: NOKEY",
                    "tracking_system: file-system",
                    f'story_location: "{story_root}"',
                    "development_status:",
                    f"  epic-{epic_id}: in-progress",
                    f"  {story_keys[0]}: done",
                    f"  {story_keys[1]}: done",
                    f"  epic-{epic_id}-retrospective: optional",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        runner = object.__new__(mod.AutopilotRunner)
        runner.project_root = root
        runner.tmp_dir = root / ".autopilot" / "tmp"
        runner.tmp_dir.mkdir(parents=True, exist_ok=True)
        runner.state_file = root / ".autopilot" / "state.json"
        runner.state = mod.AutopilotState.initial(False)
        runner.sprint_status_file = status_path
        runner.base_branch = "main"
        runner.config = SimpleNamespace(start_from="", epic_pattern="")

        transitions = []
        prompts: list[str] = []

        runner.load_sprint_status = lambda root=None: mod.SprintStatus.model_validate(
            yaml.safe_load(status_path.read_text(encoding="utf-8"))
        )
        runner.state_current_epic = lambda: epic_id
        runner.state_set = lambda phase, epic=None: transitions.append(
            ("state_set", phase.value if hasattr(phase, "value") else phase, epic)
        )
        runner.log = lambda *args, **kwargs: None
        runner.play_sound = lambda *args, **kwargs: None

        def run_codex_exec(prompt, output_file, cwd=None, reasoning_effort=None):
            prompts.append(str(prompt))
            output_file.write_text(
                "Documented retrospective\nSTATUS: RETROSPECTIVE_COMPLETE\n",
                encoding="utf-8",
            )
            return 0

        runner.run_codex_exec = run_codex_exec

        runner.phase_find_story()
        assert transitions == [("state_set", "EPIC_REVIEW", epic_id)]

        runner.phase_review_epic()

        sprint_status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        assert sprint_status["development_status"][f"epic-{epic_id}"] == "done"
        assert sprint_status["development_status"][f"epic-{epic_id}-retrospective"] == "done"
        assert runner.state.completed_epics == [epic_id]
        assert transitions[-1] == ("state_set", "FIND_EPIC", None)
        assert prompts
        assert f"Epic id: {epic_id}" in prompts[0]
        assert "Document the retrospective and return STATUS: RETROSPECTIVE_COMPLETE when done." in prompts[0]


@pytest.mark.integration_p1
def test_int_autopilot_story_dev_prompt_mentions_prior_review():
    mod = _load_autopilot_module()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        story_key = "1-5-test-story"
        story_path = (
            root / "_bmad-output" / "implementation-artifacts" / f"{story_key}.md"
        )
        status_path = (
            root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        )
        review_artifact = (
            root
            / "_bmad-outputs"
            / "review-artifacts"
            / "qa-review-20260324_080000.md"
        )
        story_path.parent.mkdir(parents=True, exist_ok=True)
        review_artifact.parent.mkdir(parents=True, exist_ok=True)
        story_path.write_text("Status: in-progress\n", encoding="utf-8")
        review_artifact.write_text(
            "\n".join(
                [
                    "---",
                    "review_status: fail",
                    "---",
                    "phase_name: QA_AUTOMATION_TEST",
                    f"source_output: {root / '.autopilot' / 'tmp' / 'qa-story-output.txt'}",
                    "return_code: 0",
                    "",
                    f"Story: {story_key}",
                    "",
                    "output:",
                    "---",
                    "review_status: fail",
                    "---",
                    "QA blocked",
                    "",
                ]
            ),
            encoding="utf-8",
        )
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
                    f"  {story_key}: in-progress",
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

        prompts: list[str] = []

        def run_codex_session_with_retry(*args, **kwargs):
            prompts.append(str(kwargs.get("initial_prompt", "")))
            return mod.CodexAttemptResult(
                return_code=0,
                thread_id="thread-dev-review-note",
                output_text="\n".join(
                    [
                        "---",
                        "workflow_status: stories_complete",
                        f"story_key: {story_key}",
                        "story_status: review",
                        "---",
                        "Implementation complete",
                        "",
                    ]
                ),
            )

        runner.run_codex_session_with_retry = run_codex_session_with_retry

        runner.phase_develop_story()

        assert prompts
        prompt = prompts[0]
        assert "Prior review detected:" in prompt
        assert "There was a qa review at:" in prompt
        assert "Read that review before continuing." in prompt
        assert str(review_artifact) in prompt
        assert transitions[-1] == ("COMMIT_SPLIT", story_key)


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
