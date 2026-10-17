"""Phase 2 tests: all 8 domain tools, safety, and failure simulation."""

from datetime import date

import pytest

import database
import business_rules
import seed_database
import tools
from tools import ToolStatus


@pytest.fixture
def conn(tmp_path):
    """Seeded database in a temp file; each test gets a fresh copy."""
    db = tmp_path / "tools.db"
    seed_database.seed(db)
    c = database.get_connection(db)
    yield c
    c.close()


def _ids(rows, key="id"):
    return [r[key] for r in rows]


# --- Read tools ---


def test_get_project_tasks_valid(conn):
    """Valid project returns its full task records."""
    r = tools.get_project_tasks(conn, "P001")
    assert r.status == ToolStatus.SUCCESS and len(r.data) == 6
    t2 = next(t for t in r.data if t["id"] == "T002")
    assert (t2["status"], t2["priority"], t2["blocker"]) == ("blocked", "critical", 1)


def test_get_project_tasks_unknown_and_invalid(conn):
    """Unknown project is not-found; malformed ids are invalid; nothing is invented."""
    assert tools.get_project_tasks(conn, "NOPE").status == ToolStatus.NOT_FOUND
    assert tools.get_project_tasks(conn, "").status == ToolStatus.INVALID_INPUT
    assert tools.get_project_tasks(conn, None).status == ToolStatus.INVALID_INPUT


def test_get_task_valid_and_unknown(conn):
    """Exact record for a known task; safe not-found otherwise."""
    r = tools.get_task(conn, "T001")
    assert r.status == ToolStatus.SUCCESS and r.data["title"] == "Prepare launch checklist"
    assert tools.get_task(conn, "MISSING").status == ToolStatus.NOT_FOUND
    assert tools.get_task(conn, "  ").status == ToolStatus.INVALID_INPUT


def test_get_team_members_valid_and_unknown(conn):
    """Members carry availability/workload; unknown project is not-found."""
    r = tools.get_team_members(conn, "P001")
    assert r.status == ToolStatus.SUCCESS and len(r.data) == 4
    by_id = {m["id"]: m for m in r.data}
    assert by_id["U002"]["availability"] == "busy" and by_id["U002"]["current_workload"] == 5
    assert by_id["U004"]["availability"] == "unavailable"
    assert tools.get_team_members(conn, "NOPE").status == ToolStatus.NOT_FOUND


def test_get_project_deadline_valid_and_unknown(conn):
    """Deadline payload includes days remaining derived from the stored date."""
    r = tools.get_project_deadline(conn, "P001")
    assert r.status == ToolStatus.SUCCESS
    assert r.data["deadline"] == database.get_project(conn, "P001")["deadline"]
    expected = (date.fromisoformat(r.data["deadline"]) - business_rules.utc_today()).days
    assert r.data["days_remaining"] == expected == 2
    assert tools.get_project_deadline(conn, "NOPE").status == ToolStatus.NOT_FOUND


# --- create_task ---


def test_create_task_valid_persists(conn):
    """Valid creation persists and returns the stored row."""
    r = tools.create_task(conn, "P001", "New demo task", owner_id="U003")
    assert r.status == ToolStatus.SUCCESS
    assert database.get_task(conn, r.data["id"])["title"] == "New demo task"
    assert len(database.list_tasks_by_project(conn, "P001")) == 7


def test_create_task_rejects_bad_inputs(conn):
    """Invalid project/owner/title never mutate the database."""
    before = len(database.list_tasks_by_project(conn, "P001"))
    assert tools.create_task(conn, "NOPE", "Title").status == ToolStatus.NOT_FOUND
    assert tools.create_task(conn, "P001", "   ").status == ToolStatus.INVALID_INPUT
    assert tools.create_task(conn, "P001", "x" * 201).status == ToolStatus.INVALID_INPUT
    assert tools.create_task(conn, "P001", "Title", owner_id="GHOST").status == ToolStatus.NOT_FOUND
    assert len(database.list_tasks_by_project(conn, "P001")) == before


# --- update_task ---


def test_update_task_valid_persists(conn):
    """Allowed field updates persist; unrelated fields are untouched."""
    r = tools.update_task(conn, "T001", priority="high")
    assert r.status == ToolStatus.SUCCESS and r.data["priority"] == "high"
    stored = database.get_task(conn, "T001")
    assert stored["priority"] == "high" and stored["title"] == "Prepare launch checklist"


