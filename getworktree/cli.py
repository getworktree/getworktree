"""Typer CLI entrypoint for the Worktree (`wt`) command."""

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from getworktree.commands.catalog.command import (
    catalog_create_command,
    catalog_delete_command,
    catalog_list_command,
    catalog_show_command,
)
from getworktree.commands.config.command import (
    config_set_command,
    config_show_command,
    config_validate_command,
)
from getworktree.commands.init.command import init_command
from getworktree.commands.sandbox.command import (
    sandbox_create_command,
    sandbox_delete_command,
    sandbox_list_command,
    sandbox_show_command,
)
from getworktree.commands.status.command import status_command
from getworktree.commands.task.command import (
    task_list_command,
    task_run_command,
    task_show_command,
)
from getworktree.commands.templates.command import (
    template_show_command,
    templates_list_command,
)
from getworktree.commands.workflow.command import (
    workflow_list_command,
    workflow_resume_command,
    workflow_run_command,
    workflow_show_command,
)
from getworktree.core.db import SandboxStatus
from getworktree.core.templates.models import TemplateType

# Initialize a central styling console for high-utility layout parsing
console = Console()

# Package Metadata matching our PyPI footprint
__version__ = "0.1.1"

# Initialize Typer App with clean configuration defaults
app = typer.Typer(
    name="wt",
    help="Isolated git worktree developer workflows and autonomous AI agent workspaces.",
    add_completion=True,
    rich_markup_mode="rich",
)

config_app = typer.Typer(
    name="config",
    help="Inspect, update, and validate Worktree configuration.",
)
app.add_typer(config_app, name="config")

workflow_app = typer.Typer(
    name="workflow",
    help="Inspect and manage Worktree workflow definitions and sessions.",
    invoke_without_command=True,
)
app.add_typer(workflow_app, name="workflow")

sandbox_app = typer.Typer(
    name="sandbox",
    help="Inspect and manage git worktree sandboxes.",
)
app.add_typer(sandbox_app, name="sandbox")

template_app = typer.Typer(
    name="template",
    help="Inspect built-in Worktree template definitions.",
    invoke_without_command=True,
)
app.add_typer(template_app, name="template")

catalog_app = typer.Typer(
    name="catalog",
    help="Inspect, index, and manage executable blueprints in .worktree/catalog/.",
    invoke_without_command=True,
)
app.add_typer(catalog_app, name="catalog")

task_app = typer.Typer(
    name="task",
    help="Inspect and execute task blueprints.",
    invoke_without_command=True,
)
app.add_typer(task_app, name="task")


def print_welcome_banner():
    """Renders a highly scannable, developer-focused ASCII brand panel."""
    banner_text = Text()
    banner_text.append("🌳 Worktree CLI ", style="bold green")
    banner_text.append(f"v{__version__}\n", style="dim cyan")
    banner_text.append("Isolated Git Workspaces & Agent Workflows", style="italic dim")

    console.print(
        Panel(banner_text, border_style="green", expand=False, padding=(1, 4))
    )


def version_callback(value: bool):
    """Callback function to handle explicit version printing flags."""
    if value:
        console.print(f"[bold green]Worktree CLI[/bold green] v{__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable extensive internal engineering telemetry logging.",
    ),
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Print the current version of the Worktree CLI and exit.",
    ),
):
    """Global configuration wrapper managing shared application context."""
    # Stash verbose settings inside the runtime context dict for downstream commands
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

    # If the developer types just 'wt' without a subcommand, render banner and help
    if ctx.invoked_subcommand is None:
        print_welcome_banner()
        console.print(ctx.get_help())
        raise typer.Exit()
    elif verbose:
        console.print(
            "[dim yellow][TELEMETRY] Global verbose tracking layer active.[/dim yellow]"
        )


@app.command(name="init")
def init_workspace(
    ctx: typer.Context,
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing config with fresh V1 defaults (destructive).",
    ),
    repair: bool = typer.Option(
        False,
        "--repair",
        help="Add missing required config keys without overwriting user values.",
    ),
):
    """Provision a secure local hidden folder path and tracking schemas."""
    init_command(
        tool_version=__version__,
        overwrite=overwrite,
        repair=repair,
    )


@app.command(name="status")
def workspace_status(ctx: typer.Context):
    """Workspace Status."""
    status_command()


