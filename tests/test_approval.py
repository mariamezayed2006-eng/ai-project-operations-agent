"""Phase 5 tests: approval lifecycle, pause-before-mutation, revalidation, WF-02 HITL."""

import asyncio

import approval
import database
import seed_database
import tools
import workflows
from approval import ApprovalStatus


def fresh_store():
    """New empty approval registry per test."""
    return approval.ApprovalStore()


def snapshot(conn):
    """Full task table snapshot for before/after mutation comparison."""
    return {
        t["id"]: (t["status"], t["owner_id"], t["priority"], t["completion_percent"])
        for t in database.list_tasks_by_project(conn, "P001")
    } | {
        t["id"]: (t["status"], t["owner_id"], t["priority"], t["completion_percent"])
        for t in database.list_tasks_by_project(conn, "P002")
    }


def test_normal_action_executes_without_approval(conn):
    """Low-impact comment runs with no request created."""
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "create_project_comment", values={"content": "Notes."}, store=store))
    assert res.action_result is not None and res.action_result.success is True
    assert res.pending is None and store.requests == {}


def test_high_impact_creates_pending_with_zero_mutation(conn):
    """Critical assignment is held; the database is byte-identical before/after."""
    store = fresh_store()
    before = snapshot(conn)
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}, store=store))
    assert res.pending is not None and res.pending.request_id == "AP001"
    assert store.get("AP001") is not None and store.get("AP001").status == ApprovalStatus.PENDING
    assert snapshot(conn) == before


def test_approve_executes_exactly_requested_action(conn):
    """Approval runs the stored values only; unrelated fields are untouched."""
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}, store=store))
    decided = approval.resolve_approval(store, conn, res.pending.request_id, "approve", "U001")
    assert decided.action_result is not None and decided.action_result.success is True
    assert store.get("AP001").status == ApprovalStatus.EXECUTED
    task = database.get_task(conn, "T002")
    assert task["owner_id"] == "U003" and task["status"] == "blocked" and task["priority"] == "critical"


def test_reject_causes_zero_mutation(conn):
    """Rejection reports failure and changes nothing."""
    store = fresh_store()
    before = snapshot(conn)
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}, store=store))
    decided = approval.resolve_approval(store, conn, res.pending.request_id, "reject", "U001")
    assert decided.action_result is not None and decided.action_result.success is False
    assert "not executed" in decided.action_result.message
    assert store.get("AP001").status == ApprovalStatus.REJECTED
    assert snapshot(conn) == before


def test_unknown_and_duplicate_resolutions_fail_safely(conn):
    """Unknown ids and second resolutions are errors, never executions."""
    store = fresh_store()
    assert approval.resolve_approval(store, conn, "AP999", "approve", "U001").error
    assert approval.reject_request(store, "AP999", "U001").error
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}, store=store))
    rid = res.pending.request_id
    approval.resolve_approval(store, conn, rid, "reject", "U001")
    assert approval.resolve_approval(store, conn, rid, "approve", "U001").error
    assert approval.reject_request(store, rid, "U001").error
    assert approval.resolve_approval(store, conn, rid, "maybe", "U001").error


def test_stale_approval_blocked_when_target_vanishes(conn):
    """Approving after the task disappears fails safely with no execution."""
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T003", values={"user_id": "U001"}, store=store))
    conn.execute("DELETE FROM tasks WHERE id = 'T003'")
    conn.commit()
    decided = approval.resolve_approval(store, conn, res.pending.request_id, "approve", "U001")
    assert decided.action_result is not None and decided.action_result.success is False
    assert store.get(res.pending.request_id).status == ApprovalStatus.FAILED


def test_approved_completion_without_evidence_blocked(conn):
    """Approval cannot launder an evidence-free completion claim."""
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "update_task", task_id="T004", values={"status": "completed"}, store=store))
    # T004 is 40% done: either held for review or refused outright — never success.
    if res.pending is not None:
        decided = approval.resolve_approval(store, conn, res.pending.request_id, "approve", "U001")
        assert decided.action_result is not None and decided.action_result.success is False
    else:
        assert res.action_result is not None and res.action_result.success is False
    assert database.get_task(conn, "T004")["status"] == "in_progress"


def test_tool_failure_after_approval_is_failed_never_success(conn):
    """Simulated persistence failure at execution time stays a failure."""
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        conn, "P002", "U005", "assign_task", task_id="T007", values={"user_id": "U006"}, store=store))
    assert res.pending is not None  # reassignment always needs review
    with tools.simulated_failure("assign_task"):
        decided = approval.resolve_approval(store, conn, res.pending.request_id, "approve", "U005")
    assert decided.action_result is not None and decided.action_result.success is False
    assert store.get(res.pending.request_id).status == ApprovalStatus.FAILED
    assert database.get_task(conn, "T007")["owner_id"] == "U005"


def test_db_failure_after_approval_is_failed(conn, tmp_path):
    """A broken connection at execution time fails safely without false success."""
    db = tmp_path / "ap.db"
    seed_database.seed(db)
    live = database.get_connection(db)
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        live, "P002", "U005", "assign_task", task_id="T007", values={"user_id": "U006"}, store=store))
    live.close()  # break persistence beneath the approved request
    decided = approval.resolve_approval(store, live, res.pending.request_id, "approve", "U005")
    assert decided.action_result is not None and decided.action_result.success is False
    assert store.get(res.pending.request_id).status == ApprovalStatus.FAILED


def test_role_denial_blocks_non_owner_mutation(conn):
    """A non-owner member cannot assign; nothing mutates."""
    before = snapshot(conn)
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U002", "assign_task", task_id="T003", values={"user_id": "U002"}))
    assert res.action_result is not None and res.action_result.success is False
    assert "permitted" in res.action_result.message
    assert snapshot(conn) == before


def test_pending_summary_states_not_executed(conn):
    """Approval requests explain themselves and state pending status honestly."""
    store = fresh_store()
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}, store=store))
    text = approval.pending_summary(store.get(res.pending.request_id))
    assert "AP001" in text and "NOT been executed" in text and "U003" in text


def test_legacy_hold_path_without_store_unchanged(conn):
    """Phase 4 hold-only behavior is preserved when no store is provided."""
    before = snapshot(conn)
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}))
    assert res.pending is not None and res.pending.request_id is None
    assert snapshot(conn) == before
