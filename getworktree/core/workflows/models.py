"""Pydantic models for full workflow definition V1."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

WorkflowAgentMode = Literal["fix_failure", "review_remediation"]
WorkflowAgentProvider = Literal["local", "ollama", "cursor", "gemini", "copilot"]
WorkflowContextInclude = Literal["trigger_output", "changed_files", "relevant_source"]
WorkflowPatchStrategy = Literal["unified_diff"]
WorkflowStopWhen = Literal["trigger_passes", "unfixable", "user_abort"]
WorkflowStepType = Literal["command", "agent", "script"]
WorkflowStepFailureAction = Literal["abort", "ignore", "retry"]


class StepReference(BaseModel):
    """Reference to a pre-defined step template in .worktree/templates/steps/."""

    model_config = {"extra": "forbid", "strict": True}

    step_id: str = Field(min_length=1)
    override_timeout_seconds: int | None = Field(default=None, ge=1)


class InlineStepDefinition(BaseModel):
    """Inline command, agent, or script step defined directly inside a workflow."""

    model_config = {"extra": "forbid", "strict": True}

    name: str = Field(min_length=1)
    type: WorkflowStepType
    description: str | None = None
    command: str | None = None
    args: list[str] | None = None
    prompt: str | None = None
    agent: str | None = None
    tools: list[str] = Field(default_factory=list)
    script_path: str | None = None
    timeout_seconds: int = Field(default=120, ge=1)
    failure_action: WorkflowStepFailureAction = Field(default="abort")


class WorkflowTrigger(BaseModel):
    """Trigger command settings for a workflow definition."""

    model_config = {"extra": "forbid", "strict": True}

    command: str = Field(min_length=1)
    args: list[str]
    timeout_seconds: int = Field(ge=1)


class WorkflowAgent(BaseModel):
    """Agent provider settings for a workflow definition."""

    model_config = {"extra": "forbid", "strict": True}

    provider: WorkflowAgentProvider
    mode: WorkflowAgentMode
    timeout_seconds: int = Field(ge=1)


class WorkflowIteration(BaseModel):
    """Iteration limits and stop conditions."""

    model_config = {"extra": "forbid", "strict": True}

    max_attempts: int = Field(ge=1)
    stop_when: list[WorkflowStopWhen] = Field(min_length=1)


class WorkflowSandbox(BaseModel):
    """Sandbox lifecycle settings for one workflow."""

    model_config = {"extra": "forbid", "strict": True}

    auto_clean: bool
    keep_on_failure: bool
    use_git_worktree: bool = Field(default=True)


class WorkflowApproval(BaseModel):
    """Approval gate before applying workflow patches."""

    model_config = {"extra": "forbid", "strict": True}

    require_before_apply: bool


class WorkflowContext(BaseModel):
    """Context payloads included for the agent."""

    model_config = {"extra": "forbid", "strict": True}

    include: list[WorkflowContextInclude] = Field(min_length=1)


class WorkflowPatch(BaseModel):
    """Patch strategy and size limits."""

    model_config = {"extra": "forbid", "strict": True}

    strategy: WorkflowPatchStrategy
    max_files: int = Field(ge=1)
    max_patch_kb: int = Field(ge=1)
    reject_binary_changes: bool | None = None


class WorkflowDefinition(BaseModel):
    """Full workflow definition V1 surface from ``workflow_v1.json``."""

    model_config = {"extra": "forbid", "strict": True}

    version: Literal[1]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    trigger: WorkflowTrigger | None = None
    agent: WorkflowAgent | None = None
    steps: list[StepReference | InlineStepDefinition] | None = None
    iteration: WorkflowIteration
    sandbox: WorkflowSandbox
    approval: WorkflowApproval
    context: WorkflowContext
    patch: WorkflowPatch

    @model_validator(mode="after")
    def validate_steps_or_trigger_agent(self) -> WorkflowDefinition:
        """Ensure either steps list or trigger and agent configuration is present."""
        if self.steps is None and (self.trigger is None or self.agent is None):
            raise ValueError(
                "Workflow must specify either a 'steps' list or both 'trigger' and 'agent'."
            )
        if self.steps is not None and len(self.steps) == 0:
            raise ValueError("Workflow 'steps' list must contain at least one step.")
        return self
