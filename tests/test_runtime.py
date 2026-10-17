"""Final-phase tests: runtime lifecycle, sessions, context, trace integration."""

import asyncio
import dataclasses

import pytest

import agents as project_agents  # noqa: F401
import database
import models
import runtime
import workflows


def test_app_context_is_plain_frozen_data():
    """Context carries identity only: frozen, no connection, no secrets."""
    ctx = runtime.AppContext(user_id="U001", project_id="P001", role="Project Manager")
    assert "conn" not in ctx.__dict__ and "key" not in "".join(ctx.__dict__)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.user_id = "U002"  # type: ignore[misc]


def test_request_connection_closes():
    """Request connections are always closed after use."""
    with runtime.request_connection(":memory:") as conn:
        database.init_db(conn)
    with pytest.raises(Exception):
        conn.execute("SELECT 1")


def test_session_roundtrip_offline(tmp_path):
    """File-backed sessions persist items without any API calls."""
    db = tmp_path / "sess.db"
    session = runtime.get_session("user-1", db_path=db)
    assert session.session_id == "user-1"

    async def flow():
        await session.add_items([{"role": "user", "content": "hello"}])
        return await session.get_items()

    items = asyncio.run(flow())
    assert len(items) == 1
    again = asyncio.run(runtime.get_session("user-1", db_path=db).get_items())
    assert len(again) == 1


def test_new_session_ids_unique():
    """Minted session ids do not collide."""
    assert len({runtime.new_session_id() for _ in range(5)}) == 5


def test_session_ids_scoped_per_user_and_project(tmp_path):
    """Same raw id in different scopes yields isolated histories."""
    db = tmp_path / "scoped.db"

    async def flow():
        await runtime.get_session(runtime.scope_session_id("U001", "P001", "s"), db_path=db).add_items(
            [{"role": "user", "content": "p1 note"}])
        same = await runtime.get_session(runtime.scope_session_id("U001", "P001", "s"), db_path=db).get_items()
        other_project = await runtime.get_session(runtime.scope_session_id("U001", "P002", "s"), db_path=db).get_items()
        other_user = await runtime.get_session(runtime.scope_session_id("U002", "P001", "s"), db_path=db).get_items()
        return same, other_project, other_user

    same, other_project, other_user = asyncio.run(flow())
    assert len(same) == 1 and other_project == [] and other_user == []
    assert runtime.scope_session_id("U001", "P001", "s") != runtime.scope_session_id("U001", "P002", "s")


def test_resolve_role_from_records(conn):
    """Roles come from stored membership, never from message claims."""
    assert runtime.resolve_role(conn, "P001", "U001") == "Project Manager"
    assert runtime.resolve_role(conn, "P001", "U002") == "Developer"
    assert runtime.resolve_role(conn, "P001", "GHOST") is None
    assert runtime.resolve_role(conn, "P002", "U001") is None  # not a P002 member


def test_run_health_update_threads_session_and_context(tmp_path, monkeypatch):
    """Runtime entrypoint delivers session + AppContext to every agent step."""
    import seed_database

    db = tmp_path / "rt.db"
    seed_database.seed(db)
    seen = []

    async def fake(agent, content, output_type, session=None, context=None):
        seen.append((agent.name, session is not None, context))
        if agent.name == "Intake Agent":
            return models.ProjectUpdate(project_id="P002", user_id="U005", message="hi")
        if agent.name == "Task Worker":
            return models.TaskAnalysis(project_id="P002")
        if agent.name == "People Worker":
            return models.PeopleAnalysis(project_id="P002")
        if agent.name == "Deadline/Risk Worker":
            return models.DeadlineAnalysis(project_id="P002", deadline="d", days_remaining=60, risk_level="low")
        if agent.name == "Status Writer":
            return models.ProjectStatus(project_id="P002", health="green", progress_percent=80)
        return models.EvaluationResult(passed=True, iteration=1)

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(runtime.run_health_update(
        db, session_id="s-1", user_id="U005", project_id="P002", message="Weekly check."))
    assert result.error is None and result.status is not None
    assert seen and all(has_session for _, has_session, _ in seen)
    contexts = {c for _, _, c in seen}
    assert len(contexts) == 1
    only = contexts.pop()
    assert (only.user_id, only.project_id, only.role) == ("U005", "P002", "Developer")


def test_run_coordination_deterministic_without_llm(tmp_path):
    """WF-02 runtime path executes low-impact actions with no model involved."""
    import approval
    import seed_database

    db = tmp_path / "rt2.db"
    seed_database.seed(db)
    store = approval.ApprovalStore()
    res = asyncio.run(runtime.run_coordination(
        db, store, session_id="s-2", user_id="U001", project_id="P001",
        action_type="create_project_comment", values={"content": "ok"}))
    assert res.action_result is not None and res.action_result.success is True
