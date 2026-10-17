"""Seed synthetic demo data for the AI Project Operations Agent (Project 4).

Only synthetic data (SDD Section 6.4). Running this script (re)creates
``data/projectops.db`` with coverage of every required demo case:

- P001 "Website Launch": deadline 2 days out (drives routing/approval demos).
- P002 "Mobile App": distant deadline (normal-path demos).
- Tasks: normal, overdue, blocked, critical, completed, high-priority.
- Team: available / busy / unavailable members with different workloads.
- Reference day: the documented application source
  ``business_rules.utc_today`` (UTC calendar day), so seeded relative
  deadlines always agree with the overdue/rule logic regardless of the
  server's local timezone.

Usage: ``python seed_database.py [--db PATH]``
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import business_rules
import database

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = REPO_ROOT / "data" / "projectops.db"


def _iso(day: date) -> str:
    return day.isoformat()


def seed(db_path: str | Path = DEFAULT_DB) -> dict[str, int]:
    """Recreate the database file and insert all demo rows. Returns row counts."""
    path = Path(db_path)
    if path.exists():
        path.unlink()
    conn = database.get_connection(path)
    try:
        database.init_db(conn)
        today = business_rules.utc_today()

        database.insert_project(conn, "P001", "Website Launch", _iso(today + timedelta(days=2)))
        database.insert_project(conn, "P002", "Mobile App", _iso(today + timedelta(days=60)))

        members = [
            # (id, project, name, role, availability, workload)
            ("U001", "P001", "Ahmed", "Project Manager", "available", 2),
            ("U002", "P001", "Sara", "Developer", "busy", 5),
            ("U003", "P001", "Layla", "Developer", "available", 1),
            ("U004", "P001", "Omar", "Tester", "unavailable", 0),
            ("U005", "P002", "Noor", "Developer", "available", 2),
            ("U006", "P002", "Khalid", "Designer", "busy", 4),
        ]
        for m in members:
            database.insert_team_member(conn, *m)

        tasks = [
            # (id, project, title, status, priority, owner, deadline, pct, blocker)
            ("T001", "P001", "Prepare launch checklist", "in_progress", "medium", "U001", today, 60, False),
            ("T002", "P001", "Fix payment gateway blocker", "blocked", "critical", "U002", today - timedelta(days=7), 30, True),
            ("T003", "P001", "Write release notes", "todo", "high", "U003", today + timedelta(days=1), 0, False),
            ("T004", "P001", "Migrate legacy content", "in_progress", "medium", "U002", today - timedelta(days=1), 40, False),
            ("T005", "P001", "Design landing page", "completed", "low", "U003", today - timedelta(days=10), 100, False),
            ("T006", "P001", "Security review", "todo", "critical", "U002", today + timedelta(days=2), 0, False),
            ("T007", "P002", "Prototype onboarding flow", "in_progress", "medium", "U005", today + timedelta(days=20), 50, False),
            ("T008", "P002", "User interviews", "todo", "low", "U006", today + timedelta(days=30), 0, False),
            ("T009", "P002", "Setup CI pipeline", "completed", "medium", "U005", today - timedelta(days=5), 100, False),
        ]
        for tid, pid, title, status, prio, owner, dl, pct, blk in tasks:
            database.insert_task(
                conn, tid, pid, title, status, prio,
                owner_id=owner, deadline=_iso(dl), completion_percent=pct, blocker=blk,
            )

        database.insert_comment(conn, "C001", "P001", "Ahmed", "Gateway vendor confirmed a fix window tomorrow.", _iso(today))
        database.insert_comment(conn, "C002", "P001", "Sara", "Waiting on vendor before retrying the failing test.", _iso(today))

        counts = {
            "projects": len(database.list_projects(conn)),
            "tasks_P001": len(database.list_tasks_by_project(conn, "P001")),
            "tasks_P002": len(database.list_tasks_by_project(conn, "P002")),
            "members_P001": len(database.list_team_members(conn, "P001")),
            "comments_P001": len(database.list_comments(conn, "P001")),
        }
        return counts
    finally:
        conn.close()


def main() -> None:
    """CLI entry point: seed the default (or given) database and print counts."""
    parser = argparse.ArgumentParser(description="Seed the Project 4 demo database.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Target SQLite file")
    args = parser.parse_args()
    counts = seed(args.db)
    print(f"Seeded {args.db}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
