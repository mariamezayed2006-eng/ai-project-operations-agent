"""Foundation tests: database creation and CRUD round-trips."""

import sqlite3

import pytest

import database


@pytest.fixture
def conn(tmp_path):
    """Fresh in-memory-style database backed by a temp file."""
    c = database.get_connection(tmp_path / "test.db")
    database.init_db(c)
    yield c
    c.close()


def test_schema_tables_created(conn):
    """init_db creates exactly the Phase 1 tables plus project memory."""
    assert sorted(database.table_names(conn)) == ["comments", "project_memory", "projects", "tasks", "team_members"]


def test_init_db_idempotent(conn):
    """Re-running init_db on an existing database is safe."""
    database.init_db(conn)
    assert sorted(database.table_names(conn)) == ["comments", "project_memory", "projects", "tasks", "team_members"]


def test_project_memory_roundtrip_and_unknown(conn):
    """Durable memory upserts per project; unknown projects read None."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    assert database.get_project_memory(conn, "P1") is None
    database.upsert_project_memory(conn, "P1", "yellow 55% | blockers: 1", "yellow", "2026-10-12")
    stored = database.get_project_memory(conn, "P1")
    assert stored["summary"] == "yellow 55% | blockers: 1" and stored["updated_at"] == "2026-10-12"
    database.upsert_project_memory(conn, "P1", "green 80% | blockers: 0", "green", "2026-10-13")
    assert database.get_project_memory(conn, "P1")["health"] == "green"
    assert database.get_project_memory(conn, "NOPE") is None


def test_project_roundtrip(conn):
    """Inserted projects are retrievable; missing ids return None."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    assert database.get_project(conn, "P1")["name"] == "Alpha"
    assert database.get_project(conn, "NOPE") is None
    assert [p["id"] for p in database.list_projects(conn)] == ["P1"]


def test_task_roundtrip_and_unknown(conn):
    """Tasks round-trip; unknown ids return None (unavailable evidence)."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    database.insert_task(conn, "T1", "P1", "Do thing", "todo", "high", owner_id="U1")
    task = database.get_task(conn, "T1")
    assert task["title"] == "Do thing" and task["blocker"] == 0
    assert database.get_task(conn, "MISSING") is None
    assert [t["id"] for t in database.list_tasks_by_project(conn, "P1")] == ["T1"]


def test_members_and_comments_roundtrip(conn):
    """Members and comments round-trip per project."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    database.insert_team_member(conn, "U1", "P1", "Sara", "Developer", "busy", 5)
    database.insert_comment(conn, "C1", "P1", "Ahmed", "Note", "2026-10-08")
    assert database.list_team_members(conn, "P1")[0]["availability"] == "busy"
    assert database.list_comments(conn, "P1")[0]["content"] == "Note"


def test_completion_percent_check_constraint(conn):
    """Completion outside 0-100 is rejected by the database."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    with pytest.raises(database.DatabaseError):
        database.insert_task(conn, "TBAD", "P1", "Bad", "todo", "low", completion_percent=101)


def test_duplicate_project_raises_database_error(conn):
    """Duplicate primary keys surface as DatabaseError, not raw sqlite errors."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    with pytest.raises(database.DatabaseError):
        database.insert_project(conn, "P1", "Alpha", "2026-10-10")


def test_connection_usable_from_worker_threads(tmp_path):
    """SDK tool threads share the workflow connection; cross-thread use must work."""
    import threading

    import tools as domain_tools

    db = tmp_path / "threads.db"
    c = database.get_connection(db)
    database.init_db(c)
    database.insert_project(c, "P1", "Alpha", "2026-10-10")
    database.insert_task(c, "T1", "P1", "Do thing", "blocked", "critical", blocker=True)
    results = []
    workers = [threading.Thread(target=lambda: results.append(domain_tools.get_task(c, "T1"))) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert len(results) == 4
    assert all(r.status == domain_tools.ToolStatus.SUCCESS for r in results)
    assert all(r.data["id"] == "T1" for r in results)
    c.close()


def test_failed_write_rolls_back_and_connection_stays_usable(conn):
    """A rejected write leaves prior rows intact and later writes succeed."""
    database.insert_project(conn, "P1", "Alpha", "2026-10-10")
    database.insert_task(conn, "T1", "P1", "Keep me", "todo", "low")
    with pytest.raises(database.DatabaseError):
        database.insert_task(conn, "T1", "P1", "Duplicate", "todo", "low")
    assert database.get_task(conn, "T1")["title"] == "Keep me"
    database.insert_task(conn, "T2", "P1", "After failure", "todo", "low")
    assert database.get_task(conn, "T2")["title"] == "After failure"


def test_concurrent_mixed_read_write_keeps_exact_state(tmp_path):
    """Readers and distinct-row writers run together; final content is exact."""
    import threading

    import tools as domain_tools

    db = tmp_path / "mixed.db"
    c = database.get_connection(db)
    database.init_db(c)
    database.insert_project(c, "P1", "Alpha", "2026-10-10")
    for i in range(6):
        database.insert_task(c, f"T{i}", "P1", f"Task {i}", "todo", "low", completion_percent=10 * i)
    errors = []

    def writer(i):
        try:
            ok = database.update_task_fields(c, f"T{i}", {"status": "in_progress", "completion_percent": 50})
            assert ok
        except Exception as exc:  # noqa: BLE001  (collected, asserted below)
            errors.append(exc)

    def reader():
        try:
            for _ in range(30):
                rows = domain_tools.get_project_tasks(c, "P1")
                assert rows.status == domain_tools.ToolStatus.SUCCESS
                for row in rows.data:
                    assert row["id"].startswith("T") and 0 <= row["completion_percent"] <= 100
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for w in threads:
        w.start()
    for w in threads:
        w.join()
    assert errors == []
    final = {t["id"]: (t["status"], t["completion_percent"]) for t in database.list_tasks_by_project(c, "P1")}
    assert final == {f"T{i}": ("in_progress", 50) for i in range(6)}
    c.close()