@config_app.command("show")
def config_show(ctx: typer.Context):
    """Display the full normalized effective configuration as JSON."""
    config_show_command()


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ...,
        help="Config key or nested dot-path (e.g. agent.model).",
    ),
    value: str = typer.Argument(
        ...,
        help="Value to store (string; typed parsing is separate).",
    ),
):
    """Set a configuration value by key or nested dot-path."""
    config_set_command(key, value)


@config_app.command("validate")
def config_validate(ctx: typer.Context):
    """Validate .worktree/config.json against the V1 schema and semantic rules."""
    config_validate_command()


@workflow_app.callback(invoke_without_command=True)
def workflow_callback(ctx: typer.Context):
    """Inspect and manage Worktree workflow definitions and sessions."""
    if ctx.invoked_subcommand is None:
        workflow_list_command()


@workflow_app.command("list")
def workflow_list(ctx: typer.Context):
    """List workflow run sessions."""
    workflow_list_command()


@workflow_app.command("show")
def workflow_show(
    id: str = typer.Argument(..., help="Workflow session ID to show."),
):
    """Show details for a specific workflow session."""
    workflow_show_command(id)


@workflow_app.command("run")
def workflow_run(
    name: str = typer.Argument(..., help="Logical workflow name to run."),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        help="Override effective max attempts (>= 1).",
        min=1,
    ),
    keep: bool = typer.Option(
        False,
        "--keep/--no-keep",
        help="When --keep, force retain the sandbox (auto_clean=False).",
    ),
    approve_each: bool | None = typer.Option(
        None,
        "--approve-each/--no-approve-each",
        help="Require (or skip) approval before each patch apply.",
    ),
    wip: bool = typer.Option(
        False,
        "--wip/--no-wip",
        help=(
            "Include uncommitted working-tree changes in the sandbox "
            "(tracked + untracked; not ignored)."
        ),
    ),
    dump_prompt: bool = typer.Option(
        False,
        "--dump-prompt/--no-dump-prompt",
        help=(
            "Dump provider-specific agent input to /tmp before each agent call "
            "(debugging aid)."
        ),
    ),
    no_worktree: bool = typer.Option(
        False,
        "--no-worktree",
        help="Run execution in-place in the working tree without creating a Git worktree.",
    ),
):
    """Run a workflow in an isolated git worktree sandbox."""
    workflow_run_command(
        name,
        max_attempts=max_attempts,
        keep=keep if keep else None,
        approve_each=approve_each,
        wip=wip,
        dump_prompt=dump_prompt,
        no_worktree=no_worktree,
    )


@workflow_app.command("resume")
def workflow_resume(
    id: str = typer.Argument(..., help="Workflow session ID to resume."),
):
    """Resume an interrupted workflow session."""
    workflow_resume_command(id)


_SANDBOX_STATUS_OPTION = typer.Option(
    None,
    "--status",
    help="Filter by lifecycle status (active, merged, cleaned, conflict).",
    case_sensitive=False,
)


@sandbox_app.command("create")
def sandbox_create(
    name: str | None = typer.Option(
        None,
        "--name",
        help="Optional human-readable name for the sandbox.",
    ),
    base_ref: str | None = typer.Option(
        None,
        "--base-ref",
        help=(
            "Git ref to base the sandbox on. When omitted, uses the current "
            "branch or config sandbox.base_ref."
        ),
    ),
    wip: bool = typer.Option(
        False,
        "--wip/--no-wip",
        help=(
            "Include uncommitted working-tree changes in the sandbox "
            "(tracked + untracked; not ignored)."
        ),
    ),
):
    """Create an isolated git worktree sandbox."""
    sandbox_create_command(name=name, base_ref=base_ref, wip=wip)


@sandbox_app.command("list")
def sandbox_list(
    status: SandboxStatus | None = _SANDBOX_STATUS_OPTION,
):
    """List tracked sandboxes and their lifecycle status."""
    sandbox_list_command(status=status.value if status is not None else None)


@sandbox_app.command("show")
def sandbox_show(
    sandbox_id: str = typer.Argument(..., help="Sandbox id to show."),
):
    """Show full detail for one tracked sandbox."""
    sandbox_show_command(sandbox_id)


@sandbox_app.command("delete")
def sandbox_delete(
    sandbox_id: str = typer.Argument(..., help="Sandbox id to delete."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the confirmation prompt and delete immediately.",
    ),
):
    """Delete a sandbox worktree and branch after confirmation."""
    sandbox_delete_command(sandbox_id, force=force)


