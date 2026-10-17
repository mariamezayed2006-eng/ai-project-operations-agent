"""Domain tools for the AI Project Operations Agent (Project 4).

Phase 2: the controlled interface between future agents and SQLite
(SDD Sections 3.2, 8.3). Every tool in this module:

- calls ``database.py`` (parameterized SQL lives there, never here),
- validates inputs before touching the database,
- returns a ``ToolResult`` envelope so callers can distinguish success,
  not-found, invalid input, simulated failure, and database failure,
- never reports success unless the mutation was persisted and re-read,
- never fabricates tasks, members, deadlines, or completions.

Approval/HITL routing is NOT decided here; these tools form the safe
execution boundary and later phases add the routing layer on top
(``business_rules.needs_human_approval`` + workflows + approval UI).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from enum import Enum
from typing import Any, Iterator

import database
from business_rules import can_mark_completed, utc_today
from models import Priority, StrictModel, TaskStatus

VALID_STATUSES = {s.value for s in TaskStatus}
VALID_PRIORITIES = {p.value for p in Priority}

TITLE_MAX = 200
CONTENT_MAX = 2000
ID_MAX = 64


class ToolStatus(str, Enum):
    """Machine-readable tool outcomes for agents and workflows."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    SIMULATED_FAILURE = "simulated_failure"
    DB_FAILURE = "db_failure"


class ToolResult(StrictModel):
    """Structured envelope returned by every domain tool."""

    status: ToolStatus
    message: str
    data: Any = None

    @property
    def ok(self) -> bool:
        """True only for a confirmed successful result."""
        return self.status == ToolStatus.SUCCESS


# --- Failure simulation (Scenario 10 hook; disabled by default) ---

_SIMULATED_FAILURES: set[str] = set()


def _check_simulated(tool_name: str) -> ToolResult | None:
    """Return a simulated failure result when armed for this tool, else None.

    Never touches the database, so it cannot corrupt data.
    """
    if tool_name in _SIMULATED_FAILURES:
        return ToolResult(
            status=ToolStatus.SIMULATED_FAILURE,
            message=f"Simulated failure for {tool_name}; no database change was made.",
        )
    return None


@contextmanager
def simulated_failure(*tool_names: str) -> Iterator[None]:
    """Arm deterministic failures for the enclosed block (tests/demos only).

    Example: ``with simulated_failure("get_task"): ...``
    """
    _SIMULATED_FAILURES.update(tool_names)
    try:
        yield
    finally:
        _SIMULATED_FAILURES.difference_update(tool_names)


# --- Input helpers ---


def _clean_id(value: Any) -> str | None:
    """Return a stripped id string, or None when malformed."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > ID_MAX:
        return None
    return cleaned


def _invalid(tool: str, why: str) -> ToolResult:
    return ToolResult(status=ToolStatus.INVALID_INPUT, message=f"{tool}: {why}")


def _not_found(tool: str, why: str) -> ToolResult:
    return ToolResult(status=ToolStatus.NOT_FOUND, message=f"{tool}: {why}")


def _db_failure(tool: str) -> ToolResult:
    # Generic message on purpose: never expose raw DB internals.
    return ToolResult(status=ToolStatus.DB_FAILURE, message=f"{tool}: database operation failed.")


def _next_id(prefix: str, existing: list[str]) -> str:
    """Next deterministic id (T009 -> T010). Falls back to prefix + 001."""
    best = 0
    for item in existing:
        if item.startswith(prefix):
            try:
                best = max(best, int(item[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{best + 1:03d}"


def _parse_iso_date(value: str) -> str | None:
    """Return the value when it is a valid YYYY-MM-DD date, else None."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


# --- Read tools (PDD FR-03) ---


def get_project_tasks(conn: sqlite3.Connection, project_id: Any) -> ToolResult:
    """Return all task records for a project, ordered by id."""
    if (hit := _check_simulated("get_project_tasks")) is not None:
        return hit
    pid = _clean_id(project_id)
    if pid is None:
        return _invalid("get_project_tasks", "project_id must be a non-empty string.")
    try:
        if database.get_project(conn, pid) is None:
            return _not_found("get_project_tasks", f"project {pid} not found.")
        tasks = database.list_tasks_by_project(conn, pid)
    except database.DatabaseError:
        return _db_failure("get_project_tasks")
    return ToolResult(status=ToolStatus.SUCCESS, message=f"Found {len(tasks)} task(s).", data=tasks)


