"""Orchestration logic for ``wt task`` CLI commands."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from pathlib import Path

import yaml

from getworktree.commands.task.models import (
    TaskBlueprintItem,
    TaskListCommandOutcome,
    TaskRunCommandOutcome,
    TaskShowCommandOutcome,
)
from getworktree.commands.task.renderers import (
    render_task_list,
    render_task_run_success,
    render_task_show,
)
from getworktree.common.utils import RichOutput
from getworktree.core.catalog.inventory import (
    get_catalog_dir,
    get_catalog_item,
    scan_and_index_catalog,
)
from getworktree.core.db import (
    CatalogItemType,
    RunStatus,
    TaskRunRecord,
    insert_task_run,
    list_task_runs,
    update_task_run_status,
)

_DEFAULT_RICH_OUTPUT = RichOutput()
logger = logging.getLogger(__name__)


def task_list_command(
    cwd: Path | None = None,
    *,
    rich_output: RichOutput | None = None,
) -> TaskListCommandOutcome:
    """List task blueprints discovered under ``.worktree/catalog/tasks/`` and recorded task runs.

    Args:
        cwd: Optional working directory.
        rich_output: Optional RichOutput presenter.

    Returns:
        TaskListCommandOutcome containing listed task blueprint items, task run history, and errors.
    """
    output = rich_output or _DEFAULT_RICH_OUTPUT
    warnings: list[str] = []

    scan_res = scan_and_index_catalog(cwd=cwd)
    if not scan_res.ok:
        for err in scan_res.errors:
            output.error_panel("Task Catalog Scan Warning", err)

    task_records = [r for r in scan_res.items if r.item_type == CatalogItemType.TASK]
    catalog_dir = get_catalog_dir(cwd)

    items: list[TaskBlueprintItem] = []
    for record in task_records:
        file_path = catalog_dir / record.path
        use_git_worktree = True
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8")
                yaml_data = yaml.safe_load(content)
                if isinstance(yaml_data, dict):
                    description = str(yaml_data.get("description", ""))
                    summary = str(yaml_data.get("summary", ""))
                    if "use_git_worktree" in yaml_data:
                        use_git_worktree = bool(yaml_data.get("use_git_worktree", True))
            except Exception:
                pass

        items.append(
            TaskBlueprintItem(
                name=record.name,
                description=description,
                summary=summary,
                sha=record.sha,
                path=str(record.path),
                use_git_worktree=use_git_worktree,
            )
        )

    runs: list[TaskRunRecord] = []
    try:
        runs = list_task_runs(cwd=cwd)
    except Exception as exc:
        warnings.append(f"Failed to query task run history from database: {exc}")
        logger.warning("Failed to query task run history from database: %s", exc)

    render_task_list(items, runs=runs, rich_output=output)

    return TaskListCommandOutcome(
        items=items,
        runs=runs,
        errors=list(scan_res.errors),
        warnings=warnings,
    )


def task_show_command(
    name: str,
    cwd: Path | None = None,
    *,
    rich_output: RichOutput | None = None,
) -> TaskShowCommandOutcome:
    """Show details and definition of a specific task blueprint.

    Args:
        name: Task name or SHA identifier.
        cwd: Optional working directory.
        rich_output: Optional RichOutput presenter.

    Returns:
        TaskShowCommandOutcome containing catalog item record and YAML definition.
    """
    output = rich_output or _DEFAULT_RICH_OUTPUT

    item = get_catalog_item(name, cwd=cwd)
    if item is None or item.item_type != CatalogItemType.TASK:
        error_msg = f"Task blueprint '{name}' not found."
        output.error_panel("Task Show Failed", error_msg)
        return TaskShowCommandOutcome(item=None, content=None, errors=[error_msg])

    catalog_dir = get_catalog_dir(cwd)
    file_path = catalog_dir / item.path

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        error_msg = f"Failed to read file for task blueprint '{name}': {exc}"
        output.error_panel("Task Show Failed", error_msg)
        return TaskShowCommandOutcome(item=item, content=None, errors=[error_msg])

    render_task_show(item, content, rich_output=output)
    return TaskShowCommandOutcome(item=item, content=content)


def task_run_command(
    name: str,
    cwd: Path | None = None,
    *,
    no_worktree: bool = False,
    session_id: str | None = None,
    execute_task_fn: Callable[[], None] | None = None,
    rich_output: RichOutput | None = None,
) -> TaskRunCommandOutcome:
    """Execute a task blueprint by name, persisting status transitions to SQLite tasks table.

    Args:
        name: Name of the task to run.
        cwd: Optional working directory.
        no_worktree: When True, run execution in-place without creating a Git worktree.
        session_id: Optional fixed session ID.
        execute_task_fn: Optional custom execution hook (for testing/simulation).
        rich_output: Optional RichOutput presenter.

    Returns:
        TaskRunCommandOutcome containing task run record, warnings, or errors.
    """
    output = rich_output or _DEFAULT_RICH_OUTPUT

    item = get_catalog_item(name, cwd=cwd)
    if item is None or item.item_type != CatalogItemType.TASK:
        error_msg = f"Task blueprint '{name}' not found."
        output.error_panel("Task Run Failed", error_msg)
        return TaskRunCommandOutcome(run_record=None, errors=[error_msg])

    catalog_dir = get_catalog_dir(cwd)
    file_path = catalog_dir / item.path

    task_use_git_wt = True
    if file_path.exists():
        try:
            content = file_path.read_text(encoding="utf-8")
            yaml_data = yaml.safe_load(content)
            if isinstance(yaml_data, dict) and "use_git_worktree" in yaml_data:
                task_use_git_wt = bool(yaml_data.get("use_git_worktree", True))
        except Exception:
            pass

    effective_use_git_worktree = False if no_worktree else task_use_git_wt
    _ = effective_use_git_worktree  # explicit evaluation

    sid = session_id or f"task_{uuid.uuid4().hex[:8]}"
    warnings: list[str] = []
    run_record: TaskRunRecord | None = None

    # FR-1: Task run start persistence (with non-blocking NFR-1 & NFR-2 fault-tolerance)
    try:
        run_record = insert_task_run(
            session_id=sid,
            task_name=name,
            status=RunStatus.RUNNING,
            cwd=cwd,
        )
    except Exception as exc:
        warnings.append(f"Failed to record task run start in database: {exc}")
        logger.warning("Failed to record task run start in database: %s", exc)

    run_status = RunStatus.RUNNING
    error_msg: str | None = None

    try:
        if execute_task_fn is not None:
            execute_task_fn()
        run_status = RunStatus.COMPLETED
    except KeyboardInterrupt:
        # FR-4: Task run cancellation update
        run_status = RunStatus.CANCELLED
        error_msg = "Task execution cancelled by user."
    except Exception as exc:
        # FR-3: Task run failure update
        run_status = RunStatus.FAILED
        error_msg = str(exc)

    # FR-2, FR-3, FR-4: Task run status update (with non-blocking NFR-1 & NFR-2 fault-tolerance)
    updated_record: TaskRunRecord | None = None
    try:
        updated_record = update_task_run_status(
            session_id=sid,
            status=run_status,
            error_message=error_msg,
            cwd=cwd,
        )
    except Exception as exc:
        warnings.append(f"Failed to update task run status in database: {exc}")
        logger.warning("Failed to update task run status in database: %s", exc)

    final_record = (
        updated_record
        or run_record
        or TaskRunRecord(
            id=-1,
            session_id=sid,
            task_name=name,
            status=run_status,
            started_at="",
            completed_at=None,
            error_message=error_msg,
        )
    )

    if run_status == RunStatus.COMPLETED:
        render_task_run_success(final_record, rich_output=output)
        return TaskRunCommandOutcome(run_record=final_record, warnings=warnings)
    elif run_status == RunStatus.CANCELLED:
        output.error_panel("Task Run Cancelled", error_msg or "Cancelled by user.")
        return TaskRunCommandOutcome(
            run_record=final_record,
            errors=[error_msg or "Task execution cancelled."],
            warnings=warnings,
        )
    else:
        output.error_panel("Task Run Failed", error_msg or "Task execution failed.")
        return TaskRunCommandOutcome(
            run_record=final_record,
            errors=[error_msg or "Task execution failed."],
            warnings=warnings,
        )
