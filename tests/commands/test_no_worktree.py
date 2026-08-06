"""Unit tests for use_git_worktree configuration and --no-worktree CLI flag."""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from getworktree.cli import app
from getworktree.commands.task.command import task_list_command, task_run_command
from getworktree.commands.workflow.command import workflow_run_command
from getworktree.core.catalog.inventory import create_catalog_item, get_catalog_dir
from getworktree.core.config.generator import generate_default_config
from getworktree.core.config.loader import load_config
from getworktree.core.workflows.models import WorkflowSandbox
from getworktree.core.workflows.runner.runner import run_workflow_iteration
from getworktree.core.workflows.runner_models import WorkflowFinalStatus
from getworktree.core.workflows.seeder import seed_starter_workflows
from getworktree.core.workflows.validate import validate_workflow_result

runner = CliRunner()


def test_task_blueprint_use_git_worktree_parsing(tmp_path: Path) -> None:
    tasks_dir = get_catalog_dir(tmp_path) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_file = tasks_dir / "in-place-task.yml"
    task_file.write_text(
        "name: in-place-task\n"
        "description: Run linters in-place\n"
        "summary: In-place task\n"
        "use_git_worktree: false\n",
        encoding="utf-8",
    )

    outcome = task_list_command(cwd=tmp_path)
    assert outcome.ok
    assert len(outcome.items) == 1
    assert outcome.items[0].name == "in-place-task"
    assert outcome.items[0].use_git_worktree is False


def test_task_run_command_no_worktree_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    create_catalog_item("task", "sample-task", cwd=tmp_path)

    # Default run
    res1 = task_run_command("sample-task", cwd=tmp_path)
    assert res1.ok

    # Run with --no-worktree
    res2 = task_run_command("sample-task", cwd=tmp_path, no_worktree=True)
    assert res2.ok

    # CLI test
    result = runner.invoke(app, ["task", "run", "sample-task", "--no-worktree"])
    assert result.exit_code == 0


def test_workflow_sandbox_use_git_worktree_model() -> None:
    sandbox = WorkflowSandbox(
        auto_clean=True,
        keep_on_failure=False,
        use_git_worktree=False,
    )
    assert sandbox.use_git_worktree is False

    default_sandbox = WorkflowSandbox(
        auto_clean=True,
        keep_on_failure=False,
    )
    assert default_sandbox.use_git_worktree is True


def test_run_workflow_iteration_in_place(tmp_path: Path) -> None:
    config_path = tmp_path / ".worktree" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    generate_default_config(config_path, project_name="test-project")
    config = load_config(cwd=tmp_path)

    workflows_dir = tmp_path / ".worktree" / "workflows"
    seed_starter_workflows(workflows_dir)

    workflow_file = workflows_dir / "fix-tests.yml"
    val = validate_workflow_result(workflow_file)
    assert val.ok and val.workflow is not None
    workflow = val.workflow
    workflow.sandbox.use_git_worktree = False

    def dummy_trigger(*args, **kwargs):
        from getworktree.core.workflows.trigger import (
            TriggerRunResult,
            TriggerRunStatus,
        )

        return TriggerRunResult(
            status=TriggerRunStatus.PASSED,
            command="echo",
            args=["hello"],
            cwd=tmp_path,
            exit_code=0,
            stdout="OK",
            stderr="",
            duration_ms=100,
        )

    result = run_workflow_iteration(
        workflow=workflow,
        cwd=tmp_path,
        config=config,
        run_trigger_fn=dummy_trigger,
    )

    assert result.status == WorkflowFinalStatus.PASSED
    assert result.sandbox_path == tmp_path
    assert result.sandbox_retained is False


def test_workflow_run_command_no_worktree_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".worktree" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    generate_default_config(config_path, project_name="test-project")

    workflows_dir = tmp_path / ".worktree" / "workflows"
    seed_starter_workflows(workflows_dir)

    def dummy_runner(*args, **kwargs):
        from getworktree.core.workflows.runner_models import (
            StopReason,
            WorkflowFinalStatus,
            WorkflowRunResult,
        )

        assert kwargs.get("use_git_worktree") is False
        return WorkflowRunResult(
            status=WorkflowFinalStatus.PASSED,
            session_id="wf_test",
            workflow_name="fix-tests",
            sandbox_path=tmp_path,
            attempts=[],
            stop_reason=StopReason.TRIGGER_PASSED,
        )

    with pytest.raises(typer.Exit) as exc_info:
        workflow_run_command(
            "fix-tests",
            cwd=tmp_path,
            no_worktree=True,
            run_workflow_fn=dummy_runner,
        )
    assert exc_info.value.exit_code == 0