def get_task(conn: sqlite3.Connection, task_id: Any) -> ToolResult:
    """Return the exact stored record for one task."""
    if (hit := _check_simulated("get_task")) is not None:
        return hit
    tid = _clean_id(task_id)
    if tid is None:
        return _invalid("get_task", "task_id must be a non-empty string.")
    try:
        task = database.get_task(conn, tid)
    except database.DatabaseError:
        return _db_failure("get_task")
    if task is None:
        return _not_found("get_task", f"task {tid} not found.")
    return ToolResult(status=ToolStatus.SUCCESS, message="Task found.", data=task)


def get_team_members(conn: sqlite3.Connection, project_id: Any) -> ToolResult:
    """Return team members of a project with availability/workload data."""
    if (hit := _check_simulated("get_team_members")) is not None:
        return hit
    pid = _clean_id(project_id)
    if pid is None:
        return _invalid("get_team_members", "project_id must be a non-empty string.")
    try:
        if database.get_project(conn, pid) is None:
            return _not_found("get_team_members", f"project {pid} not found.")
        members = database.list_team_members(conn, pid)
    except database.DatabaseError:
        return _db_failure("get_team_members")
    return ToolResult(status=ToolStatus.SUCCESS, message=f"Found {len(members)} member(s).", data=members)


def get_project_deadline(conn: sqlite3.Connection, project_id: Any) -> ToolResult:
    """Return a project's deadline plus days remaining for risk analysis.

    Only derives ``days_remaining`` from the stored date (explicitly allowed
    by SDD Section 3.3); no policy decisions are made here.
    """
    if (hit := _check_simulated("get_project_deadline")) is not None:
        return hit
    pid = _clean_id(project_id)
    if pid is None:
        return _invalid("get_project_deadline", "project_id must be a non-empty string.")
    try:
        project = database.get_project(conn, pid)
    except database.DatabaseError:
        return _db_failure("get_project_deadline")
    if project is None:
        return _not_found("get_project_deadline", f"project {pid} not found.")
    try:
        days_remaining = (date.fromisoformat(project["deadline"]) - utc_today()).days
    except ValueError:
        return _db_failure("get_project_deadline")
    return ToolResult(
        status=ToolStatus.SUCCESS,
        message="Deadline found.",
        data={
            "project_id": project["id"],
            "project_name": project["name"],
            "deadline": project["deadline"],
            "days_remaining": days_remaining,
        },
    )


# --- Write tools (PDD FR-09; execution boundary only, no approval routing) ---


def create_task(conn: sqlite3.Connection, project_id: Any, title: Any, owner_id: Any = None) -> ToolResult:
    """Create a task after validation; report it only after persistence."""
    if (hit := _check_simulated("create_task")) is not None:
        return hit
    pid = _clean_id(project_id)
    if pid is None:
        return _invalid("create_task", "project_id must be a non-empty string.")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        return _invalid("create_task", "title must be 1-200 non-blank characters.")
    owner: str | None = None
    if owner_id is not None:
        owner = _clean_id(owner_id)
        if owner is None:
            return _invalid("create_task", "owner_id must be a non-empty string when given.")
    try:
        if database.get_project(conn, pid) is None:
            return _not_found("create_task", f"project {pid} not found.")
        if owner is not None and database.get_team_member(conn, owner) is None:
            return _not_found("create_task", f"owner {owner} not found.")
        # Task ids are global primary keys: scan every project to avoid collision.
        all_ids = [
            t["id"] for p in database.list_projects(conn) for t in database.list_tasks_by_project(conn, p["id"])
        ]
        task_id = _next_id("T", all_ids)
        database.insert_task(conn, task_id, pid, title.strip(), "todo", "medium", owner_id=owner)
        created = database.get_task(conn, task_id)
    except database.DatabaseError:
        return _db_failure("create_task")
    if created is None:  # persistence not confirmed: never claim success
        return _db_failure("create_task")
    return ToolResult(status=ToolStatus.SUCCESS, message=f"Task {task_id} created.", data=created)


