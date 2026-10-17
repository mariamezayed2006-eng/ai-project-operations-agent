"""Phase 4 tests: patterns, WF-01, WF-02. No live model/API calls.

LLM steps are faked by replacing ``workflows.run_agent_step``; WF-02 and
all routing/merge logic run for real against a seeded temp database.
"""

import asyncio
import json
from datetime import date

import pytest

import agents as project_agents  # noqa: F401  (registers agents_sdk alias)
import agents_sdk
import database
import models
import seed_database
import tools
import workflows
from tools import ToolStatus


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded database per test."""
    db = tmp_path / "wf.db"
    seed_database.seed(db)
    c = database.get_connection(db)
    yield c
    c.close()


def make_update(message="Weekly check, all on track.", **kw):
    """Build a ProjectUpdate with safe defaults."""
    return models.ProjectUpdate(project_id="P001", user_id="U001", message=message, **kw)


def canned_status(**kw):
    return models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=55,
        blockers=["gateway blocked"], overdue_tasks=["T002"],
        evidence_task_ids=["T002"], **kw,
    )


def install_fake(monkeypatch, mapping):
    """Fake run_agent_step: dispatch canned values by agent name, log order."""
    log = []

    async def fake(agent, content, output_type, *args, **kwargs):
        log.append(agent.name)
        value = mapping[agent.name]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(agent, content)
        return value

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    return log


def standard_mapping(**over):
    base = {
        "Intake Agent": make_update(),
        "Task Worker": models.TaskAnalysis(project_id="P001", blocked_task_ids=["T002"], findings=["t"]),
        "People Worker": models.PeopleAnalysis(project_id="P001", unavailable_member_ids=["U004"]),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"
        ),
        "Status Writer": canned_status(),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
        "Project Orchestrator": "orchestrated summary",
    }
    base.update(over)
    return base


# --- PATTERN: Routing (pure, deterministic) ---


def test_route_normal_for_routine_update():
    """Routine message with no actions stays on the automated path."""
    assert workflows.route_update(make_update(), 30) == workflows.ROUTE_NORMAL


def test_route_review_triggers():
    """Critical text, assignment, priority escalation, and near deadline each route to review."""
    assert workflows.route_update(make_update("Critical blocker on gateway"), 30) == workflows.ROUTE_REVIEW
    assign = make_update("Please reassign.", requested_actions=[
        models.RequestedAction(action_type="assign_task", task_id="T001")])
    assert workflows.route_update(assign, 30) == workflows.ROUTE_REVIEW
    prio = make_update("Escalate priority.", requested_actions=[
        models.RequestedAction(action_type="update_task", task_id="T001", values={"priority": "critical"})])
    assert workflows.route_update(prio, 30) == workflows.ROUTE_REVIEW
    plain_action = make_update("Add task.", requested_actions=[
        models.RequestedAction(action_type="create_task", values={"title": "Docs"})])
    assert workflows.route_update(plain_action, 2) == workflows.ROUTE_REVIEW  # deadline edge + commitment


def test_route_negative_edges():
    """Day-3 and no-commitment cases stay normal (matches business-rule edges)."""
    plain_action = make_update("Add task.", requested_actions=[
        models.RequestedAction(action_type="create_task", values={"title": "Docs"})])
    assert workflows.route_update(plain_action, 3) == workflows.ROUTE_NORMAL
    assert workflows.route_update(plain_action, None) == workflows.ROUTE_NORMAL


def test_update_risk_flags_conservative():
    """Any task mutation counts as commitment-affecting before evidence arrives."""
    flags = workflows.update_risk_flags(make_update("x", requested_actions=[
        models.RequestedAction(action_type="create_project_comment", values={"content": "hi"})]))
    assert flags["action_affects_commitment"] is False
    assert flags["critical_blocker"] is False


# --- PATTERN: Parallelization ---


def test_gather_runs_workers_concurrently(conn, monkeypatch):
    """Task and People analyses overlap in time (true asyncio.gather fan-out)."""
    entered = []
    release = asyncio.Event()

    async def fake_task(c, p, *a):
        entered.append("task")
        await release.wait()
        return models.TaskAnalysis(project_id=p)

    async def fake_people(c, p, *a):
        entered.append("people")
        await release.wait()
        return models.PeopleAnalysis(project_id=p)

    async def fake_deadline(c, p, *a):
        return models.DeadlineAnalysis(project_id=p, deadline="d", days_remaining=9, risk_level="low")

    monkeypatch.setattr(workflows, "run_task_analysis", fake_task)
    monkeypatch.setattr(workflows, "run_people_analysis", fake_people)
    monkeypatch.setattr(workflows, "run_deadline_analysis", fake_deadline)

    async def driver():
        job = asyncio.create_task(workflows.gather_evidence(conn, "P001"))
        await asyncio.sleep(0.05)
        assert set(entered) == {"task", "people"}  # both started, neither finished
        release.set()
        return await job

    bundle = asyncio.run(driver())
    assert bundle.task is not None and bundle.people is not None and bundle.unavailable == []


def test_gather_failure_becomes_unavailable(conn, monkeypatch):
    """A failed worker is recorded as unavailable; others still merge."""

    async def boom(c, p, *a):
        raise RuntimeError("model down")

    async def ok_people(c, p, *a):
        return models.PeopleAnalysis(project_id=p)

    async def ok_deadline(c, p, *a):
        return models.DeadlineAnalysis(project_id=p, deadline="d", days_remaining=9, risk_level="low")

    monkeypatch.setattr(workflows, "run_task_analysis", boom)
    monkeypatch.setattr(workflows, "run_people_analysis", ok_people)
    monkeypatch.setattr(workflows, "run_deadline_analysis", ok_deadline)
    bundle = asyncio.run(workflows.gather_evidence(conn, "P001"))
    assert bundle.task is None and bundle.people is not None
    assert any(u.startswith("task:") for u in bundle.unavailable)


def test_gather_subset_selection(conn, monkeypatch):
    """Only requested specialists run; the rest are marked unavailable."""
    calls = []

    async def fake(c, p, *a):
        calls.append(p)
        return models.TaskAnalysis(project_id=p)

    monkeypatch.setattr(workflows, "run_task_analysis", fake)
    bundle = asyncio.run(workflows.gather_evidence(conn, "P001", include=("task",)))
    assert bundle.task is not None and bundle.people is None and bundle.deadline is None
    assert len(bundle.unavailable) == 2


def test_merge_evidence_marks_failures():
    """WorkerFailure objects become explicit unavailable entries, never merged."""
    bundle = workflows.merge_evidence(
        workflows.WorkerFailure("task", "timeout"), models.PeopleAnalysis(project_id="P"), None
    )
    assert bundle.task is None and bundle.people is not None
    assert "task: timeout" in bundle.unavailable and "deadline: no result returned" in bundle.unavailable


# --- PATTERN: Orchestrator-Workers ---


def test_attach_workers_uses_as_tool_and_preserves_bases(conn):
    """Orchestrator copy gains 3 worker tools; shared base agents stay tool-free."""
    bound = workflows.bind_worker_agents(conn)
    assert set(bound) == {"task_worker", "people_worker", "deadline_worker"}
    assert [t.name for t in bound["task_worker"].tools] == ["get_project_tasks", "get_task"]
    assert [t.name for t in bound["people_worker"].tools] == ["get_team_members"]
    assert [t.name for t in bound["deadline_worker"].tools] == ["get_project_deadline", "get_project_tasks"]
    orch = workflows.attach_workers_to_orchestrator(bound)
    assert sorted(t.name for t in orch.tools) == ["deadline_analysis", "people_analysis", "task_analysis"]
    assert project_agents.AGENTS["project_orchestrator"].tools == []
    assert project_agents.AGENTS["task_worker"].tools == []


def test_worker_output_collector_captures_structured_json():
    """Extractors stash typed worker output for single-execution capture."""

    collector, extractors = workflows.make_worker_output_collector()
    canned = models.TaskAnalysis(project_id="P001", blocked_task_ids=["T002"])

    class FakeResult:
        def final_output_as(self, cls):
            assert cls is models.TaskAnalysis
            return canned

    payload = extractors["task_worker"](FakeResult())
    assert json.loads(payload)["blocked_task_ids"] == ["T002"]
    assert collector["task_worker"] == payload


# --- PATTERN: Evaluator-Optimizer ---


def test_revise_accepts_first_draft():
    """Passing evaluation stops after one iteration with no feedback."""
    seen = []

    async def make(fb):
        seen.append(fb)
        return canned_status()

    async def ev(d):
        return models.EvaluationResult(passed=True, iteration=1)

    draft, outcome = asyncio.run(workflows.revise_with_evaluator(make, ev))
    assert outcome.accepted and outcome.iterations == 1 and seen == [None] and draft is not None


def test_revise_reject_then_accept_passes_feedback():
    """Rejection feedback reaches the writer; second pass can succeed."""
    feedbacks = []
    calls = {"n": 0}

    async def make(fb):
        feedbacks.append(fb)
        return canned_status()

    async def ev(d):
        calls["n"] += 1
        if calls["n"] == 1:
            return models.EvaluationResult(passed=False, issues=["unsupported owner"], revision_instructions="drop owner", iteration=1)
        return models.EvaluationResult(passed=True, iteration=2)

    draft, outcome = asyncio.run(workflows.revise_with_evaluator(make, ev))
    assert outcome.accepted and outcome.iterations == 2
    assert "drop owner" in feedbacks[1] and "unsupported owner" in feedbacks[1]


def test_revise_double_reject_exits_safely():
    """Two rejections stop the loop; safest draft returns flagged for review."""

    async def make(fb):
        return canned_status()

    async def ev(d):
        return models.EvaluationResult(passed=False, issues=["bad"], iteration=2)

    draft, outcome = asyncio.run(workflows.revise_with_evaluator(make, ev))
    assert not outcome.accepted and outcome.iterations == 2
    assert outcome.warning and draft is not None and draft.needs_human_approval is True


def test_revise_writer_error_is_safe():
    """A crashing writer yields an error outcome, never a fabricated status."""

    async def make(fb):
        raise RuntimeError("model down")

    async def ev(d):  # pragma: no cover
        return models.EvaluationResult(passed=True, iteration=1)

    draft, outcome = asyncio.run(workflows.revise_with_evaluator(make, ev))
    assert draft is None and outcome.error and "draft failed" in outcome.error


def test_run_agent_step_surfaces_invalid_output(monkeypatch):
    """Transport/validation failures propagate so chains degrade safely."""

    class Dummy:
        def final_output_as(self, cls):
            raise ValueError("bad json")

    async def fake_run(agent, content, **kw):
        return Dummy()

    monkeypatch.setattr(agents_sdk.Runner, "run", fake_run)
    with pytest.raises(ValueError):
        asyncio.run(workflows.run_agent_step(project_agents.AGENTS["intake"], "hi", models.ProjectUpdate))


def test_writer_model_behavior_error_becomes_safe_envelope(conn, monkeypatch):
    """A live-style ModelBehaviorError at draft time yields an error result, never a crash."""
    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001"),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": agents_sdk.ModelBehaviorError("Invalid JSON when parsing model output"),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }
    log = []

    async def fake(agent, content, output_type, *args, **kwargs):
        log.append(agent.name)
        value = mapping[agent.name]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.status is None and result.error == "draft failed: ModelBehaviorError"
    assert "Status Writer" in log  # chain reached the writer before failing safely


def test_malformed_evaluator_output_becomes_safe_envelope():
    """A non-EvaluationResult verdict cannot crash the loop or fake acceptance."""

    async def make(fb):
        return models.ProjectStatus(project_id="P001", health="green", progress_percent=80)

    async def evaluate(draft):
        return "looks good to me"

    draft, outcome = asyncio.run(workflows.revise_with_evaluator(make, evaluate))
    assert draft is None and not outcome.accepted
    assert outcome.error == "evaluation produced invalid output"


def test_evidence_text_contains_reference_date_and_deadlines():
    """Writer input carries the explicit as_of day and absolute task deadlines."""
    bundle = workflows.EvidenceBundle(task=models.TaskAnalysis(project_id="P001"))
    update = models.ProjectUpdate(project_id="P001", user_id="U001", message="hi")
    rows = [{"id": "T002", "deadline": "2026-10-02", "status": "blocked",
             "priority": "critical", "completion_percent": 30}]
    text = workflows.render_evidence_text(bundle, update, as_of_date=date(2026, 10, 11), task_rows=rows)
    assert "2026-10-11" in text and "do not guess" in text.lower()
    assert "T002: deadline 2026-10-02" in text


def test_wf01_overdue_constructed_from_db_not_writer(conn, monkeypatch):
    """Authoritative overdue ids replace writer prose; evidence citations extended."""
    from tools import ToolResult

    writer_status = models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=55,
        overdue_tasks=["T999", "unavailable (current date not provided)"],
        evidence_task_ids=["T001"])
    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001", blocked_task_ids=["T002"]),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": writer_status,
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        if agent.name == "Status Writer":
            assert "as_of_date" in content  # writer saw the reference day
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    # Seeded rows are relative to today: T002 (today-7) and T004 (today-1)
    # overdue, T005 completed excluded — stable on any run day.
    assert result.status.overdue_tasks == ["T002", "T004"]
    assert {"T002", "T004"} <= set(result.status.evidence_task_ids)
    assert "T999" not in result.status.overdue_tasks


def test_wf01_sentence_id_rejected_when_rows_unavailable(conn, monkeypatch):
    """Without authoritative rows a sentence-as-ID still fails closed via guardrail."""
    from tools import ToolResult

    def failing_tasks(c, pid):
        return ToolResult(status=ToolStatus.DB_FAILURE, message="down")

    writer_status = models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=55,
        overdue_tasks=["unavailable (current date not provided)"],
        evidence_task_ids=["T002"])
    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001"),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": writer_status,
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    monkeypatch.setattr(workflows.tools, "get_project_tasks", failing_tasks)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.status is None and result.error is not None and "guardrail" in result.error
    """Wrong-typed worker results merge as unavailable, never as evidence."""

    mapping = {
        "Task Worker": models.TaskAnalysis(project_id="P001"),
        "People Worker": "not a PeopleAnalysis",
        "Deadline/Risk Worker": None,
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    bundle = asyncio.run(workflows.gather_evidence(conn, "P001"))
    assert bundle.task is not None and bundle.people is None and bundle.deadline is None
    assert any(u.startswith("people:") for u in bundle.unavailable)
    assert any(u.startswith("deadline:") for u in bundle.unavailable)


def passing_evaluator(iteration=1):
    return models.EvaluationResult(passed=True, iteration=iteration)


def test_facts_visible_to_evaluator_before_verdict(conn, monkeypatch):
    """The evaluator judges constructed facts, not raw writer prose."""
    judged = []
    writer_status = models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55)

    def evaluator(agent, content):
        draft_json = content.split("Draft: ", 1)[1].split("\nEvidence:", 1)[0]
        judged.append(json.loads(draft_json))
        return passing_evaluator()

    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001", blocked_task_ids=["T002"]),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": writer_status,
        "Evaluator": evaluator,
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        value = mapping[agent.name]
        return value(agent, content) if callable(value) else value

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.accepted
    assert any("T002" in b for b in judged[0]["blockers"])
    assert set(judged[0]["overdue_tasks"]) == {"T002", "T004"}
    assert judged[0]["risks"] != []


def _wf01_mapping(writer_status, evaluator_value):
    return {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001", blocked_task_ids=["T002"]),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": writer_status,
        "Evaluator": evaluator_value,
    }


def _dispatching_fake(mapping, log=None):
    async def fake(agent, content, output_type, *args, **kwargs):
        if log is not None:
            log.append(agent.name)
        value = mapping[agent.name]
        if isinstance(value, Exception):
            raise value
        return value(agent, content) if callable(value) else value
    return fake


def test_t002_named_blocked_critical_despite_empty_writer(conn, monkeypatch):
    """Constructed blockers name T002 with status/priority from rows alone."""
    writer_status = models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55)
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, passing_evaluator())))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert any("T002" in b and "blocked" in b and "critical" in b for b in result.status.blockers)


def test_writer_risks_merged_and_recommendations_preserved(conn, monkeypatch):
    """Deterministic risks prepend; writer prose and recommendations kept verbatim."""
    writer_status = models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=55,
        risks=["Vendor delay may slip T003"],
        recommended_actions=["Review vendor fix for T002"],
        evidence_task_ids=["T002", "T003"])
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, passing_evaluator())))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert "Vendor delay may slip T003" in result.status.risks
    assert any("T002" in r for r in result.status.risks)  # deterministic entries present
    assert result.status.recommended_actions == ["Review vendor fix for T002"]


def test_invented_recommendation_id_still_blocked(conn, monkeypatch):
    """A fabricated T999 in recommendations fails closed despite construction."""
    writer_status = models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=55,
        recommended_actions=["Contact T999 owner immediately"],
        evidence_task_ids=["T002"])
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, passing_evaluator())))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.status is None and result.error is not None and "guardrail" in result.error


def test_guardrail_blocked_path_records_evidence_path(conn, monkeypatch):
    """Even fail-closed results report which evidence engine ran."""
    writer_status = models.ProjectStatus(
        project_id="P001", health="yellow", progress_percent=55,
        recommended_actions=["Contact T999 owner immediately"],
        evidence_task_ids=["T002"])
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, passing_evaluator())))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.status is None and "guardrail" in (result.error or "")
    assert result.evidence_path == "parallel"


def test_no_rows_means_no_fabricated_facts(conn, monkeypatch):
    """Unavailable rows leave writer emptiness intact; nothing invented."""
    from tools import ToolResult

    def failing_tasks(c, pid):
        return ToolResult(status=ToolStatus.DB_FAILURE, message="down")

    writer_status = models.ProjectStatus(project_id="P001", health="green", progress_percent=80)
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, passing_evaluator())))
    monkeypatch.setattr(workflows.tools, "get_project_tasks", failing_tasks)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert result.status.blockers == [] and result.status.risks == [] and result.status.overdue_tasks == []


def test_rejection_survives_construction(conn, monkeypatch):
    """An unsafe draft stays rejected; factual patches never manufacture acceptance."""
    calls = {"n": 0}

    def evaluator(agent, content):
        calls["n"] += 1
        return models.EvaluationResult(passed=False, issues=["unsafe delivery promise"],
                                       revision_instructions="remove promise", iteration=min(calls["n"], 2))

    writer_status = models.ProjectStatus(
        project_id="P001", health="green", progress_percent=90,
        recommended_actions=["We guarantee delivery Friday"])
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, evaluator)))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert not result.accepted
    # Exhausted loop yields provisional-or-blocked, never acceptance: here the
    # unsafe commitment trips the output guardrail after the evaluator rejects.
    assert result.status is None and "guardrail" in (result.error or "")
    assert result.iterations == 2


def test_revision_path_applies_construction(conn, monkeypatch):
    """Second-iteration drafts are constructed too; cap stays at two."""
    calls = {"writer": 0, "eval": 0}

    def writer(agent, content):
        calls["writer"] += 1
        return models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55)

    def evaluator(agent, content):
        calls["eval"] += 1
        if calls["eval"] == 1:
            return models.EvaluationResult(passed=False, issues=["thin risks"],
                                           revision_instructions="name the blocked-task risk", iteration=1)
        return passing_evaluator(iteration=2)

    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer, evaluator)))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.accepted and result.iterations == 2
    assert any("T002" in b for b in result.status.blockers)


def test_wf01_happy_path_order_and_result(conn, monkeypatch):
    """PATTERN chaining: intake → workers → writer → evaluator; valid status returned."""
    log = install_fake(monkeypatch, standard_mapping())
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None and result.accepted
    # Authoritative rows contain critical blocked T002, so the corrected
    # policy routes for review even on a fully evidenced accepted run.
    assert result.route == workflows.ROUTE_REVIEW
    assert result.status.needs_human_approval is True
    assert log[0] == "Intake Agent" and log[-2:] == ["Status Writer", "Evaluator"]
    assert set(log[1:4]) == {"Task Worker", "People Worker", "Deadline/Risk Worker"}
    assert result.evidence is not None and result.evidence.unavailable == []


def test_wf01_review_path_uses_orchestration_without_mutation(conn, monkeypatch):
    """Critical update delegates via orchestrator; review flagged; data untouched."""
    before = [dict(t) for t in database.list_tasks_by_project(conn, "P001")]
    critical = make_update(
        "Critical blocker on gateway.",
        blockers=["critical payment gateway blocker"],
        requested_actions=[models.RequestedAction(action_type="assign_task", task_id="T002")],
    )
    log = install_fake(monkeypatch, standard_mapping(**{"Intake Agent": critical}))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Critical blocker."))
    assert result.error is None and result.route == workflows.ROUTE_REVIEW
    assert "Project Orchestrator" in log and "Task Worker" not in log  # single orchestrated execution
    assert result.status is not None and result.status.needs_human_approval is True
    assert [dict(t) for t in database.list_tasks_by_project(conn, "P001")] == before
    assert result.evidence is not None and result.evidence.unavailable  # collector empty under fakes


def test_wf01_worker_failures_stay_unavailable(conn, monkeypatch):
    """All workers failing still yields an explicit partial result, not guesses."""
    mapping = standard_mapping(**{
        "Task Worker": RuntimeError("down"),
        "People Worker": RuntimeError("down"),
        "Deadline/Risk Worker": RuntimeError("down"),
        "Status Writer": models.ProjectStatus(project_id="P001", health="yellow", progress_percent=40),
    })
    install_fake(monkeypatch, mapping)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert len(result.evidence.unavailable) == 3


def test_wf01_deadline_tool_failure_degrades_route(conn, monkeypatch):
    """Simulated deadline failure leaves days unknown; routine case still completes."""
    install_fake(monkeypatch, standard_mapping())
    with tools.simulated_failure("get_project_deadline"):
        result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    # Missing deadline weakens routing input, and authoritative critical rows
    # require review regardless.
    assert result.route == workflows.ROUTE_REVIEW


def test_wf01_intake_failure_is_safe(conn, monkeypatch):
    """Intake crash stops the chain with an error envelope, no partial status."""
    install_fake(monkeypatch, standard_mapping(**{"Intake Agent": RuntimeError("down")}))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "hi"))
    assert result.status is None and result.error and "intake failed" in result.error


# --- WF-02 ---


def test_wf02_comment_executes_with_confirmation(conn):
    """Low-impact comment runs through the tool and confirms from persistence."""
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "create_project_comment", values={"content": "Standup notes."}))
    assert res.error is None and res.pending is None
    assert res.action_result is not None and res.action_result.success is True
    assert res.action_result.confirmation and len(database.list_comments(conn, "P001")) == 3


def test_wf02_critical_assignment_pends_without_mutation(conn):
    """Assigning critical blocked T002 is held for review; owner unchanged."""
    before = database.get_task(conn, "T002")["owner_id"]
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="T002", values={"user_id": "U003"}))
    assert res.error is None and res.action_result is None and res.route == workflows.ROUTE_REVIEW
    assert res.pending is not None and res.pending.task_id == "T002"
    assert database.get_task(conn, "T002")["owner_id"] == before


def test_wf02_priority_escalation_pends(conn):
    """Raising T001 to critical is a significant change: held, not applied."""
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "update_task", task_id="T001", values={"priority": "critical"}))
    assert res.pending is not None and res.route == workflows.ROUTE_REVIEW
    assert database.get_task(conn, "T001")["priority"] == "medium"


def test_wf02_small_priority_change_executes(conn):
    """Medium→low on far-deadline P002 is not significant: executes with confirmation."""
    res = asyncio.run(workflows.coordinate_task(
        conn, "P002", "U005", "update_task", task_id="T007", values={"priority": "low"}))
    assert res.action_result is not None and res.action_result.success is True
    assert database.get_task(conn, "T007")["priority"] == "low"


def test_wf02_unknown_task_and_bad_action_blocked(conn):
    """Unknown tasks fail safely; unknown action types are errors, never success."""
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "assign_task", task_id="NOPE", values={"user_id": "U003"}))
    assert res.action_result is not None and res.action_result.success is False
    res2 = asyncio.run(workflows.coordinate_task(conn, "P001", "U001", "delete_project", task_id="T001"))
    assert res2.error and res2.action_result is None


def test_wf02_completion_safety_enforced(conn):
    """Completing 30%-done T002 fails; nothing is fabricated or mutated."""
    res = asyncio.run(workflows.coordinate_task(
        conn, "P001", "U001", "update_task", task_id="T002", values={"status": "completed"}))
    assert res.action_result is not None and res.action_result.success is False
    assert database.get_task(conn, "T002")["status"] == "blocked"


def test_wf02_tool_failure_never_succeeds(conn):
    """Simulated persistence failure returns failure, not success."""
    with tools.simulated_failure("create_project_comment"):
        res = asyncio.run(workflows.coordinate_task(
            conn, "P001", "U001", "create_project_comment", values={"content": "Hi."}))
    assert res.action_result is not None and res.action_result.success is False
    assert res.action_result.message.startswith("simulated_failure")


def test_bind_read_tools_exposes_four_named_tools(conn):
    """Binding layer exposes exactly the read tools with SDK-compatible names."""
    bound = workflows.bind_read_tools(conn)
    assert sorted(bound) == ["get_project_deadline", "get_project_tasks", "get_task", "get_team_members"]
    assert all(getattr(t, "name", None) for t in bound.values())
    assert tools.get_task(conn, "T001").status == ToolStatus.SUCCESS  # Phase 2 behavior intact


def test_review_active_when_task_worker_fails_but_rows_confirm_t002(conn, monkeypatch):
    """Worker failure cannot clear an authoritative critical-blocked review."""
    async def boom(c, p, *a):
        raise RuntimeError("model down")

    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    monkeypatch.setattr(workflows, "run_task_analysis", boom)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert result.route == workflows.ROUTE_REVIEW
    assert result.status.needs_human_approval is True
    assert any("T002" in b for b in result.status.blockers)  # rows, not the failed worker
    assert any(u.startswith("task:") for u in result.evidence.unavailable)


def test_failed_workers_keep_confirmed_facts(conn, monkeypatch):
    """Rows confirm facts while failed analyses stay explicitly unavailable."""
    async def boom(c, p, *a):
        raise RuntimeError("model down")

    async def ok_deadline(c, p, *a):
        return models.DeadlineAnalysis(project_id=p, deadline="d", days_remaining=9, risk_level="low")

    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Status Writer": models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    monkeypatch.setattr(workflows, "run_task_analysis", boom)
    monkeypatch.setattr(workflows, "run_people_analysis", boom)
    monkeypatch.setattr(workflows, "run_deadline_analysis", ok_deadline)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert any("T002" in b for b in result.status.blockers)
    assert any(u.startswith("task:") for u in result.evidence.unavailable)
    assert any(u.startswith("people:") for u in result.evidence.unavailable)
    assert result.route == workflows.ROUTE_REVIEW


def test_missing_rows_never_conclude_no_review_needed(conn, monkeypatch):
    """Unreadable authoritative rows force review instead of a false normal."""

    def failing_tasks(c, pid):
        from tools import ToolResult
        return ToolResult(status=ToolStatus.DB_FAILURE, message="down")

    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001"),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": models.ProjectStatus(project_id="P001", health="green", progress_percent=80),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    monkeypatch.setattr(workflows.tools, "get_project_tasks", failing_tasks)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert result.route == workflows.ROUTE_REVIEW
    assert result.status.needs_human_approval is True


def test_double_reject_stays_provisional_on_review_route(conn, monkeypatch):
    """Exhausted evaluation remains provisional and review-routed."""
    calls = {"n": 0}

    def evaluator(agent, content):
        calls["n"] += 1
        return models.EvaluationResult(passed=False, issues=["too thin"],
                                       revision_instructions="add detail",
                                       iteration=min(calls["n"], 2))

    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001"),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="2026-10-10", days_remaining=2, risk_level="high"),
        "Status Writer": models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55),
        "Evaluator": evaluator,
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        value = mapping[agent.name]
        return value(agent, content) if callable(value) else value

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert not result.accepted and result.iterations == 2
    assert result.route == workflows.ROUTE_REVIEW
    if result.status is not None:
        assert result.status.needs_human_approval is True
    assert result.warning or result.error


def test_fully_evidenced_accepted_p002_stays_normal(conn, monkeypatch):
    """No critical rows, complete evidence, accepted draft: normal routing kept."""
    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P002", user_id="U005", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P002"),
        "People Worker": models.PeopleAnalysis(project_id="P002"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P002", deadline="d", days_remaining=60, risk_level="low"),
        "Status Writer": models.ProjectStatus(project_id="P002", health="green", progress_percent=70),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P002", "U005", "Weekly check."))
    assert result.error is None and result.accepted
    assert result.route == workflows.ROUTE_NORMAL
    assert result.status is not None and result.status.needs_human_approval is False


def test_review_flag_authorizes_no_mutation(conn, monkeypatch):
    """needs_human_approval on a status is review attention, not write permission."""
    import database

    before = {t["id"]: dict(t) for t in database.list_tasks_by_project(conn, "P001")}
    before.update({t["id"]: dict(t) for t in database.list_tasks_by_project(conn, "P002")})
    mapping = {
        "Intake Agent": models.ProjectUpdate(
            project_id="P001", user_id="U001", message="Critical blocker.",
            blockers=["critical payment gateway blocker"]),
        "Status Writer": models.ProjectStatus(project_id="P001", health="red", progress_percent=30),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
        "Project Orchestrator": "summary",
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Critical blocker."))
    assert result.route == workflows.ROUTE_REVIEW
    assert result.status is not None and result.status.needs_human_approval is True
    after = {t["id"]: dict(t) for t in database.list_tasks_by_project(conn, "P001")}
    after.update({t["id"]: dict(t) for t in database.list_tasks_by_project(conn, "P002")})
    assert after == before


def test_writer_yellow_corrected_to_rule_red(conn, monkeypatch):
    """The exact reported contradiction: critical-blocked rows force RED health."""
    writer_status = models.ProjectStatus(project_id="P001", health="yellow", progress_percent=60)
    monkeypatch.setattr(workflows, "run_agent_step",
                        _dispatching_fake(_wf01_mapping(writer_status, passing_evaluator())))
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.error is None and result.status is not None
    assert result.status.health == models.Health.RED


def test_healthy_project_keeps_green(conn, monkeypatch):
    """Construction does not inflate health where rows show no risk."""
    writer_status = models.ProjectStatus(project_id="P002", health="green", progress_percent=70)
    mapping = {
        "Intake Agent": models.ProjectUpdate(project_id="P002", user_id="U005", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P002"),
        "People Worker": models.PeopleAnalysis(project_id="P002"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P002", deadline="d", days_remaining=60, risk_level="low"),
        "Status Writer": writer_status,
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P002", "U005", "Weekly check."))
    assert result.error is None and result.status is not None
    assert result.status.health == models.Health.GREEN


def _accepted_p001_mapping():
    return {
        "Intake Agent": models.ProjectUpdate(project_id="P001", user_id="U001", message="hi"),
        "Task Worker": models.TaskAnalysis(project_id="P001"),
        "People Worker": models.PeopleAnalysis(project_id="P001"),
        "Deadline/Risk Worker": models.DeadlineAnalysis(
            project_id="P001", deadline="d", days_remaining=9, risk_level="low"),
        "Status Writer": models.ProjectStatus(project_id="P001", health="yellow", progress_percent=55),
        "Evaluator": models.EvaluationResult(passed=True, iteration=1),
    }


def test_accepted_run_records_durable_memory(conn, monkeypatch):
    """Accepted assessments persist a deterministic summary for later runs."""
    mapping = _accepted_p001_mapping()

    async def fake(agent, content, output_type, *args, **kwargs):
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    assert database.get_project_memory(conn, "P001") is None
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert result.accepted
    stored = database.get_project_memory(conn, "P001")
    # Seeded P001 rows are critical-blocked, so deterministic health is red.
    assert stored is not None and "red 55%" in stored["summary"] and stored["health"] == "red"
    # Completed T005 is never listed as overdue; progress passes through from the draft.
    assert "T005" not in stored["summary"]
    assert "55%" in stored["summary"]


def test_rejected_run_records_no_memory(conn, monkeypatch):
    """Provisional or failed runs never pollute long-term memory."""

    def evaluator(agent, content):
        return models.EvaluationResult(passed=False, issues=["thin"], revision_instructions="fix", iteration=1)

    mapping = _accepted_p001_mapping()
    mapping["Evaluator"] = evaluator

    async def fake(agent, content, output_type, *args, **kwargs):
        value = mapping[agent.name]
        return value(agent, content) if callable(value) else value

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    result = asyncio.run(workflows.health_update(conn, "P001", "U001", "Weekly check."))
    assert not result.accepted
    assert database.get_project_memory(conn, "P001") is None


def test_prior_memory_offered_as_background_not_fact(conn, monkeypatch):
    """Second run sees the first run's summary labeled as background."""
    mapping = _accepted_p001_mapping()
    seen = []

    async def fake(agent, content, output_type, *args, **kwargs):
        if agent.name == "Status Writer":
            seen.append(content)
        return mapping[agent.name]

    monkeypatch.setattr(workflows, "run_agent_step", fake)
    asyncio.run(workflows.health_update(conn, "P001", "U001", "First check."))
    asyncio.run(workflows.health_update(conn, "P001", "U001", "Second check."))
    assert any("Last known status" in prompt and "background only" in prompt for prompt in seen[1:])