_TEMPLATE_TYPE_OPTION = typer.Option(
    None,
    "--type",
    help="Filter built-in templates by type (workflow, task, step).",
)


@template_app.callback(invoke_without_command=True)
def template_callback(
    ctx: typer.Context,
    type: TemplateType | None = _TEMPLATE_TYPE_OPTION,
):
    """Inspect built-in Worktree template definitions."""
    if ctx.invoked_subcommand is None:
        templates_list_command(type_filter=type)


@template_app.command("list")
def template_list(
    ctx: typer.Context,
    type: TemplateType | None = _TEMPLATE_TYPE_OPTION,
):
    """List wt-defined built-in templates."""
    templates_list_command(type_filter=type)


@template_app.command("show")
def template_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Template name to show."),
    type: TemplateType | None = _TEMPLATE_TYPE_OPTION,
):
    """Show metadata and definition content of a specific built-in template."""
    outcome = template_show_command(name, type_filter=type)
    if not outcome.ok:
        raise typer.Exit(code=1)


_CATALOG_TYPE_OPTION = typer.Option(
    None,
    "--type",
    help="Filter catalog blueprints by type (workflow, task, step).",
)


@catalog_app.callback(invoke_without_command=True)
def catalog_callback(
    ctx: typer.Context,
    type: str | None = _CATALOG_TYPE_OPTION,
):
    """Inspect and manage executable blueprints in .worktree/catalog/."""
    if ctx.invoked_subcommand is None:
        outcome = catalog_list_command(type_filter=type)
        if not outcome.ok:
            raise typer.Exit(code=1)


@catalog_app.command("list")
def catalog_list(
    ctx: typer.Context,
    type: str | None = _CATALOG_TYPE_OPTION,
):
    """List catalog blueprints."""
    outcome = catalog_list_command(type_filter=type)
    if not outcome.ok:
        raise typer.Exit(code=1)


@catalog_app.command("create")
def catalog_create(
    ctx: typer.Context,
    type: str = typer.Argument(..., help="Blueprint item type (workflow, task, step)."),
    name: str = typer.Option(
        ..., "--name", help="Name for the catalog blueprint file."
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        help="Optional built-in template name to populate content from.",
    ),
):
    """Create a new catalog blueprint under .worktree/catalog/<type>s/<name>.yml."""
    outcome = catalog_create_command(item_type=type, name=name, template=template)
    if not outcome.ok:
        raise typer.Exit(code=1)


@catalog_app.command("show")
def catalog_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Catalog blueprint SHA or name to show."),
):
    """Show metadata and definition content of a catalog blueprint."""
    outcome = catalog_show_command(sha_or_name=name)
    if not outcome.ok:
        raise typer.Exit(code=1)


@catalog_app.command("delete")
def catalog_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Catalog blueprint SHA or name to delete."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip deletion confirmation prompt.",
    ),
):
    """Delete a catalog blueprint file and its database index record."""
    if not force:
        confirm = typer.confirm(
            f"Are you sure you want to delete catalog blueprint '{name}'?"
        )
        if not confirm:
            console.print("Deletion cancelled.")
            raise typer.Exit()

    outcome = catalog_delete_command(sha_or_name=name)
    if not outcome.ok:
        raise typer.Exit(code=1)


@task_app.callback(invoke_without_command=True)
def task_callback(ctx: typer.Context):
    """Inspect and execute task blueprints."""
    if ctx.invoked_subcommand is None:
        outcome = task_list_command()
        if not outcome.ok:
            raise typer.Exit(code=1)


@task_app.command("list")
def task_list(ctx: typer.Context):
    """List available task blueprints."""
    outcome = task_list_command()
    if not outcome.ok:
        raise typer.Exit(code=1)


@task_app.command("show")
def task_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Task blueprint name or SHA to show."),
):
    """Show metadata and definition content of a task blueprint."""
    outcome = task_show_command(name=name)
    if not outcome.ok:
        raise typer.Exit(code=1)


@task_app.command("run")
def task_run(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Task blueprint name to run."),
    no_worktree: bool = typer.Option(
        False,
        "--no-worktree",
        help="Run execution in-place in the working tree without creating a Git worktree.",
    ),
):
    """Run a task blueprint."""
    outcome = task_run_command(name=name, no_worktree=no_worktree)
    if not outcome.ok:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