def update_task(
    conn: sqlite3.Connection,
    task_id: Any,
    status: Any = None,
    priority: Any = None,
    deadline: Any = None,
) -> ToolResult:
    """Update allowed task fields after validation; confirm from the stored row."""
    if (hit := _check_simulated("update_task")) is not None:
        return hit
    tid = _clean_id(task_id)
    if tid is None:
        return _invalid("update_task", "task_id must be a non-empty string.")
    if status is None and priority is None and deadline is None:
        return _invalid("update_task", "at least one of status, priority, or deadline is required.")
    fields: dict[str, Any] = {}
    if status is not None:
        if not isinstance(status, str) or status not in VALID_STATUSES:
            return _invalid("update_task", f"status must be one of {sorted(VALID_STATUSES)}.")
        fields["status"] = status
    if priority is not None:
        if not isinstance(priority, str) or priority not in VALID_PRIORITIES:
            return _invalid("update_task", f"priority must be one of {sorted(VALID_PRIORITIES)}.")
        fields["priority"] = priority
    if deadline is not None:
        if not isinstance(deadline, str) or _parse_iso_date(deadline) is None:
            return _invalid("update_task", "deadline must be a valid YYYY-MM-DD date.")
        fields["deadline"] = deadline
    try:
        current = database.get_task(conn, tid)
    except database.DatabaseError:
        return _db_failure("update_task")
    if current is None:
        return _not_found("update_task", f"task {tid} not found.")
    if fields.get("status") == TaskStatus.COMPLETED.value and not can_mark_completed(
        True, current["completion_percent"], has_supporting_evidence=current["completion_percent"] == 100
    ):
        # Tool-level completion gate: stored progress must already be 100%.
        # (Evidence review and approval routing belong to later phases.)
        return _invalid(
            "update_task",
            "task cannot be marked completed while completion_percent is not 100; completion must not be fabricated.",
        )
    try:
        if not database.update_task_fields(conn, tid, fields):
            return _db_failure("update_task")
        updated = database.get_task(conn, tid)
    except database.DatabaseError:
        return _db_failure("update_task")
    if updated is None:
        return _db_failure("update_task")
    return ToolResult(status=ToolStatus.SUCCESS, message=f"Task {tid} updated.", data=updated)


def assign_task(conn: sqlite3.Connection, task_id: Any, user_id: Any) -> ToolResult:
    """Assign a task to a member of the same project; confirm from the stored row."""
    if (hit := _check_simulated("assign_task")) is not None:
        return hit
    tid = _clean_id(task_id)
    uid = _clean_id(user_id)
    if tid is None:
        return _invalid("assign_task", "task_id must be a non-empty string.")
    if uid is None:
        return _invalid("assign_task", "user_id must be a non-empty string.")
    try:
        task = database.get_task(conn, tid)
        if task is None:
            return _not_found("assign_task", f"task {tid} not found.")
        member = database.get_team_member(conn, uid)
        if member is None or member["project_id"] != task["project_id"]:
            return _not_found("assign_task", f"user {uid} not found on this task's project.")
        if not database.update_task_fields(conn, tid, {"owner_id": uid}):
            return _db_failure("assign_task")
        updated = database.get_task(conn, tid)
    except database.DatabaseError:
        return _db_failure("assign_task")
    if updated is None or updated.get("owner_id") != uid:
        return _db_failure("assign_task")
    return ToolResult(status=ToolStatus.SUCCESS, message=f"Task {tid} assigned to {uid}.", data=updated)


def create_project_comment(conn: sqlite3.Connection, project_id: Any, content: Any) -> ToolResult:
    """Persist a project comment; return it only after persistence."""
    if (hit := _check_simulated("create_project_comment")) is not None:
        return hit
    pid = _clean_id(project_id)
    if pid is None:
        return _invalid("create_project_comment", "project_id must be a non-empty string.")
    if not isinstance(content, str) or not content.strip() or len(content.strip()) > CONTENT_MAX:
        return _invalid("create_project_comment", "content must be 1-2000 non-blank characters.")
    try:
        if database.get_project(conn, pid) is None:
            return _not_found("create_project_comment", f"project {pid} not found.")
        existing = [c["id"] for c in database.list_comments(conn, pid)]
        comment_id = _next_id("C", existing)
        text = content.strip()
        database.insert_comment(conn, comment_id, pid, "agent", text, utc_today().isoformat())
        created = [c for c in database.list_comments(conn, pid) if c["id"] == comment_id]
    except database.DatabaseError:
        return _db_failure("create_project_comment")
    if not created:  # persistence not confirmed: never claim success
        return _db_failure("create_project_comment")
    return ToolResult(status=ToolStatus.SUCCESS, message=f"Comment {comment_id} created.", data=created[0])