def test_update_task_rejects_bad_inputs(conn):
    """Unknown task, bad values, empty update, and bad dates are rejected cleanly."""
    assert tools.update_task(conn, "NOPE", priority="high").status == ToolStatus.NOT_FOUND
    assert tools.update_task(conn, "T001", status="done").status == ToolStatus.INVALID_INPUT
    assert tools.update_task(conn, "T001", priority="urgent").status == ToolStatus.INVALID_INPUT
    assert tools.update_task(conn, "T001").status == ToolStatus.INVALID_INPUT
    assert tools.update_task(conn, "T001", deadline="not-a-date").status == ToolStatus.INVALID_INPUT
    assert tools.update_task(conn, "T001", deadline="2026-13-99").status == ToolStatus.INVALID_INPUT


def test_update_task_completion_safety(conn):
    """Completion cannot be fabricated: 30%-complete T002 must be refused."""
    before = database.get_task(conn, "T002")
    r = tools.update_task(conn, "T002", status="completed")
    assert r.status == ToolStatus.INVALID_INPUT
    assert database.get_task(conn, "T002")["status"] == before["status"]  # unchanged


def test_update_task_completed_when_percent_100(conn):
    """A 100%-complete task (T005) may transition to completed."""
    r = tools.update_task(conn, "T005", status="completed")
    assert r.status == ToolStatus.SUCCESS and r.data["status"] == "completed"


# --- assign_task ---


def test_assign_task_valid_persists(conn):
    """Valid assignment persists and is confirmed from the stored row."""
    r = tools.assign_task(conn, "T001", "U003")
    assert r.status == ToolStatus.SUCCESS and r.data["owner_id"] == "U003"
    assert database.get_task(conn, "T001")["owner_id"] == "U003"


def test_assign_task_rejects_unknowns(conn):
    """Unknown task, unknown user, and cross-project user never mutate."""
    before = database.get_task(conn, "T001")["owner_id"]
    assert tools.assign_task(conn, "NOPE", "U003").status == ToolStatus.NOT_FOUND
    assert tools.assign_task(conn, "T001", "GHOST").status == ToolStatus.NOT_FOUND
    assert tools.assign_task(conn, "T001", "U005").status == ToolStatus.NOT_FOUND  # other project
    assert tools.assign_task(conn, "T001", "").status == ToolStatus.INVALID_INPUT
    assert database.get_task(conn, "T001")["owner_id"] == before


# --- create_project_comment ---


def test_create_comment_valid_persists(conn):
    """Valid comment persists and is returned."""
    r = tools.create_project_comment(conn, "P001", "Vendor fix verified.")
    assert r.status == ToolStatus.SUCCESS and r.data["content"] == "Vendor fix verified."
    assert len(database.list_comments(conn, "P001")) == 3


def test_create_comment_rejects_bad_inputs(conn):
    """Unknown project and blank/oversized content never persist."""
    before = len(database.list_comments(conn, "P001"))
    assert tools.create_project_comment(conn, "NOPE", "Hi").status == ToolStatus.NOT_FOUND
    assert tools.create_project_comment(conn, "P001", "   ").status == ToolStatus.INVALID_INPUT
    assert tools.create_project_comment(conn, "P001", "x" * 2001).status == ToolStatus.INVALID_INPUT
    assert len(database.list_comments(conn, "P001")) == before


# --- Failure handling ---


def test_simulated_failure_no_db_change(conn):
    """Armed tools fail deterministically without touching the database."""
    before = len(database.list_tasks_by_project(conn, "P001"))
    with tools.simulated_failure("get_task", "create_task"):
        assert tools.get_task(conn, "T001").status == ToolStatus.SIMULATED_FAILURE
        assert tools.create_task(conn, "P001", "Title").status == ToolStatus.SIMULATED_FAILURE
    assert len(database.list_tasks_by_project(conn, "P001")) == before
    # Disabled by default again after the block:
    assert tools.get_task(conn, "T001").status == ToolStatus.SUCCESS


def test_simulated_failure_all_write_tools(conn):
    """Every write tool honors the simulation hook."""
    with tools.simulated_failure("update_task", "assign_task", "create_project_comment"):
        assert tools.update_task(conn, "T001", priority="low").status == ToolStatus.SIMULATED_FAILURE
        assert tools.assign_task(conn, "T001", "U003").status == ToolStatus.SIMULATED_FAILURE
        assert tools.create_project_comment(conn, "P001", "Hi").status == ToolStatus.SIMULATED_FAILURE


def test_no_false_success_on_closed_connection(tmp_path):
    """A broken database surfaces DB_FAILURE, never success."""
    db = tmp_path / "broken.db"
    seed_database.seed(db)
    c = database.get_connection(db)
    c.close()
    assert tools.get_task(c, "T001").status == ToolStatus.DB_FAILURE
    assert tools.get_project_tasks(c, "P001").status == ToolStatus.DB_FAILURE
