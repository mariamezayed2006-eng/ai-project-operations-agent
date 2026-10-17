"""Foundation tests: Pydantic model validation for all 7 SDD models."""

import pytest
from pydantic import ValidationError

import models


def test_project_update_valid():
    """A normal intake object validates."""
    u = models.ProjectUpdate(project_id="P001", user_id="U001", message="Update: gateway blocked")
    assert u.blockers == [] and u.requested_actions == []


def test_project_update_rejects_extra_fields():
    """Unknown fields fail loudly instead of being silently kept."""
    with pytest.raises(ValidationError):
        models.ProjectUpdate(project_id="P", user_id="U", message="m", unknown_field="x")


def test_project_status_valid_and_progress_bounds():
    """Valid status passes; progress outside 0-100 is rejected."""
    s = models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=60,
        blockers=["gateway"], evidence_task_ids=["T002"],
    )
    assert s.health == models.Health.YELLOW
    with pytest.raises(ValidationError):
        models.ProjectStatus(project_id="P001", health="green", progress_percent=101)


def test_project_status_rejects_bad_health_and_missing_evidence():
    """Health is a closed set; task claims require evidence ids."""
    with pytest.raises(ValidationError):
        models.ProjectStatus(project_id="P001", health="orange", progress_percent=10)
    with pytest.raises(ValidationError):
        models.ProjectStatus(
            project_id="P001", health="red", progress_percent=10,
            blockers=["gateway"], evidence_task_ids=[],
        )


def test_action_result_success_needs_confirmation():
    """Success without a tool confirmation is rejected (SDD write rule)."""
    ok = models.ActionResult(
        action_type="assign_task", target_id="T002", success=True,
        confirmation="assign_task success=true", message="Assigned",
    )
    assert ok.success
    with pytest.raises(ValidationError):
        models.ActionResult(action_type="assign_task", target_id="T002", success=True, confirmation=" ")


def test_evaluation_result_failure_needs_guidance_and_bounded_iteration():
    """Failed evaluations must explain; iterations cap at 2."""
    bad = models.EvaluationResult(passed=False, issues=["unsupported claim"], iteration=1)
    assert not bad.passed
    with pytest.raises(ValidationError):
        models.EvaluationResult(passed=False, iteration=1)  # no guidance
    with pytest.raises(ValidationError):
        models.EvaluationResult(passed=True, iteration=3)


def test_worker_analyses_validate():
    """Worker evidence models accept typed findings."""
    t = models.TaskAnalysis(project_id="P001", blocked_task_ids=["T002"], findings=["gateway blocked"])
    p = models.PeopleAnalysis(project_id="P001", unavailable_member_ids=["U004"])
    d = models.DeadlineAnalysis(
        project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high",
    )
    assert t.blocked_task_ids == ["T002"] and p.unavailable_member_ids == ["U004"]
    assert d.risk_level == models.RiskLevel.HIGH
