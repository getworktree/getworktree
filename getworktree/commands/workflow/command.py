"""Workflow command handlers: list, show, run, and resume workflow sessions."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from getworktree.commands.workflow.renderers import (
    build_patch_review_panel,
    exit_code_for_status,
    format_progress_event,
    format_run_output,
    render_workflow_list,
)
from getworktree.common.utils import RichOutput
from getworktree.core.config.loader import ConfigLoadStatus, load_config_result
from getworktree.core.db import get_sandbox, get_workflow_run, list_workflow_runs
from getworktree.core.workflows.render import (
    format_workflow_show_resolve_failure,
    format_workflow_show_validate_failure,
)
from getworktree.core.workflows.resolve import resolve_workflow_by_name
from getworktree.core.workflows.runner import (
    StopReason,
    WorkflowRunResult,
    run_workflow_iteration,
)
from getworktree.core.workflows.validate import validate_workflow_result

rich_output = RichOutput()


def workflow_list_command(*, cwd: Path | None = None) -> None:
    """Query recorded workflow run sessions and render a formatted table.

    Read-only: does not mutate workflow files or start sandboxes.
    Exit ``0`` on success (including when no recorded workflows exist);
    exit ``1`` on uninitialized worktree or config load failure.

    Args:
        cwd: Repository root. Defaults to process CWD.
    """
    root = (cwd or Path.cwd()).resolve()
    load = load_config_result(cwd=root)
    if not load.ok or load.config is None:
        message = (
            load.errors[0]
            if load.errors
            else "Worktree is not initialized. Run `wt init`."
        )
        rich_output.error_panel("Workflow List Failed", message)
        raise typer.Exit(code=1)

    workflows = list_workflow_runs(cwd=root)
    render_workflow_list(workflows, cwd=root, rich_output=rich_output)
    raise typer.Exit(code=0)


def _format_warning_bullets(warnings: list[str]) -> list[str]:
    """Format engine warnings as bullet lines with indented continuations."""
    lines: list[str] = []
    for warning in warnings:
        parts = warning.splitlines() or [""]
        lines.append(f"- {parts[0]}")
        for continuation in parts[1:]:
            lines.append(f"  {continuation}")
    return lines


def workflow_show_command(session_id: str, *, cwd: Path | None = None) -> None:
    """Show details for a specific workflow session by session ID.

    Read-only: does not mutate workflow files or start sandboxes.
    Exit ``0`` when workflow session is found; exit ``1`` on failure or missing session.

    Args:
        session_id: Workflow session ID to show.
        cwd: Repository root. Defaults to process CWD.
    """
    root = (cwd or Path.cwd()).resolve()
    load = load_config_result(cwd=root)
    if not load.ok or load.config is None:
        message = (
            load.errors[0]
            if load.errors
            else "Worktree is not initialized. Run `wt init`."
        )
        rich_output.error_panel("Workflow Show Failed", message)
        raise typer.Exit(code=1)

    row = get_workflow_run(session_id, cwd=root) or get_sandbox(session_id, cwd=root)
    if row is None:
        rich_output.error_panel(
            "Workflow Show Failed",
            f"Workflow session '{session_id}' not found.",
        )
        raise typer.Exit(code=1)

    sid = getattr(row, "session_id", getattr(row, "id", session_id))
    name = getattr(row, "workflow_name", getattr(row, "name", None)) or "-"
    branch = getattr(row, "branch_name", "-")
    status = row.status.value if hasattr(row.status, "value") else str(row.status)
    started = getattr(row, "started_at", getattr(row, "created_at", "-"))
    completed = getattr(row, "completed_at", None)
    err_msg = getattr(row, "error_message", None)

    rich_output.info(f"Workflow Session: {sid}")
    rich_output.info(f"Name:             {name}")
    rich_output.info(f"Branch:           {branch}")
    rich_output.info(f"Status:           {status}")
    rich_output.info(f"Started At:       {started}")
    if completed:
        rich_output.info(f"Completed At:     {completed}")
    if err_msg:
        rich_output.info(f"Error:            {err_msg}")
    raise typer.Exit(code=0)


def workflow_resume_command(session_id: str, *, cwd: Path | None = None) -> None:
    """Resume an interrupted workflow session by session ID.

    Exit ``0`` when workflow session is resumed; exit ``1`` on missing session.

    Args:
        session_id: Workflow session ID to resume.
        cwd: Repository root. Defaults to process CWD.
    """
    root = (cwd or Path.cwd()).resolve()
    load = load_config_result(cwd=root)
    if not load.ok or load.config is None:
        message = (
            load.errors[0]
            if load.errors
            else "Worktree is not initialized. Run `wt init`."
        )
        rich_output.error_panel("Workflow Resume Failed", message)
        raise typer.Exit(code=1)

    row = get_workflow_run(session_id, cwd=root) or get_sandbox(session_id, cwd=root)
    if row is None:
        rich_output.error_panel(
            "Workflow Resume Failed",
            f"Workflow session '{session_id}' not found.",
        )
        raise typer.Exit(code=1)

    rich_output.info(f"Resuming workflow session '{session_id}'...")
    raise typer.Exit(code=0)


def _make_approve_callback(
    *,
    attempt_holder: dict[str, int],
) -> Callable[[str], bool]:
    """Build an approval callback that shows the diff, then prompts (non-TTY → deny)."""

    def approve_patch(diff: str) -> bool:
        attempt = attempt_holder.get("attempt", 1)
        rich_output.console.print(build_patch_review_panel(diff))
        prompt = f"Apply agent patch for attempt {attempt}? [y/N]"
        if not sys.stdin.isatty():
            rich_output.info(prompt)
            rich_output.info("Non-interactive stdin: treating approval as rejected.")
            return False
        return bool(
            rich_output.console.input(prompt + " ").strip().lower() in {"y", "yes"}
        )

    return approve_patch


def workflow_run_command(
    name: str,
    *,
    max_attempts: int | None = None,
    keep: bool | None = None,
    approve_each: bool | None = None,
    wip: bool = False,
    dump_prompt: bool = False,
    no_worktree: bool = False,
    cwd: Path | None = None,
    run_workflow_fn: Callable[..., WorkflowRunResult] | None = None,
) -> None:
    """Resolve a workflow definition, run the iteration controller, render summary, exit.

    Args:
        name: Workflow definition name.
        max_attempts: Optional ``--max-attempts`` override (≥1).
        keep: When True, force ``auto_clean=False``; when False/None, leave default.
        approve_each: When set, override workflow approval.require_before_apply.
        wip: When True, overlay uncommitted working-tree changes into sandbox.
        dump_prompt: When True, dump provider-specific agent input to ``/tmp``.
        no_worktree: When True, run execution in-place without creating a Git worktree.
        cwd: Repository root.
        run_workflow_fn: Injectable controller (tests); defaults to ``run_workflow_iteration``.
    """
    root = (cwd or Path.cwd()).resolve()
    runner = run_workflow_fn or run_workflow_iteration

    if max_attempts is not None and max_attempts < 1:
        rich_output.error_panel(
            "Workflow Run Failed",
            "--max-attempts must be an integer >= 1.",
        )
        raise typer.Exit(code=1)

    load = load_config_result(cwd=root)
    if load.status == ConfigLoadStatus.NOT_FOUND:
        rich_output.error_panel(
            "Workflow Run Failed",
            load.errors[0]
            if load.errors
            else "Worktree is not initialized. Run `wt init`.",
        )
        raise typer.Exit(code=1)
    if not load.ok or load.config is None:
        detail = load.errors[0] if load.errors else "Invalid configuration."
        rich_output.error_panel("Workflow Run Failed", detail)
        raise typer.Exit(code=1)

    config = load.config
    resolved = resolve_workflow_by_name(name, cwd=root)
    if not resolved.ok:
        rich_output.error_panel(
            "Workflow Run Failed",
            format_workflow_show_resolve_failure(resolved),
        )
        raise typer.Exit(code=1)

    assert resolved.entry is not None
    validated = validate_workflow_result(resolved.entry.source_path)
    if not validated.ok:
        rich_output.error_panel(
            "Workflow Run Failed",
            format_workflow_show_validate_failure(validated),
        )
        raise typer.Exit(code=1)

    assert validated.workflow is not None
    workflow = validated.workflow

    auto_clean: bool | None = False if keep is True else None
    require_before_apply: bool | None = approve_each
    prompt_dump_dir = Path("/tmp") if dump_prompt else None

    attempt_holder: dict[str, int] = {"attempt": 1}
    streamed_progress = False

    def _print_plain(text: str) -> None:
        rich_output.console.print(
            text,
            end="",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    def on_event(event_name: str, payload: dict[str, Any]) -> None:
        nonlocal streamed_progress
        if event_name == "attempt_start":
            attempt_holder["attempt"] = int(payload.get("attempt", 1))
        line = format_progress_event(event_name, payload)
        if line is None:
            return
        streamed_progress = True
        _print_plain(line)

    approve_cb = None
    effective_require = (
        require_before_apply
        if require_before_apply is not None
        else workflow.approval.require_before_apply
    )
    if effective_require:
        approve_cb = _make_approve_callback(attempt_holder=attempt_holder)

    abort_event = threading.Event()
    result: WorkflowRunResult | None = None

    try:
        result = runner(
            workflow=workflow,
            cwd=root,
            config=config,
            caller_max_attempts=max_attempts,
            auto_clean=auto_clean,
            require_before_apply=require_before_apply,
            abort_event=abort_event,
            approve_patch=approve_cb,
            on_event=on_event,
            session_timeout_seconds=config.sandbox.default_timeout_seconds,
            detect_repeat_failures=config.workflow.detect_repeat_failures,
            include_wip=wip,
            prompt_dump_dir=prompt_dump_dir,
            use_git_worktree=False if no_worktree else None,
        )
    except KeyboardInterrupt:
        abort_event.set()
        if result is None:
            _print_plain("Interrupted.\n")
            raise typer.Exit(code=130) from None

    assert result is not None
    if result.errors and result.stop_reason in {
        StopReason.SANDBOX_CREATE_FAILED,
        StopReason.CONFIGURATION_ERROR,
    }:
        for err in result.errors:
            rich_output.error_panel("Workflow Run Failed", err)

    text = format_run_output(
        result,
        cwd=root,
        include_attempts=not streamed_progress,
    )
    if streamed_progress and text:
        _print_plain("\n")
    _print_plain(text)
    raise typer.Exit(code=exit_code_for_status(result.status))
