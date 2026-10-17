"""Pydantic models for the AI Project Operations Agent (Project 4).

Implements the 7 structured-output models from SDD Section 3.3.
Value sets below are the "known sets" the SDD requires but does not
enumerate; they are kept minimal and documented here as an assumption:

- TaskStatus: todo | in_progress | blocked | completed
- Priority: low | medium | high | critical
- Availability: available | busy | unavailable
- Health: green | yellow | red
- RiskLevel: low | medium | high
- ActionType allow-list: create_task | update_task | assign_task | create_project_comment

NOTE (plan contradiction #2): PDD Section 8.3 names a ``ProjectEvidence``
stage output, but SDD Section 3.3 does not define it as a model. The merged
worker evidence therefore stays a plain object in future ``workflows.py``;
there is intentionally no ``ProjectEvidence`` model here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base model: forbid unknown fields so invalid model output fails loudly."""

    model_config = ConfigDict(extra="forbid")


class TaskStatus(str, Enum):
    """Allowed task lifecycle states."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class Priority(str, Enum):
    """Allowed task priorities."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Availability(str, Enum):
    """Allowed team-member availability states."""

    AVAILABLE = "available"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


class Health(str, Enum):
    """Allowed project health values (SDD: green/yellow/red only)."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class RiskLevel(str, Enum):
    """Allowed schedule-risk levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionType(str, Enum):
    """Allow-listed action types a user update may request."""

    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"
    ASSIGN_TASK = "assign_task"
    CREATE_PROJECT_COMMENT = "create_project_comment"


class RequestedAction(StrictModel):
    """One action requested in a user update."""

    action_type: ActionType
    task_id: str | None = Field(default=None, max_length=64)
    values: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(StrictModel):
    """Normalized user update produced by the Intake Agent (PDD FR-02)."""

    project_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    mentioned_task_ids: list[str] = Field(default_factory=list, max_length=50)
    blockers: list[str] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)
    requested_actions: list[RequestedAction] = Field(default_factory=list, max_length=20)


class TaskAnalysis(StrictModel):
    """Structured task evidence from the Task Worker."""

    project_id: str = Field(min_length=1, max_length=64)
    overdue_task_ids: list[str] = Field(default_factory=list, max_length=200)
    blocked_task_ids: list[str] = Field(default_factory=list, max_length=200)
    critical_task_ids: list[str] = Field(default_factory=list, max_length=200)
    findings: list[str] = Field(default_factory=list, max_length=50)


class PeopleAnalysis(StrictModel):
    """Structured ownership/workload evidence from the People Worker."""

    project_id: str = Field(min_length=1, max_length=64)
    overloaded_member_ids: list[str] = Field(default_factory=list, max_length=100)
    unavailable_member_ids: list[str] = Field(default_factory=list, max_length=100)
    findings: list[str] = Field(default_factory=list, max_length=50)


class DeadlineAnalysis(StrictModel):
    """Deadline and schedule-risk evidence from the Deadline Worker."""

    project_id: str = Field(min_length=1, max_length=64)
    deadline: str = Field(min_length=1, max_length=32, description="ISO date of the project deadline")
    days_remaining: int = Field(description="Days from today to the deadline; negative when overdue")
    risk_level: RiskLevel
    at_risk_task_ids: list[str] = Field(default_factory=list, max_length=200)


class ProjectStatus(StrictModel):
    """Final management status produced by the Status Writer (PDD FR-07)."""

    project_id: str = Field(min_length=1, max_length=64)
    health: Health
    progress_percent: int = Field(ge=0, le=100)
    blockers: list[str] = Field(default_factory=list, max_length=50)
    risks: list[str] = Field(default_factory=list, max_length=50)
    overdue_tasks: list[str] = Field(default_factory=list, max_length=200)
    recommended_actions: list[str] = Field(default_factory=list, max_length=50)
    evidence_task_ids: list[str] = Field(default_factory=list, max_length=200)
    needs_human_approval: bool = Field(
        default=False,
        description="Review attention for this assessment (a human should look); "
        "never mutation authorization — WF-01 performs no writes.",
    )

    @model_validator(mode="after")
    def _evidence_required_for_task_claims(self) -> ProjectStatus:
        """Task claims must cite evidence (SDD Section 3.3 grounding rule)."""
        if (self.blockers or self.overdue_tasks) and not self.evidence_task_ids:
            raise ValueError("evidence_task_ids is required when blockers or overdue_tasks are reported")
        return self


class EvaluationResult(StrictModel):
    """Pass/fail review of a draft status (PDD FR-08)."""

    passed: bool
    issues: list[str] = Field(default_factory=list, max_length=50)
    missing_evidence: list[str] = Field(default_factory=list, max_length=100)
    revision_instructions: str = Field(default="", max_length=2000)
    iteration: int = Field(ge=0, le=2, description="Draft number; bounded revision loop caps at 2")

    @model_validator(mode="after")
    def _failed_needs_guidance(self) -> EvaluationResult:
        """A rejection must say what to fix so the bounded retry can act on it."""
        if not self.passed and not (self.issues or self.missing_evidence or self.revision_instructions):
            raise ValueError("a failed evaluation must provide issues, missing_evidence, or revision_instructions")
        return self


class ActionResult(StrictModel):
    """Verified outcome of a task coordination action (SDD WF-02 output schema)."""

    action_type: ActionType
    target_id: str = Field(min_length=1, max_length=64)
    success: bool
    approval_required: bool = False
    confirmation: str = Field(default="", max_length=1000)
    message: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _success_needs_confirmation(self) -> ActionResult:
        """Success may only be reported from a tool confirmation (SDD write rule)."""
        if self.success and not self.confirmation.strip():
            raise ValueError("confirmation is required when success is true")
        return self
