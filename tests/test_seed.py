"""Foundation tests: seeded demo data covers every required demo case."""

from datetime import date

import business_rules
import database
import seed_database


def test_seed_covers_required_demo_cases(tmp_path):
    """Seed provides normal/overdue/blocked/critical/completed tasks and varied team."""
    db = tmp_path / "seed.db"
    counts = seed_database.seed(db)
    assert counts == {"projects": 2, "tasks_P001": 6, "tasks_P002": 3, "members_P001": 4, "comments_P001": 2}

    conn = database.get_connection(db)
    try:
        tasks = database.list_tasks_by_project(conn, "P001")
        by_id = {t["id"]: t for t in tasks}
        assert by_id["T001"]["status"] == "in_progress"  # normal
        assert by_id["T002"]["status"] == "blocked" and by_id["T002"]["blocker"] == 1  # blocked
        assert by_id["T002"]["priority"] == "critical"  # critical
        assert by_id["T005"]["status"] == "completed" and by_id["T005"]["completion_percent"] == 100

        today = business_rules.utc_today().isoformat()
        overdue = [t for t in tasks if t["deadline"] and t["deadline"] < today and t["status"] != "completed"]
        assert len(overdue) >= 2  # T002, T004 drive routing/risk demos

        members = {m["id"]: m for m in database.list_team_members(conn, "P001")}
        assert members["U002"]["availability"] == "busy"
        assert members["U004"]["availability"] == "unavailable"
        assert members["U003"]["availability"] == "available"

        p1 = database.get_project(conn, "P001")
        days = (date.fromisoformat(p1["deadline"]) - business_rules.utc_today()).days
        assert days == 2  # approaching deadline for approval/escalation demos
    finally:
        conn.close()
