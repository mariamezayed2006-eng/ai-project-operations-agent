"""Human-in-the-loop approval lifecycle for the AI Project Operations Agent (Project 4).

Workflow-level safety around the Phase 2 protected tools (PDD Section 9,
SDD Section 6.3). States: PENDING → APPROVED → EXECUTED / FAILED, or
PENDING → REJECTED. The store is process-scoped by design (the SDD defines
no approval table); later phases may persist it.

Pause-before-mutation is structural: creating a request never mutates.
Approval is immutable by construction: ``approve``/``execute`` take no
action parameters — only the stored ``requested_values`` ever execute.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

import business_rules as rules
import database
import models
import tools
from tools import ToolStatus

APPROVE = "approve"
REJECT = "reject"


class ApprovalStatus(str, Enum):
    """Lifecycle states; no other states exist."""

    PENDING = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class ApprovalRequest:
    """One held action awaiting a human decision (evidence summary, no reasoning)."""

    id: str
    project_id: str
    task_id: str | None
    action_type: str
    requested_values: dict[str, Any]
    current_state: dict[str, Any]
    reason: str
    evidence: str
    requester: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    message: str = ""


@dataclass
class ApprovalOutcome:
    """Result of creating a request (safe failure instead of exceptions)."""

    success: bool
    request: ApprovalRequest | None
    message: str


@dataclass
class ApprovalStore:
    """Process-scoped request registry with deterministic AP### ids."""

    requests: dict[str, ApprovalRequest] = field(default_factory=dict)
    counter: int = 0

    def get(self, request_id: str) -> ApprovalRequest | None:
        """Return one request by id, or None when unknown."""
        return self.requests.get(request_id)


def approval_reason(flags: dict[str, bool], days_remaining: int | None) -> str:
    """Render the policy reason from the triggered rule branches."""
    parts = []
    if flags.get("critical_blocker"):
        parts.append("critical blocker involved")
    if flags.get("significant_priority_change"):
        parts.append("significant priority change")
    if flags.get("high_impact_reassignment"):
        parts.append("high-impact reassignment")
    if days_remaining is not None and days_remaining <= rules.RED_DEADLINE_DAYS and flags.get("action_affects_commitment"):
        parts.append(f"deadline in {days_remaining} day(s) with commitment-affecting change")
    return "; ".join(parts) or "policy review required"


def create_approval_request(
    store: ApprovalStore,
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str | None,
    action_type: str,
    requested_values: dict[str, Any],
    requester: str,
    reason: str,
    evidence: str = "",
) -> ApprovalOutcome:
    """Validate and hold an action for review. Never mutates project data."""
    if action_type not in {a.value for a in models.ActionType}:
        return ApprovalOutcome(False, None, f"unknown action_type: {action_type}")
    try:
        if action_type in {"update_task", "assign_task"}:
            if task_id is None or database.get_task(conn, task_id) is None:
                return ApprovalOutcome(False, None, f"task {task_id} not found; no request created.")
            current: dict[str, Any] = dict(database.get_task(conn, task_id) or {})
        else:
            if database.get_project(conn, project_id) is None:
                return ApprovalOutcome(False, None, f"project {project_id} not found; no request created.")
            current = dict(database.get_project(conn, project_id) or {})
    except database.DatabaseError:
        return ApprovalOutcome(False, None, "database operation failed; no request created.")
    store.counter += 1
    request = ApprovalRequest(
        id=f"AP{store.counter:03d}",
        project_id=project_id,
        task_id=task_id,
        action_type=action_type,
        requested_values=dict(requested_values),
        current_state=current,
        reason=reason,
        evidence=evidence,
        requester=requester,
    )
    store.requests[request.id] = request
    return ApprovalOutcome(True, request, f"Request {request.id} pending human review.")


def reject_request(store: ApprovalStore, request_id: str, approver: str) -> Any:
    """Reject a pending request. Never mutates project data."""
    from workflows import CoordinationResult  # local import: workflows owns the envelope

    _ = approver  # recorded by future audit/trace layers; decision authority is the act itself
    request = store.get(request_id)
    if request is None:
        return CoordinationResult(error=f"unknown approval request: {request_id}")
    if request.status != ApprovalStatus.PENDING:
        return CoordinationResult(error=f"request {request_id} already resolved as {request.status.value}")
    request.status = ApprovalStatus.REJECTED
    request.message = "Rejected by human approver; the action was not executed."
    return CoordinationResult(
        action_result=models.ActionResult(
            action_type=models.ActionType(request.action_type),
            target_id=request.task_id or request.project_id,
            success=False,
            approval_required=True,
            message=request.message,
        )
    )


