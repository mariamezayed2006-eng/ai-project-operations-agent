"""Foundation tests: deterministic approval, completion, write, and health rules."""

import pytest
from datetime import date, datetime, timezone

import business_rules as br


def test_approval_each_trigger_independently():
    """Each OR branch of the approval rule fires on its own."""
    assert br.needs_human_approval(True, 30, False, False, False)  # critical blocker
    assert br.needs_human_approval(False, 2, True, False, False)  # deadline edge + commitment
    assert br.needs_human_approval(False, 30, False, True, False)  # priority change
    assert br.needs_human_approval(False, 30, False, False, True)  # reassignment


def test_approval_negative_cases():
    """No trigger (or deadline pressure without commitment impact) means no approval."""
    assert not br.needs_human_approval(False, 30, False, False, False)
    assert not br.needs_human_approval(False, 2, False, False, False)
    assert not br.needs_human_approval(False, 3, True, False, False)  # 3 days is outside the <= 2 window
    assert not br.needs_human_approval(False, None, True, False, False)


def test_completion_rule_combinations():
    """Completion requires existence + 100% + evidence, all three."""
    assert br.can_mark_completed(True, 100, True)
    assert not br.can_mark_completed(False, 100, True)
    assert not br.can_mark_completed(True, 99, True)
    assert not br.can_mark_completed(True, 100, False)


def test_write_rule():
    """Success claims need a confirmation; failures do not."""
    assert br.is_success_claim_valid(True, "update_task success=true")
    assert not br.is_success_claim_valid(True, "")
    assert not br.is_success_claim_valid(True, None)
    assert br.is_success_claim_valid(False, "")


def test_health_red_cases():
    """Red: critical blocker, 2+ overdue, or near deadline with blocked/overdue work."""
    assert br.derive_health(True, 0, 0, 0, 30) == "red"
    assert br.derive_health(False, 0, 2, 0, 30) == "red"
    assert br.derive_health(False, 1, 0, 0, 2) == "red"
    assert br.derive_health(False, 0, 1, 0, 1) == "red"


def test_health_yellow_and_green_cases():
    """Yellow: any blocked/overdue/critical or deadline within 7 days. Else green."""
    assert br.derive_health(False, 1, 0, 0, 30) == "yellow"
    assert br.derive_health(False, 0, 1, 0, 30) == "yellow"
    assert br.derive_health(False, 0, 0, 1, 30) == "yellow"
    assert br.derive_health(False, 0, 0, 0, 7) == "yellow"
    assert br.derive_health(False, 0, 0, 0, 30) == "green"
    assert br.derive_health(False, 0, 0, 0, None) == "green"


def test_health_rejects_negative_counts():
    """Negative evidence counts fail fast."""
    with pytest.raises(ValueError):
        br.derive_health(False, -1, 0, 0, 30)


def test_utc_today_is_current_utc_day():
    """Single reference-date source matches UTC (not server local date)."""
    assert br.utc_today() == datetime.now(timezone.utc).date()


def test_derive_overdue_past_today_future():
    """Past-due (not completed) counts; today/future/completed/missing/malformed do not."""
    rows = [
        {"id": "PAST", "deadline": "2026-10-01", "status": "in_progress"},
        {"id": "TODAY", "deadline": "2026-10-11", "status": "todo"},
        {"id": "FUTURE", "deadline": "2026-12-01", "status": "todo"},
        {"id": "DONE", "deadline": "2026-01-01", "status": "completed"},
        {"id": "NODEADLINE", "deadline": None, "status": "todo"},
        {"id": "BADDATE", "deadline": "not-a-date", "status": "todo"},
        {"id": "NOID", "deadline": "2026-01-01", "status": "todo"},
    ]
    rows[-1].pop("id")
    assert br.derive_overdue_task_ids(rows, date(2026, 10, 11)) == ["PAST"]


def test_derive_blocker_entries_from_rows():
    """Blocked status or flag yields id/status/priority entries; id-less rows skipped."""
    rows = [
        {"id": "B1", "status": "blocked", "priority": "critical"},
        {"id": "F1", "status": "in_progress", "priority": "low", "blocker": True},
        {"id": "OK", "status": "todo", "priority": "medium"},
        {"status": "blocked", "priority": "high"},
    ]
    entries = br.derive_blocker_entries(rows)
    assert len(entries) == 2
    assert any("B1" in e and "blocked" in e and "critical" in e for e in entries)
    assert any(e.startswith("F1") for e in entries)


def test_derive_risk_entries_and_material_ids():
    """Blocked/critical/overdue rows produce factual risk lines and material ids."""
    rows = [
        {"id": "B1", "status": "blocked", "priority": "critical", "deadline": "2026-10-01"},
        {"id": "OK", "status": "todo", "priority": "low", "deadline": "2026-12-01"},
    ]
    risks = br.derive_risk_entries(rows, date(2026, 10, 11))
    assert any("B1 is blocked" in r for r in risks)
    assert any("critical priority" in r for r in risks)
    assert any("overdue as of 2026-10-11" in r and "B1" in r for r in risks)
    assert br.derive_material_task_ids(rows, date(2026, 10, 11)) == {"B1"}
    assert br.derive_risk_entries([{"id": "OK", "status": "todo", "priority": "low"}], date(2026, 10, 11)) == []


def _healthy(**over):
    base = dict(critical_blocked_rows=False, accepted=True, rows_available=True,
                workers_complete=True, deadline_days_remaining=30, action_affects_commitment=False)
    base.update(over)
    return base


def test_has_critical_blocked():
    """Only blocked-and-critical rows count (T002 pattern)."""
    assert br.has_critical_blocked([{"id": "T002", "status": "blocked", "priority": "critical"}]) is True
    assert br.has_critical_blocked([{"id": "T1", "status": "blocked", "priority": "low"}]) is False
    assert br.has_critical_blocked([{"id": "T6", "status": "todo", "priority": "critical"}]) is False
    assert br.has_critical_blocked([]) is False


def test_needs_health_review_branches():
    """Each review trigger fires independently; the healthy case stays normal."""
    assert br.needs_health_review(**_healthy()) is False
    assert br.needs_health_review(**_healthy(critical_blocked_rows=True)) is True
    assert br.needs_health_review(**_healthy(accepted=False)) is True
    assert br.needs_health_review(**_healthy(rows_available=False)) is True
    assert br.needs_health_review(**_healthy(workers_complete=False)) is True
    assert br.needs_health_review(**_healthy(deadline_days_remaining=2, action_affects_commitment=True)) is True
    assert br.needs_health_review(**_healthy(deadline_days_remaining=2, action_affects_commitment=False)) is False
    assert br.needs_health_review(**_healthy(deadline_days_remaining=30, action_affects_commitment=True)) is False
