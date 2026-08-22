"""Class-based execution service for blueprint resume commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from worktree.common.utils import RichOutput
from worktree.core.blueprint.models import (
    BlueprintKind,
    BlueprintRunCommandOutcome,
)
from worktree.core.blueprint.renderers import render_blueprint_run_success
from worktree.core.db import RunRecord, RunsRepository, RunStatus
from worktree.core.engine import Engine, EngineResumeError, EngineRuntimeError
from worktree.core.runtime import (
    CliFailurePrompter,
    FailurePrompter,
    RunOutcome,
    resolve_run_observer,
)


@dataclass
class BlueprintResumeService:
    """Service encapsulating the paused session resume lifecycle."""

    session_id: str | None = None
    cwd: Path | None = None
    non_interactive: bool = False
    output: RichOutput = field(default_factory=RichOutput)

    root: Path = field(init=False)
    db: RunsRepository = field(init=False)

    def __post_init__(self) -> None:
        self.root = (self.cwd or Path.cwd()).resolve()
        self.db = RunsRepository(self.root)

    def execute(self) -> BlueprintRunCommandOutcome:
        """Find session if omitted, classify and resume via Engine."""
        target_session_id, target_kind, resolve_error = self._resolve_target_session()
        if resolve_error is not None or not target_session_id:
            return self._fail(resolve_error or "No paused session found to resume.")

        effective_non_interactive, prompter = self._resolve_prompter(target_kind)
        observer = resolve_run_observer(self.output, non_interactive=effective_non_interactive)

        try:
            with observer:
                run_outcome = Engine(self.root).resume(
                    target_session_id,
                    observer=observer,
                    failure_prompter=prompter,
                    non_interactive=effective_non_interactive,
                )
        except (EngineResumeError, EngineRuntimeError) as exc:
            return self._fail(str(exc))

        for warning in run_outcome.warnings:
            self.output.info(warning)

        return self._finalize(target_session_id, run_outcome)

    def _resolve_target_session(self) -> tuple[str, BlueprintKind | None, str | None]:
        if not self.session_id:
            record = self.db.get_latest_paused()
            if record is None:
                return "", None, "No paused session found to resume."
            self.output.info(f"Resuming latest paused session '{record.session_id}' ({record.blueprint_name})...")
            return record.session_id, record.kind, None

        self.output.info(f"Resuming session '{self.session_id}'...")
        record = self._load_record(self.session_id)
        target_kind = record.kind if record is not None else None
        return self.session_id, target_kind, None

    def _fail(self, message: str) -> BlueprintRunCommandOutcome:
        self.output.error_panel("Resume Failed", message)
        return BlueprintRunCommandOutcome(run_record=None, errors=[message])

    def _resolve_prompter(self, kind: BlueprintKind | None = None) -> tuple[bool, FailurePrompter | None]:
        if self.non_interactive:
            return True, None
        prompter_kind = "workflow" if kind == BlueprintKind.WORKFLOW else "task"
        prompter = CliFailurePrompter(self.output, kind=prompter_kind)
        if not prompter.is_interactive:
            return True, None
        return False, prompter

    def _load_record(self, session_id: str) -> RunRecord | None:
        try:
            return self.db.get(session_id)
        except Exception:
            return None

    def _render_success(self, record: RunRecord | None) -> None:
        if record is not None:
            render_blueprint_run_success(record, record.kind, rich_output=self.output)

    def _finalize(self, session_id: str, run_outcome: RunOutcome) -> BlueprintRunCommandOutcome:
        record = self._load_record(session_id)
        warnings = list(run_outcome.warnings)

        if run_outcome.ok:
            self._render_success(record)
            return BlueprintRunCommandOutcome(run_record=record, warnings=warnings)

        if run_outcome.status == RunStatus.PAUSED:
            msg = run_outcome.error_message or "Run paused; checkpoint saved."
            self.output.info(msg)
            return BlueprintRunCommandOutcome(run_record=record, warnings=warnings)

        if run_outcome.status == RunStatus.CANCELLED:
            msg = run_outcome.error_message or "Cancelled by user."
            self.output.error_panel("Resume Cancelled", msg)
            return BlueprintRunCommandOutcome(
                run_record=record,
                errors=[msg],
                warnings=warnings,
            )

        msg = run_outcome.error_message or f"Cannot resume session '{session_id}'."
        self.output.error_panel("Resume Failed", msg)
        return BlueprintRunCommandOutcome(
            run_record=record,
            errors=[msg],
            warnings=warnings,
        )
