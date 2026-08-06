"""Outcome models for task CLI commands."""

from __future__ import annotations

from pydantic import BaseModel, Field

from getworktree.core.db import CatalogRecord, TaskRunRecord


class TaskBlueprintItem(BaseModel):
    """View model for a task catalog blueprint."""

    model_config = {"extra": "forbid", "strict": True}

    name: str
    description: str
    summary: str
    sha: str
    path: str
    use_git_worktree: bool = True


class TaskListCommandOutcome(BaseModel):
    """Outcome for ``wt task list`` (or default ``wt task``)."""

    model_config = {"extra": "forbid", "strict": True}

    items: list[TaskBlueprintItem] = Field(default_factory=list)
    runs: list[TaskRunRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True if no errors were encountered."""
        return len(self.errors) == 0


class TaskShowCommandOutcome(BaseModel):
    """Outcome for ``wt task show``."""

    model_config = {"extra": "forbid", "strict": True}

    item: CatalogRecord | None = None
    content: str | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True if item exists and no errors occurred."""
        return self.item is not None and len(self.errors) == 0


class TaskRunCommandOutcome(BaseModel):
    """Outcome for ``wt task run``."""

    model_config = {"extra": "forbid", "strict": True}

    run_record: TaskRunRecord | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True if task run executed without errors."""
        return self.run_record is not None and len(self.errors) == 0
