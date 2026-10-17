"""UI-polish tests: theme, real-data metrics, and the new handlers.

Offline only (no API calls). The new handlers reuse the same backend
functions the existing tests already cover, so these tests focus on
rendering honesty and on the safety properties the UI must not bypass.
"""

import gradio as gr
import pytest

import app
import approval
import database
import models
import seed_database
import workflows


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the UI at a throwaway database so the demo DB is never touched."""
    db = tmp_path / "ui_polish.db"
    seed_database.seed(db)
    monkeypatch.setattr(app, "DB_PATH", db)
    return db


def _fresh_store(monkeypatch):
    store = approval.ApprovalStore()
    monkeypatch.setattr(app, "STORE", store)
    return store


# --- Theme and palette --------------------------------------------------------


def test_palette_matches_required_spec():
    """Every required colour is present and used by the stylesheet."""
    required = {
        "bg": "#0A1220", "card": "#111D30", "surface": "#243650",
        "accent": "#C6A982", "cream": "#E8D8C2", "text": "#F2F0EB",
        "muted": "#A9B4C5", "border": "#2B3A50",
        "ok": "#22C55E", "warn": "#F59E0B", "crit": "#EF4444",
    }
    assert app.PALETTE == required
    for hexcode in required.values():
        assert hexcode in app.CUSTOM_CSS


def test_theme_builds_without_error():
    """Theme construction is version-safe (unknown keys filtered)."""
    theme = app.build_theme()
    assert theme is not None


# --- Real-data snapshot and metrics -------------------------------------------


def test_snapshot_derives_real_counts(isolated_db):
    """Counts/overdue/blocked come from the database via the backend rules."""
    snap = app._snapshot("P001")
    assert snap["total"] == 6
    assert snap["status_counts"]["completed"] == 1
    assert snap["overdue"] == ["T002", "T004"]
    assert snap["blocked_ids"] == ["T002"]
    assert snap["critical_ids"] == ["T002", "T006"]


def test_metrics_use_database_values_not_placeholders(isolated_db):
    """Cards show the true project figures; another project shows its own."""
    p1 = app._render_metrics(app._snapshot("P001"))
    assert "Total tasks" in p1 and '">6<' in p1
    assert "Overdue" in p1 and '">2<' in p1  # 2 overdue >= RED threshold -> critical tone
    p2 = app._render_metrics(app._snapshot("P002"))
    assert '">3<' in p2


def test_tasks_rows_shape_matches_headers(isolated_db):
    """The task table rows align with the declared headers."""
    rows = app.tasks_rows("P001")
    assert len(rows) == 6
    assert all(len(row) == len(app.TASK_HEADERS) for row in rows)
    assert rows[0][0] == "T001"


# --- Health tab ----------------------------------------------------------------


def test_health_run_returns_five_honest_outputs(monkeypatch, isolated_db):
    """A successful run populates metrics, banner, analysis, activity, trace."""
    status = models.ProjectStatus(
        project_id="P001", health=models.Health.RED, progress_percent=40,
        blockers=["T002 - blocked, critical priority"], overdue_tasks=["T002"],
        evidence_task_ids=["T002"],
    )
    result = workflows.HealthUpdateResult(
        status=status, route="requires_human_review", evidence=workflows.EvidenceBundle(),
        iterations=2, accepted=False, evidence_path="parallel",
        warning="Evaluator rejected all drafts.",
    )
    monkeypatch.setattr("runtime.run_health_update", _fake(result))

    metrics, banner, analysis, activity, trace = app.health_run("P001", "U001", "hi", "sess")
    assert "Total tasks" in metrics
    assert "Project health: Critical" in banner
    assert "Health: RED" in analysis
    assert "Workflow activity" in activity
    assert "Run trace" in trace and "parallel workers" in trace


def test_health_run_transport_error_is_safe(monkeypatch, isolated_db):
    """A transport failure is reported honestly and never as a status."""
    monkeypatch.setattr("runtime.run_health_update", _raising())
    metrics, banner, analysis, activity, trace = app.health_run("P001", "U001", "hi", "sess")
    assert "Request failed safely" in analysis
    assert "Nothing was changed" in analysis
    assert "failed safely" in banner.lower()


def _fake(result):
    async def run(*args, **kwargs):
        return result
    return run


def _raising():
    async def run(*args, **kwargs):
        raise RuntimeError("boom")
    return run


# --- Task Actions tab ----------------------------------------------------------


def test_action_run_executes_comment_without_api(isolated_db):
    """The comment path executes once and the table refreshes."""
    banner, text, activity, table = app.action_run(
        "P001", "U001", "sess", "create_project_comment",
        "", "", "", "", "", "", "", "UI polish note.")
    assert "Executed and verified" in text
    assert "Executed and verified" in banner
    conn = database.get_connection(isolated_db)
    try:
        assert any(c["content"] == "UI polish note." for c in database.list_comments(conn, "P001"))
    finally:
        conn.close()


def test_action_run_tolerates_hidden_none_fields(isolated_db):
    """Hidden Gradio components arrive as None; the handler must not crash.

    Regression: ``None.strip()`` used to raise AttributeError when fields
    were hidden for the selected action.
    """
    banner, text, activity, table = app.action_run(
        "P001", "U001", "sess", "create_project_comment",
        None, None, None, None, None, None, None, "None-safe note.")
    assert "Executed and verified" in text
    conn = database.get_connection(isolated_db)
    try:
        assert any(c["content"] == "None-safe note." for c in database.list_comments(conn, "P001"))
    finally:
        conn.close()


def test_action_run_high_impact_holds_without_mutation(isolated_db):
    """High-impact assignments hold for review and change nothing."""
    conn = database.get_connection(isolated_db)
    before = database.get_task(conn, "T002")["owner_id"]
    conn.close()
    banner, text, activity, table = app.action_run(
        "P001", "U001", "sess", "assign_task",
        "T002", "", "", "U003", "", "", "", "")
    assert "Pending review — NOT executed" in banner
    assert "NOT executed" in text
    assert "Human review" in activity and "required" in activity
    conn = database.get_connection(isolated_db)
    try:
        assert database.get_task(conn, "T002")["owner_id"] == before
    finally:
        conn.close()


def test_toggle_action_fields_returns_eight_updates():
    """Field visibility helper returns one update per field, no errors."""
    updates = app.toggle_action_fields("create_task")
    assert len(updates) == 8
    updates = app.toggle_action_fields("create_project_comment")
    assert len(updates) == 8


# --- Approvals tab -------------------------------------------------------------


def test_approval_panel_pending_states_not_executed(monkeypatch, isolated_db):
    """Panel distinguishes a proposal from an executed action."""
    _fresh_store(monkeypatch)
    app.action_fn("P001", "U001", "sess", "assign_task", "T002", "", "", "U003", "", "", "", "")
    rid = app.approvals_fn()[0]
    panel = app.approval_panel(rid)
    assert "Pending request" in panel and "not yet executed" in panel.lower()
    assert "pending_review" in panel
    assert "Approval flow" in panel


def test_decide_run_approve_executes_then_blocks_duplicate(monkeypatch, isolated_db):
    """Approve executes after revalidation; a second submit is refused."""
    _fresh_store(monkeypatch)
    app.action_fn("P001", "U001", "sess", "assign_task", "T002", "", "", "U003", "", "", "", "")
    rid = app.approvals_fn()[0]
    panel, banner, text, updated = app.decide_run(rid, "U001", "approve")
    assert "Executed after approval" in text
    assert "ok" in banner
    assert "executed" in panel
    conn = database.get_connection(isolated_db)
    try:
        assert database.get_task(conn, "T002")["owner_id"] == "U003"
    finally:
        conn.close()
    _, _, text2, _ = app.decide_run(rid, "U001", "approve")
    assert "already resolved" in text2


def test_decide_run_reject_mutates_nothing(monkeypatch, isolated_db):
    """Reject reports a resolved state and performs no write."""
    _fresh_store(monkeypatch)
    app.action_fn("P001", "U001", "sess", "assign_task", "T006", "", "", "U001", "", "", "", "")
    rid = app.approvals_fn()[0]
    _, banner, text, _ = app.decide_run(rid, "U001", "reject")
    assert "without execution" in text
    assert "Rejected" in banner
    conn = database.get_connection(isolated_db)
    try:
        assert database.get_task(conn, "T006")["owner_id"] != "U001"
    finally:
        conn.close()


def test_refresh_approvals_clears_detail(monkeypatch):
    """Refresh returns a fresh dropdown update and clears the detail panel."""
    _fresh_store(monkeypatch)
    update, panel, banner, result = app.refresh_approvals()
    assert "Select a pending request" in panel
    assert banner == "" and result == ""


def test_select_approval_none_is_a_noop(monkeypatch):
    """A programmatic None selection must not clobber the visible panel/banner."""
    _fresh_store(monkeypatch)
    detail, banner, result = app.select_approval(None)
    assert not isinstance(detail, str)   # gr.update(), not a replacement panel
    assert not isinstance(banner, str)
    assert not isinstance(result, str)


def test_decide_run_clears_selection_without_clobbering(monkeypatch, isolated_db):
    """After a decision the dropdown clears but the executed panel is kept."""
    _fresh_store(monkeypatch)
    app.action_fn("P001", "U001", "sess", "assign_task", "T002", "", "", "U003", "", "", "", "")
    rid = app.approvals_fn()[0]
    panel, banner, text, dropdown = app.decide_run(rid, "U001", "approve")
    assert "executed" in panel          # executed panel retained
    assert "Executed after approval" in banner
    assert dropdown.get("value") is None
    assert dropdown.get("choices") == []


# --- Whole UI ------------------------------------------------------------------


def test_build_ui_still_constructs():
    """The themed three-tab UI still builds offline."""
    ui = app.build_ui()
    assert isinstance(ui, gr.Blocks)
