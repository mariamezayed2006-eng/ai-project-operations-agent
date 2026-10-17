"""Deterministic business rules for the AI Project Operations Agent (Project 4).

Implements ONLY the Phase 1 rules (PDD Sections 6-9, SDD Section 6.2):

- ``needs_human_approval`` — approval rule, implemented verbatim.
- ``can_mark_completed`` — completion rule, implemented verbatim.
- ``is_success_claim_valid`` — write rule helper: success claims require a
  tool confirmation; used later by protected tools and workflows.
- ``derive_health`` — project health from evidence counts.

Health thresholds (assumption — neither doc defines green/yellow/red math;
see plan contradiction #5). Chosen to be simple, deterministic, and easy to
change via the module constants below:

- RED when: a critical blocker exists, OR overdue tasks >= ``RED_OVERDUE_COUNT`` (2),
  OR the deadline is within ``RED_DEADLINE_DAYS`` (2) days AND blocked/overdue work exists.
- YELLOW when: any blocked/overdue/critical task exists, OR the deadline is within
  ``YELLOW_DEADLINE_DAYS`` (7) days.
- GREEN otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

RED_OVERDUE_COUNT = 2
RED_DEADLINE_DAYS = 2
YELLOW_DEADLINE_DAYS = 7


def utc_today() -> date:
    """Single documented reference-date source: current UTC calendar day.

    Stored deadlines are plain ISO calendar dates; overdue classification
    compares calendar days in UTC. Centralized here so every caller shares
    one definition of "today" (never LLM-guessed).
    """
    return datetime.now(timezone.utc).date()


def derive_overdue_task_ids(tasks: list[dict], as_of: date) -> list[str]:
    """Derive authoritative overdue ids from task rows and a reference date.

    Overdue = stored deadline strictly before ``as_of`` and status not
    completed. A deadline of today is due, not overdue. Rows with missing
    or unparseable deadlines are skipped (cannot be classified, never
    guessed). Returns sorted ids for determinism.
    """
    today = as_of.isoformat()
    overdue = []
    for task in tasks:
        deadline = task.get("deadline")
        if not deadline:
            continue
        try:
            due = date.fromisoformat(str(deadline))
        except ValueError:
            continue
        if due.isoformat() < today and task.get("status") != "completed":
            task_id = task.get("id")
            if task_id:
                overdue.append(str(task_id))
    return sorted(overdue)


def _row_id(task: dict) -> str | None:
    task_id = task.get("id")
    return str(task_id) if task_id else None


def _sorted_rows(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: str(t.get("id") or ""))


def is_blocked(task: dict) -> bool:
    """True when the row represents blocked work (status or blocker flag)."""
    return task.get("status") == "blocked" or bool(task.get("blocker"))


def derive_blocker_entries(tasks: list[dict]) -> list[str]:
    """Build blocker entries from authoritative rows: id, status, priority.

    Pure facts (``T002 — blocked, critical priority``); no dependencies or
    owners are inferred. Rows without an id are skipped.
    """
    entries = []
    for task in _sorted_rows(tasks):
        task_id = _row_id(task)
        if task_id and is_blocked(task):
            entries.append(f"{task_id} - {task.get('status')}, {task.get('priority')} priority")
    return entries


def derive_risk_entries(tasks: list[dict], as_of: date) -> list[str]:
    """Build factual risk entries from rows: blocked, critical, overdue work.

    Each entry states an evidenced material fact labeled as risk input;
    interpretation and wording beyond that stay with the Status Writer.
    Empty when the rows support no material risk.
    """
    entries = []
    for task in _sorted_rows(tasks):
        task_id = _row_id(task)
        if not task_id:
            continue
        if is_blocked(task):
            entries.append(f"{task_id} is blocked ({task.get('priority')} priority)")
        if task.get("priority") == "critical":
            entries.append(f"{task_id} carries critical priority (status {task.get('status')})")
    overdue = derive_overdue_task_ids(tasks, as_of)
    if overdue:
        entries.append(f"{len(overdue)} overdue as of {as_of.isoformat()}: {', '.join(overdue)}")
    return entries


def derive_material_task_ids(tasks: list[dict], as_of: date) -> set[str]:
    """Ids backing the constructed facts: blocked, critical, or overdue rows."""
    ids = set()
    for task in tasks:
        task_id = _row_id(task)
        if not task_id:
            continue
        if is_blocked(task) or task.get("priority") == "critical":
            ids.add(task_id)
    ids.update(derive_overdue_task_ids(tasks, as_of))
    return ids


def has_critical_blocked(tasks: list[dict]) -> bool:
    """True when any row is both blocked and critical priority (e.g. T002)."""
    return any(is_blocked(task) and task.get("priority") == "critical" and _row_id(task) for task in tasks)


def needs_health_review(
    *,
    critical_blocked_rows: bool,
    accepted: bool,
    rows_available: bool,
    workers_complete: bool,
    deadline_days_remaining: int | None,
    action_affects_commitment: bool,
) -> bool:
    """WF-01 health-review policy: does a human need to look at this assessment?

    Distinct from WF-02 mutation approval (``needs_human_approval`` there
    authorizes a database write). Here True means review ATTENTION only —
    WF-01 is read-only and this flag can never authorize a mutation.
    A review is required when authoritative rows show a critical blocker,
    the final draft was not accepted, essential row evidence is missing,
    worker analysis is incomplete, or the deadline/commitment escalation
    (centralized ``needs_human_approval`` rule, reused — never duplicated)
    fires. A normal, fully evidenced, accepted assessment stays normal.
    """
    if critical_blocked_rows:
        return True
    if not accepted:
        return True
    if not rows_available:
        return True
    if not workers_complete:
        return True
    return bool(
        needs_human_approval(
            critical_blocker=False,
            deadline_days_remaining=deadline_days_remaining,
            action_affects_commitment=action_affects_commitment,
            significant_priority_change=False,
            high_impact_reassignment=False,
        )
    )


def needs_human_approval(
    critical_blocker: bool,
    deadline_days_remaining: int | None,
    action_affects_commitment: bool,
    significant_priority_change: bool,
    high_impact_reassignment: bool,
) -> bool:
    """Return True when a requested action must pause for human approval.

    Verbatim SDD rule: critical_blocker OR (deadline_days_remaining <= 2 AND
    action_affects_commitment) OR significant_priority_change OR
    high_impact_reassignment.
    """
    deadline_escalation = (
        deadline_days_remaining is not None
        and deadline_days_remaining <= RED_DEADLINE_DAYS
        and action_affects_commitment
    )
    return bool(
        critical_blocker or deadline_escalation or significant_priority_change or high_impact_reassignment
    )


def can_mark_completed(task_exists: bool, completion_percent: int, has_supporting_evidence: bool) -> bool:
    """Return True only when a task may be marked completed.

    Verbatim SDD rule: task exists AND completion_percent == 100 AND
    supporting evidence is available.
    """
    return bool(task_exists and completion_percent == 100 and has_supporting_evidence)


def is_success_claim_valid(success: bool, confirmation: str | None) -> bool:
    """Check a reported action outcome against the SDD write rule.

    A side effect may be reported as successful only when the protected
    tool returned a successful confirmation. A non-success outcome
    (failure/rejection message) is valid without a confirmation.
    """
    if not success:
        return True
    return bool(confirmation and confirmation.strip())


def derive_health(
    has_critical_blocker: bool,
    blocked_count: int,
    overdue_count: int,
    critical_count: int,
    days_remaining: int | None,
) -> str:
    """Derive project health (``red`` | ``yellow`` | ``green``) from evidence.

    Thresholds live in ``RED_OVERDUE_COUNT``, ``RED_DEADLINE_DAYS`` and
    ``YELLOW_DEADLINE_DAYS``; adjust them there, not in callers.
    Negative counts raise ValueError (fail fast on bad evidence plumbing).
    """
    for name, value in (
        ("blocked_count", blocked_count),
        ("overdue_count", overdue_count),
        ("critical_count", critical_count),
    ):
        if value < 0:
            raise ValueError(f"{name} must be >= 0")

    if has_critical_blocker or overdue_count >= RED_OVERDUE_COUNT:
        return "red"
    if (
        days_remaining is not None
        and days_remaining <= RED_DEADLINE_DAYS
        and (blocked_count >= 1 or overdue_count >= 1)
    ):
        return "red"
    if blocked_count >= 1 or overdue_count >= 1 or critical_count >= 1:
        return "yellow"
    if days_remaining is not None and days_remaining <= YELLOW_DEADLINE_DAYS:
        return "yellow"
    return "green"