def approve_and_execute(store: ApprovalStore, conn: sqlite3.Connection, request_id: str, approver: str) -> Any:
    """Approve a pending request, revalidate live state, then execute once.

    Executes ONLY the stored ``requested_values`` (immutability). Any
    revalidation failure → FAILED with no mutation and no auto-retry.
    """
    from workflows import CoordinationResult

    _ = approver
    request = store.get(request_id)
    if request is None:
        return CoordinationResult(error=f"unknown approval request: {request_id}")
    if request.status != ApprovalStatus.PENDING:
        return CoordinationResult(error=f"request {request_id} already resolved as {request.status.value}")
    request.status = ApprovalStatus.APPROVED
    try:
        outcome = _revalidate_and_execute(conn, request)
    except database.DatabaseError:
        request.status = ApprovalStatus.FAILED
        request.message = "Database failure during approved execution; nothing was reported as success."
        return CoordinationResult(
            action_result=models.ActionResult(
                action_type=models.ActionType(request.action_type),
                target_id=request.task_id or request.project_id,
                success=False, approval_required=True, message=request.message,
            )
        )
    request.status = ApprovalStatus.EXECUTED if outcome.success else ApprovalStatus.FAILED
    request.message = outcome.message
    return CoordinationResult(action_result=outcome)


def resolve_approval(store: ApprovalStore, conn: sqlite3.Connection, request_id: str, decision: str, approver: str) -> Any:
    """Single decision entrypoint: 'approve' executes, 'reject' holds."""
    from workflows import CoordinationResult

    if decision == APPROVE:
        return approve_and_execute(store, conn, request_id, approver)
    if decision == REJECT:
        return reject_request(store, request_id, approver)
    return CoordinationResult(error=f"unknown decision: {decision}; use 'approve' or 'reject'.")


def _revalidate_and_execute(conn: sqlite3.Connection, request: ApprovalRequest) -> models.ActionResult:
    """Reload live state, re-check validity, execute stored values, verify."""
    action = models.ActionType(request.action_type)
    target = request.task_id or request.project_id
    failure = lambda msg: models.ActionResult(
        action_type=action, target_id=target, success=False, approval_required=True, message=msg)

    if action in {models.ActionType.UPDATE_TASK, models.ActionType.ASSIGN_TASK}:
        task = database.get_task(conn, request.task_id or "")
        if task is None:
            return failure(f"task {request.task_id} no longer exists; approved action not executed.")
        if task.get("project_id") != request.project_id:
            return failure("task project changed since approval; approved action not executed.")
        if action == models.ActionType.UPDATE_TASK and request.requested_values.get("status") == "completed":
            if not rules.can_mark_completed(True, task["completion_percent"], task["completion_percent"] == 100):
                return failure("completion evidence still insufficient; approved action not executed.")
        if action == models.ActionType.ASSIGN_TASK:
            member = database.get_team_member(conn, str(request.requested_values.get("user_id", "")))
            if member is None or member["project_id"] != request.project_id:
                return failure("assignee unavailable; approved action not executed.")
    elif database.get_project(conn, request.project_id) is None:
        return failure(f"project {request.project_id} no longer exists; approved action not executed.")

    values = request.requested_values
    if action == models.ActionType.CREATE_TASK:
        res = tools.create_task(conn, request.project_id, values.get("title", ""), owner_id=values.get("owner_id"))
    elif action == models.ActionType.UPDATE_TASK:
        res = tools.update_task(conn, request.task_id or "", status=values.get("status"),
                                priority=values.get("priority"), deadline=values.get("deadline"))
    elif action == models.ActionType.ASSIGN_TASK:
        res = tools.assign_task(conn, request.task_id or "", values.get("user_id", ""))
    else:
        res = tools.create_project_comment(conn, request.project_id, values.get("content", ""))

    if res.status != ToolStatus.SUCCESS:
        return failure(f"approved tool call failed ({res.status.value}); nothing was reported as success.")
    confirmed_id = res.data.get("id") if isinstance(res.data, dict) else None
    return models.ActionResult(
        action_type=action, target_id=str(confirmed_id or target), success=True,
        approval_required=True, confirmation=f"approved {request.id} executed and verified.",
        message=f"{request.action_type} executed after approval of {request.id}.",
    )


def pending_summary(request: ApprovalRequest) -> str:
    """Concise human-facing summary (evidence, not reasoning)."""
    return (
        f"{request.id}: {request.action_type} on {request.task_id or request.project_id} "
        f"requested by {request.requester} — {request.reason}. "
        f"Requested: {request.requested_values}. Status: {request.status.value}. "
        "This action is pending and has NOT been executed."
    )


__all__ = [
    "APPROVE", "REJECT", "ApprovalStatus", "ApprovalRequest", "ApprovalOutcome",
    "ApprovalStore", "approval_reason", "create_approval_request", "reject_request",
    "approve_and_execute", "resolve_approval", "pending_summary",
]
