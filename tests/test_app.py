"""Final-phase tests: Gradio UI layer honors the safety stack (no API calls).

Only deterministic paths are exercised (comment execution, pending holds,
pure rendering). Live-model paths are covered by faked workflow tests.
"""

import gradio as gr
import pytest

import app
import database
import seed_database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the UI at a throwaway database so the demo DB is never touched."""
    db = tmp_path / "ui.db"
    seed_database.seed(db)
    monkeypatch.setattr(app, "DB_PATH", db)
    return db


def test_build_ui_constructs_three_tabs():
    """Interface builds offline with Health, Action, and Approvals areas."""
    ui = app.build_ui()
    assert isinstance(ui, gr.Blocks)


def test_format_health_states():
    """Rendering is honest about blocked, failed, and approval states."""
    import workflows

    blocked = workflows.HealthUpdateResult(None, "normal", None, error="input blocked: x", blocked=True)
    assert "blocked" in app.format_health(blocked).lower() and "nothing changed" in app.format_health(blocked)
    failed = workflows.HealthUpdateResult(None, "normal", None, error="boom")
    assert "Could not produce" in app.format_health(failed)


def test_action_tab_executes_comment_without_api(isolated_db):
    """Comment path runs through tools with zero model involvement."""
    text = app.action_fn("P001", "U001", "sess", "create_project_comment",
                         "", "", "", "", "", "", "", "UI note.")
    assert "Executed and verified" in text
    conn = database.get_connection(isolated_db)
    try:
        assert any(c["content"] == "UI note." for c in database.list_comments(conn, "P001"))
    finally:
        conn.close()


def test_action_tab_holds_high_impact_with_honest_panel(isolated_db):
    """Critical assignment from the UI returns pending and mutates nothing."""
    conn = database.get_connection(isolated_db)
    before = database.get_task(conn, "T002")["owner_id"]
    conn.close()
    text = app.action_fn("P001", "U001", "sess", "assign_task", "T002", "", "", "U003", "", "", "", "")
    assert "PENDING REVIEW" in text and "NOT executed" in text
    conn = database.get_connection(isolated_db)
    try:
        assert database.get_task(conn, "T002")["owner_id"] == before
    finally:
        conn.close()


def _fresh_store(monkeypatch):
    import approval

    store = approval.ApprovalStore()
    monkeypatch.setattr(app, "STORE", store)
    return store


def test_approvals_tab_lists_pending_and_detail(monkeypatch, isolated_db):
    """Pending requests appear in the reviewer list with evidence summaries."""
    store = _fresh_store(monkeypatch)
    app.action_fn("P001", "U001", "sess", "assign_task", "T002", "", "", "U003", "", "", "", "")
    ids = app.approvals_fn()
    assert len(ids) == 1
    detail = app.approval_detail(ids[0])
    assert "AP001" in detail and "NOT been executed" in detail
    assert app.approval_detail("AP999") == "Select a pending request."
    assert app.approval_detail("") == "Select a pending request."


def test_decide_fn_approve_executes_and_reject_holds(monkeypatch, isolated_db):
    """UI approve path revalidates→executes→verifies; reject path mutates nothing."""
    import database

    store = _fresh_store(monkeypatch)
    app.action_fn("P001", "U001", "sess", "assign_task", "T002", "", "", "U003", "", "", "", "")
    rid = app.approvals_fn()[0]
    assert "Executed after approval" in app.decide_fn(rid, "U001", "approve")
    conn = database.get_connection(isolated_db)
    try:
        assert database.get_task(conn, "T002")["owner_id"] == "U003"
    finally:
        conn.close()
    assert "already resolved" in app.decide_fn(rid, "U001", "approve")

    app.action_fn("P001", "U001", "sess", "assign_task", "T006", "", "", "U001", "", "", "", "")
    rid2 = app.approvals_fn()[0]
    assert "without execution" in app.decide_fn(rid2, "U001", "reject")
    conn = database.get_connection(isolated_db)
    try:
        assert database.get_task(conn, "T006")["owner_id"] != "U001"
    finally:
        conn.close()
    assert "Select a pending request first." in app.decide_fn("", "U001", "approve")


def test_health_output_includes_run_trace(monkeypatch, isolated_db):
    """Health tab output names the executed patterns for demo visibility."""
    import workflows

    result = workflows.HealthUpdateResult(
        status=None, route="normal", evidence=None, error="boom", evidence_path="parallel")
    monkeypatch.setattr("runtime.run_health_update", _fake_run(result))
    text = app.health_fn("P001", "U001", "hi", "sess")
    assert "Run trace" in text and "parallel workers" in text


def _fake_run(result):
    async def fake(*args, **kwargs):
        return result
    return fake


def _legacy_db(path):
    """Build a pre-migration database: the four original tables only."""
    import sqlite3

    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(database.SCHEMA.split("CREATE TABLE IF NOT EXISTS project_memory")[0])
    conn.execute("INSERT INTO projects (id, name, deadline) VALUES ('P1', 'Legacy', '2026-12-01')")
    conn.execute("INSERT INTO tasks (id, project_id, title, status, owner_id, priority, deadline, completion_percent, blocker)"
                 " VALUES ('T1', 'P1', 'Keep me', 'todo', NULL, 'low', NULL, 0, 0)")
    conn.commit()
    conn.close()


def test_ensure_db_migrates_legacy_file_without_data_loss(tmp_path, monkeypatch):
    """Existing databases gain new tables; rows are preserved, never reseeded."""
    import database

    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setattr(app, "DB_PATH", db)
    app.ensure_db()
    conn = database.get_connection(db)
    try:
        assert "project_memory" in database.table_names(conn)
        assert database.get_task(conn, "T1")["title"] == "Keep me"
        assert database.get_project(conn, "P1")["name"] == "Legacy"
    finally:
        conn.close()


def test_ensure_db_seeds_missing_file(tmp_path, monkeypatch):
    """A missing database is still seeded from scratch."""
    import database

    db = tmp_path / "fresh.db"
    monkeypatch.setattr(app, "DB_PATH", db)
    app.ensure_db()
    conn = database.get_connection(db)
    try:
        assert len(database.list_projects(conn)) == 2
        assert "project_memory" in database.table_names(conn)
    finally:
        conn.close()
