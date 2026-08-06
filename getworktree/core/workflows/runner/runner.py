"""Top-level workflow iteration orchestration: ``run_workflow_iteration``.

Resolves config/sandbox/agent setup, then runs the per-attempt dispatch workflow
against a shared ``_WorkflowContext`` (see ``steps.py``), delegating each stage
to a ``_run_*_step`` function and handling sandbox cleanup.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from getworktree.common.fs import get_session_dir
from getworktree.core.config.loader import load_config_result
from getworktree.core.config.models import WorktreeConfig
from getworktree.core.db import (
    RunStatus,
    insert_workflow_run,
    update_workflow_run_status,
)
from getworktree.core.git_sandbox import (
    GitSandboxManager,
    SandboxSession,
    should_cleanup_sandbox,
)
from getworktree.core.workflows.models import WorkflowDefinition
from getworktree.core.workflows.patch import apply_patch_result
from getworktree.core.workflows.payload import build_failure_payload
from getworktree.core.workflows.runner.helpers import (
    _emit,
    _now_iso,
    capture_and_persist_diff,
    default_list_changed_files,
    resolve_max_attempts,
)
from getworktree.core.workflows.runner.steps import (
    _run_agent_step,
    _run_approval_step,
    _run_patch_step,
    _run_trigger_step,
    _WorkflowContext,
)
from getworktree.core.workflows.runner_models import (
    ApplyPatchFn,
    ApprovePatchFn,
    AttemptRecord,
    BuildPayloadFn,
    CleanupSandboxFn,
    CreateSandboxFn,
    DiscardMutationFn,
    IsAbortedFn,
    ListChangedFilesFn,
    OnAttemptEndFn,
    OnEventFn,
    RunTriggerFn,
    StepOutcome,
    StopReason,
    WorkflowFinalStatus,
    WorkflowRunResult,
)
from getworktree.core.workflows.safety import SafetyState
from getworktree.core.workflows.trigger import run_trigger

if TYPE_CHECKING:
    from getworktree.core.workflows.agents.base import AgentAdapter


def run_workflow_iteration(
    *,
    workflow: WorkflowDefinition,
    cwd: Path | None = None,
    config: WorktreeConfig | None = None,
    caller_max_attempts: int | None = None,
    auto_clean: bool | None = None,
    keep_on_failure: bool | None = None,
    require_before_apply: bool | None = None,
    abort_event: threading.Event | None = None,
    is_aborted: IsAbortedFn | None = None,
    approve_patch: ApprovePatchFn | None = None,
    agent: AgentAdapter | None = None,
    list_changed_files: ListChangedFilesFn | None = None,
    run_trigger_fn: RunTriggerFn | None = None,
    apply_patch_fn: ApplyPatchFn | None = None,
    discard_mutation_fn: DiscardMutationFn | None = None,
    build_payload_fn: BuildPayloadFn | None = None,
    create_sandbox_fn: CreateSandboxFn | None = None,
    cleanup_sandbox_fn: CleanupSandboxFn | None = None,
    on_attempt_end: OnAttemptEndFn | None = None,
    on_event: OnEventFn | None = None,
    session_id: str | None = None,
    session_timeout_seconds: int | None = None,
    detect_repeat_failures: bool | None = None,
    include_wip: bool = False,
    prompt_dump_dir: Path | None = None,
    use_git_worktree: bool | None = None,
) -> WorkflowRunResult:
    """Run one full workflow session attempt cycle.

    Orchestrates sandbox create → trigger → (on failure) payload → agent →
    optional approval → patch apply → next attempt until a terminal stop.
    Each stage is a ``_run_*_step`` function dispatching on ``StepOutcome``
    against a shared ``_WorkflowContext``; this function resolves config/sandbox
    setup, then just runs the per-attempt dispatch workflow and cleanup.

    Args:
        workflow: Validated workflow definition.
        cwd: Repository root. Defaults to process CWD.
        config: Effective config; loaded from ``cwd`` when omitted.
        caller_max_attempts: Optional CLI override for attempt budget.
        auto_clean: Override workflow/config sandbox auto_clean when set.
        keep_on_failure: Override workflow/config keep_on_failure when set.
        require_before_apply: Override approval gate when set.
        abort_event: Cooperative abort flag checked between steps.
        is_aborted: Alternate abort predicate.
        approve_patch: Approval callback when require_before_apply is true.
        agent: Injected agent adapter; defaults to factory from workflow provider.
        list_changed_files: Callable returning sandbox-relative changed paths.
        run_trigger_fn: Injected trigger runner (tests).
        apply_patch_fn: Injected patch apply (tests).
        discard_mutation_fn: Injected sandbox reset for direct-mutation
            providers (tests); defaults to ``discard_since``.
        build_payload_fn: Injected payload builder (tests).
        create_sandbox_fn: Injected sandbox create (tests).
        cleanup_sandbox_fn: Injected sandbox cleanup (tests).
        on_attempt_end: Optional hook after each attempt record is finalized.
        on_event: Optional structured event callback for UX streaming.
        session_id: Optional fixed sandbox session id.
        session_timeout_seconds: Session wall-clock cap; defaults to
            ``config.sandbox.default_timeout_seconds``.
        detect_repeat_failures: Override config ``workflow.detect_repeat_failures``.
        include_wip: When True, overlay uncommitted working-tree changes into
            the sandbox after create (``--wip``).
        prompt_dump_dir: Optional directory to write provider-specific
            agent-input dumps (one file per attempt) before each agent call.
        use_git_worktree: Optional override to run in-place without creating a worktree.

    Returns:
        Structured :class:`WorkflowRunResult` (never raises for classified paths).
    """
    # Lazy import avoids circular dependency: agents.base → workflows.payload → workflows.
    from getworktree.core.workflows.agents.factory import get_agent_adapter
    from getworktree.core.workflows.agents.mutation_git import discard_since

    root = (cwd or Path.cwd()).expanduser().resolve()
    workflow_name = workflow.name
    empty_session = session_id or ""

    if config is None:
        load = load_config_result(cwd=root)
        if not load.ok or load.config is None:
            detail = load.errors[0] if load.errors else str(load.status)
            return WorkflowRunResult(
                status=WorkflowFinalStatus.FAILED,
                session_id=empty_session,
                workflow_name=workflow_name,
                stop_reason=StopReason.CONFIGURATION_ERROR,
                errors=[f"Workflow run configuration error: {detail}"],
            )
        config = load.config

    try:
        max_attempts = resolve_max_attempts(
            workflow=workflow,
            config=config,
            caller_max_attempts=caller_max_attempts,
        )
    except Exception as exc:  # defensive
        return WorkflowRunResult(
            status=WorkflowFinalStatus.FAILED,
            session_id=empty_session,
            workflow_name=workflow_name,
            stop_reason=StopReason.CONFIGURATION_ERROR,
            errors=[f"Workflow run configuration error: {exc}"],
        )

    if max_attempts < 1:
        return WorkflowRunResult(
            status=WorkflowFinalStatus.FAILED,
            session_id=empty_session,
            workflow_name=workflow_name,
            stop_reason=StopReason.CONFIGURATION_ERROR,
            max_attempts=max_attempts,
            errors=[
                "Workflow run configuration error: "
                f"effective max_attempts is {max_attempts} (must be >= 1)"
            ],
        )

    resolved_auto = (
        auto_clean if auto_clean is not None else workflow.sandbox.auto_clean
    )
    resolved_keep = (
        keep_on_failure
        if keep_on_failure is not None
        else workflow.sandbox.keep_on_failure
    )
    # Effective approval: explicit override, else workflow definition, else config.
    if require_before_apply is not None:
        resolved_require = require_before_apply
    else:
        resolved_require = workflow.approval.require_before_apply

    stop_when = set(workflow.iteration.stop_when)
    trigger_runner = run_trigger_fn or run_trigger
    patch_applier = apply_patch_fn or apply_patch_result
    mutation_discarder = discard_mutation_fn or discard_since
    payload_builder = build_payload_fn or build_failure_payload
    changed_files_fn = list_changed_files or default_list_changed_files

    resolved_use_worktree = (
        use_git_worktree
        if use_git_worktree is not None
        else workflow.sandbox.use_git_worktree
    )

    manager: GitSandboxManager | None = None
    session: SandboxSession | None = None

    if resolved_use_worktree:
        if create_sandbox_fn is not None:
            create_result = create_sandbox_fn()
        else:
            manager = GitSandboxManager(cwd=root)
            create_result = manager.create_sandbox_result(
                session_id=session_id,
                include_wip=include_wip,
            )

        if not create_result.ok or create_result.session is None:
            errors = list(create_result.errors) or [
                f"Sandbox create failed: {create_result.status}"
            ]
            return WorkflowRunResult(
                status=WorkflowFinalStatus.FAILED,
                session_id=session_id or "",
                workflow_name=workflow_name,
                stop_reason=StopReason.SANDBOX_CREATE_FAILED,
                max_attempts=max_attempts,
                errors=errors,
            )

        session = create_result.session
        sandbox_path = session.sandbox_path
        sid = session.session_id
    else:
        sid = session_id or f"wf_{uuid.uuid4().hex[:8]}"
        sandbox_path = root
        session = SandboxSession(
            session_id=sid,
            target_branch="-",
            sandbox_path=root,
            base_commit="HEAD",
            created_at=_now_iso(),
        )
    attempts: list[AttemptRecord] = []
    final_status = WorkflowFinalStatus.FAILED
    stop_reason = StopReason.MAX_ATTEMPTS_EXHAUSTED
    run_errors: list[str] = []
    warnings: list[str] = []
    command_passed: bool | None = None

    try:
        insert_workflow_run(
            session_id=sid,
            workflow_name=workflow_name,
            branch_name=session.target_branch,
            status=RunStatus.RUNNING,
            cwd=root,
        )
    except Exception as exc:
        warnings.append(f"Failed to record workflow run start in database: {exc}")

    def _record_db_status() -> None:
        run_status = (
            RunStatus.COMPLETED
            if final_status == WorkflowFinalStatus.PASSED
            else RunStatus.CANCELLED
            if final_status == WorkflowFinalStatus.ABORTED
            else RunStatus.FAILED
        )
        err_msg = (
            "; ".join(run_errors)
            if run_errors
            else (
                f"Stop reason: {stop_reason.value if hasattr(stop_reason, 'value') else stop_reason}"
                if run_status != RunStatus.COMPLETED
                else None
            )
        )
        try:
            update_workflow_run_status(
                session_id=sid,
                status=run_status,
                error_message=err_msg,
                cwd=root,
            )
        except Exception as exc:
            warnings.append(f"Failed to update workflow run status in database: {exc}")

    session_dir = get_session_dir(root, sid)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        warnings.append(f"Failed to create session directory '{session_dir}': {exc}")

    _emit(
        on_event,
        "session_start",
        session_id=sid,
        sandbox_path=str(sandbox_path),
        workflow_name=workflow_name,
        max_attempts=max_attempts,
        wip=session.wip_applied,
        wip_paths=list(session.wip_paths),
    )

    if agent is None:
        try:
            agent = get_agent_adapter(workflow.agent.provider, config=config.agent)
        except ValueError as exc:
            run_errors.append(str(exc))
            final_status = WorkflowFinalStatus.FAILED
            stop_reason = StopReason.CONFIGURATION_ERROR
            _finish_cleanup = True
        else:
            _finish_cleanup = False
    else:
        _finish_cleanup = False

    def _cleanup() -> None:
        nonlocal session
        if session is None:
            return
        session.command_passed = command_passed
        do_clean = should_cleanup_sandbox(
            auto_clean=resolved_auto,
            keep_on_failure=resolved_keep,
            command_passed=command_passed,
        )
        if not do_clean:
            return
        if cleanup_sandbox_fn is not None:
            cleanup_sandbox_fn(session)
        elif manager is not None:
            manager.cleanup_sandbox(session)

    if _finish_cleanup or agent is None:
        capture_and_persist_diff(session=session, cwd=root, warnings=warnings)
        _cleanup()
        _record_db_status()
        retained = not should_cleanup_sandbox(
            auto_clean=resolved_auto,
            keep_on_failure=resolved_keep,
            command_passed=command_passed,
        )
        return WorkflowRunResult(
            status=final_status,
            session_id=sid,
            workflow_name=workflow_name,
            sandbox_path=sandbox_path if retained else None,
            attempts=attempts,
            stop_reason=stop_reason,
            errors=run_errors,
            warnings=warnings,
            max_attempts=max_attempts,
            sandbox_retained=retained,
        )

    reject_binary = (
        workflow.patch.reject_binary_changes
        if workflow.patch.reject_binary_changes is not None
        else config.patch.reject_binary_changes
    )
    max_files = workflow.patch.max_files
    max_patch_kb = workflow.patch.max_patch_kb

    resolved_session_timeout = (
        session_timeout_seconds
        if session_timeout_seconds is not None
        else config.sandbox.default_timeout_seconds
    )
    resolved_detect_repeat = (
        detect_repeat_failures
        if detect_repeat_failures is not None
        else config.workflow.detect_repeat_failures
    )
    safety = SafetyState()

    ctx = _WorkflowContext(
        workflow=workflow,
        config=config,
        agent=agent,
        sandbox_path=sandbox_path,
        session_id=sid,
        max_attempts=max_attempts,
        stop_when=stop_when,
        resolved_require=resolved_require,
        reject_binary=reject_binary,
        max_files=max_files,
        max_patch_kb=max_patch_kb,
        resolved_detect_repeat=resolved_detect_repeat,
        resolved_session_timeout=resolved_session_timeout,
        trigger_runner=trigger_runner,
        patch_applier=patch_applier,
        mutation_discarder=mutation_discarder,
        payload_builder=payload_builder,
        changed_files_fn=changed_files_fn,
        approve_patch=approve_patch,
        abort_event=abort_event,
        is_aborted=is_aborted,
        on_event=on_event,
        on_attempt_end=on_attempt_end,
        prompt_dump_dir=prompt_dump_dir,
        attempts=attempts,
        run_errors=run_errors,
        safety=safety,
    )

    def _apply(outcome: StepOutcome) -> bool:
        """Assign a terminal outcome's fields; return whether to retry."""
        nonlocal final_status, stop_reason, command_passed
        if outcome.continue_workflow:
            return True
        final_status = outcome.final_status
        stop_reason = outcome.stop_reason
        command_passed = outcome.command_passed
        return False

    try:
        for attempt_idx in range(1, max_attempts + 1):
            if ctx.aborted():
                final_status = WorkflowFinalStatus.ABORTED
                stop_reason = StopReason.USER_ABORT
                break

            if ctx.timed_out():
                final_status = WorkflowFinalStatus.FAILED
                stop_reason = StopReason.SESSION_TIMEOUT
                command_passed = False
                break

            record = AttemptRecord(attempt=attempt_idx, started_at=_now_iso())
            _emit(
                on_event,
                "attempt_start",
                attempt=attempt_idx,
                max_attempts=max_attempts,
            )

            trigger_outcome, trigger_result = _run_trigger_step(
                ctx, attempt_idx, record
            )
            if trigger_outcome is not None:
                if _apply(trigger_outcome):
                    continue
                break
            assert trigger_result is not None  # narrows for type checkers

            agent_outcome, agent_response = _run_agent_step(
                ctx, attempt_idx, record, trigger_result=trigger_result
            )
            if agent_outcome is not None:
                if _apply(agent_outcome):
                    continue
                break
            assert agent_response is not None  # narrows for type checkers

            approval_outcome = _run_approval_step(
                ctx, attempt_idx, record, agent_response
            )
            if approval_outcome is not None:
                if _apply(approval_outcome):
                    continue
                break

            patch_outcome = _run_patch_step(ctx, attempt_idx, record, agent_response)
            if _apply(patch_outcome):
                continue
            break
        else:
            # for-workflow exhausted without break
            if final_status != WorkflowFinalStatus.PASSED:
                final_status = WorkflowFinalStatus.FAILED
                stop_reason = StopReason.MAX_ATTEMPTS_EXHAUSTED
                command_passed = False

    finally:
        if command_passed is None and final_status == WorkflowFinalStatus.PASSED:
            command_passed = True
        elif command_passed is None and final_status in {
            WorkflowFinalStatus.FAILED,
            WorkflowFinalStatus.UNFIXABLE,
        }:
            command_passed = False
        # ABORTED leaves command_passed as None (unclassified) per sandbox policy.

        session.command_passed = command_passed
        capture_and_persist_diff(session=session, cwd=root, warnings=warnings)
        _record_db_status()
        will_clean = (
            should_cleanup_sandbox(
                auto_clean=resolved_auto,
                keep_on_failure=resolved_keep,
                command_passed=command_passed,
            )
            if resolved_use_worktree
            else False
        )
        if will_clean:
            if cleanup_sandbox_fn is not None:
                cleanup_sandbox_fn(session)
            elif manager is not None:
                manager.cleanup_sandbox(session)
            retained = False
            result_sandbox: Path | None = None
        else:
            retained = True if resolved_use_worktree else False
            result_sandbox = sandbox_path

    return WorkflowRunResult(
        status=final_status,
        session_id=sid,
        workflow_name=workflow_name,
        sandbox_path=result_sandbox,
        attempts=attempts,
        stop_reason=stop_reason,
        errors=run_errors,
        warnings=warnings,
        max_attempts=max_attempts,
        sandbox_retained=retained,
    )
