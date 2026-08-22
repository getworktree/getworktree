"""Class-based execution service for blueprint run commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from worktree.common.utils import RichOutput
from worktree.core.blueprint.exceptions import (
    BlueprintLoadError,
    BlueprintNotFoundError,
    BlueprintValidationError,
)
from worktree.core.blueprint.models import (
    BlueprintKind,
    BlueprintRunCommandOutcome,
)
from worktree.core.blueprint.renderers import BlueprintRenderer, render_blueprint_run_success
from worktree.core.blueprint.services.blueprint import Blueprint
from worktree.core.catalog import Catalog
from worktree.core.db import RunRecord, RunsRepository, RunStatus
from worktree.core.engine import Engine, EngineInputError, EngineRuntimeError, RunRequest
from worktree.core.inputs import format_input_error_message
from worktree.core.runtime import (
    CliFailurePrompter,
    FailurePrompter,
    RunOutcome,
    resolve_run_observer,
)


@dataclass
class BlueprintRunService:
    """Service encapsulating the blueprint execution lifecycle."""

    name: str
    kind: BlueprintKind | None = None
    cwd: Path | None = None
    no_sandbox: bool = False
    keep: bool = False
    agent: str | None = None
    session_id: str | None = None
    cli_args: list[str] | None = None
    non_interactive: bool = False
    output: RichOutput = field(default_factory=RichOutput)

    root: Path = field(init=False)
    db: RunsRepository = field(init=False)
    renderer: BlueprintRenderer = field(init=False)

    def __post_init__(self) -> None:
        self.root = (self.cwd or Path.cwd()).resolve()
        self.db = RunsRepository(self.root)
        self.renderer = BlueprintRenderer(self.kind or BlueprintKind.TASK)

    @property
    def _kind_label(self) -> str:
        return self.kind.value if self.kind is not None else "blueprint"

    def execute(self) -> BlueprintRunCommandOutcome:
        """Run the full execution pipeline and return the outcome."""
        blueprint, fail_outcome = self._load_blueprint()
        if fail_outcome is not None or blueprint is None:
            return fail_outcome or self._fail(f"Failed to load {self._kind_label} '{self.name}'.")

        if self.kind is None:
            self.kind = blueprint.kind
            self.renderer = BlueprintRenderer(self.kind)

        effective_non_interactive, prompter = self._resolve_prompter()
        self.output.info(f"Running {self._kind_label} '{self.name}'...")
        observer = resolve_run_observer(self.output, non_interactive=effective_non_interactive)

        try:
            with observer:
                run_outcome = Engine(self.root).run(
                    blueprint,
                    RunRequest(
                        cli_args=self.cli_args,
                        use_sandbox=not self.no_sandbox,
                        keep=self.keep,
                        agent=self.agent,
                        session_id=self.session_id,
                        observer=observer,
                        failure_prompter=prompter,
                        non_interactive=effective_non_interactive,
                    ),
                )
        except EngineInputError as exc:
            return self._fail(
                format_input_error_message(
                    kind=self._kind_label,
                    name=self.name,
                    result=exc.result,
                    declarations=blueprint.inputs,
                )
            )
        except EngineRuntimeError as exc:
            return self._fail(str(exc))

        for warning in run_outcome.warnings:
            self.output.info(warning)

        return self._finalize(run_outcome)

    def _fail(self, message: str) -> BlueprintRunCommandOutcome:
        panel_title = f"{self._kind_label.capitalize()} Run Failed"
        self.output.error_panel(panel_title, message)
        return BlueprintRunCommandOutcome(run_record=None, errors=[message])

    def _load_blueprint(self) -> tuple[Blueprint | None, BlueprintRunCommandOutcome | None]:
        catalog = Catalog(self.root)
        try:
            blueprint = Blueprint.load(self.name, catalog=catalog)
        except (BlueprintNotFoundError, BlueprintLoadError) as exc:
            return None, self._fail(self.renderer.render_resolve_failure([str(exc)]))
        except BlueprintValidationError as exc:
            return None, self._fail(self.renderer.render_validate_failure([str(exc)]))

        if self.kind is not None and blueprint.kind is not self.kind:
            msg = (
                f"Blueprint '{self.name}' is a {blueprint.kind.value}; "
                f"wt {self.kind.value} run requires a {self.kind.value}."
            )
            return None, self._fail(msg)

        return blueprint, None

    def _resolve_prompter(self) -> tuple[bool, FailurePrompter | None]:
        if self.non_interactive:
            return True, None
        prompter_kind = "workflow" if self.kind == BlueprintKind.WORKFLOW else "task"
        prompter = CliFailurePrompter(self.output, kind=prompter_kind)
        if not prompter.is_interactive:
            return True, None
        return False, prompter

    def _load_record(self, session_id: str) -> RunRecord | None:
        try:
            return self.db.get(session_id)
        except Exception:
            return None

    def _fallback_record(
        self,
        session_id: str,
        status: RunStatus,
        error: str | None,
    ) -> RunRecord:
        return RunRecord(
            id=-1,
            session_id=session_id,
            blueprint_name=self.name,
            kind=self.kind or BlueprintKind.TASK,
            branch_name="",
            status=status,
            started_at="",
            completed_at=None,
            error_message=error,
        )

    def _render_success(self, final_record: RunRecord) -> None:
        render_blueprint_run_success(final_record, self.kind, rich_output=self.output)

    def _finalize(self, run_outcome: RunOutcome) -> BlueprintRunCommandOutcome:
        sid = run_outcome.session_id or ""
        record = self._load_record(sid) if sid else None
        final_record = record or self._fallback_record(
            sid,
            run_outcome.status,
            run_outcome.error_message,
        )
        warnings = list(run_outcome.warnings)

        if run_outcome.ok:
            self._render_success(final_record)
            return BlueprintRunCommandOutcome(run_record=final_record, warnings=warnings)

        if run_outcome.status == RunStatus.PAUSED:
            msg = run_outcome.error_message or f"{self._kind_label.capitalize()} paused; checkpoint saved."
            self.output.info(msg)
            return BlueprintRunCommandOutcome(run_record=final_record, warnings=warnings)

        if run_outcome.status == RunStatus.CANCELLED:
            msg = run_outcome.error_message or "Cancelled by user."
            self.output.error_panel(f"{self._kind_label.capitalize()} Run Cancelled", msg)
            return BlueprintRunCommandOutcome(
                run_record=final_record,
                errors=[msg],
                warnings=warnings,
            )

        msg = self.renderer.render(run_outcome)
        self.output.error_panel(f"{self._kind_label.capitalize()} Run Failed", msg)
        return BlueprintRunCommandOutcome(
            run_record=final_record,
            errors=[msg],
            warnings=warnings,
        )
