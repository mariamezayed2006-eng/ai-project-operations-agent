"""Workflows and orchestration for the AI Project Operations Agent (Project 4).

Phase 4: WF-01 (health update), WF-02 (task coordination), and the five
required orchestration patterns. Code-orchestrated vs LLM-orchestrated
split (SDD Section 4.2) is marked inline:

- CODE-orchestrated: step sequencing, risk/approval routing, evaluator
  iteration cap, and parallel fan-out via ``asyncio.gather``.
- LLM-orchestrated: the Project Orchestrator deciding which specialists
  to invoke through agent-as-tool, and the Status Writer's wording.

Boundaries (unchanged from earlier phases): no global connection — the
caller owns ``conn`` and every tool call flows through ``tools.py``
(parameterized SQL lives in ``database.py``). No SQL here, no model
baggage in prompts, no chain-of-thought. LLM calls go through the single
``run_agent_step`` seam so tests can fake them without live API calls.

Deferred to later phases: guardrails, HITL pause/resume, Gradio,
SQLiteSession, RunContext, and tracing integration.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import agents as project_agents  # noqa: F401  (registers the real SDK as `agents_sdk`)
import agents_sdk
import business_rules as rules
import database
import models
import provider
import tools
from tools import ToolStatus

# --- CODE-orchestrated: routing outcomes and revision cap ---

ROUTE_NORMAL = "normal"
ROUTE_REVIEW = "requires_human_review"
MAX_REVISIONS = 2

COMMITMENT_ACTIONS = {"create_task", "update_task", "assign_task"}
IMPACT_PRIORITIES = {"high", "critical"}


# --- Structured workflow results (plain dataclasses, not Pydantic) ---


@dataclass
class WorkerFailure:
    """One specialist analysis that produced no evidence."""

    worker: str
    reason: str


@dataclass
class EvidenceBundle:
    """Merged specialist evidence; unavailable entries are explicit, never guessed."""

    task: models.TaskAnalysis | None = None
    people: models.PeopleAnalysis | None = None
    deadline: models.DeadlineAnalysis | None = None
    unavailable: list[str] = field(default_factory=list)


@dataclass
class RevisionOutcome:
    """Result of the bounded evaluator loop."""

    accepted: bool
    iterations: int
    warning: str | None = None
    error: str | None = None


@dataclass
class HealthUpdateResult:
    """Safe envelope for WF-01; ``status`` is None when the chain failed safely."""

    status: models.ProjectStatus | None
    route: str
    evidence: EvidenceBundle | None
    iterations: int = 0
    accepted: bool = False
    warning: str | None = None
    error: str | None = None
    blocked: bool = False
    evidence_path: str = "none"  # "parallel" | "orchestrated" | "none" (observability only)


@dataclass
class PendingReview:
    """A high-impact action held for future human approval; NOT executed."""

    action_type: str
    task_id: str | None
    requested_values: dict[str, Any]
    reason: str
    request_id: str | None = None


@dataclass
class CoordinationResult:
    """Safe envelope for WF-02: exactly one of action_result / pending / error."""

    action_result: models.ActionResult | None = None
    pending: PendingReview | None = None
    route: str = ROUTE_NORMAL
    error: str | None = None


# --- CODE-orchestrated: deterministic routing inputs ---


def update_risk_flags(update: models.ProjectUpdate) -> dict[str, bool]:
    """Derive routing flags from a ProjectUpdate (no evidence needed).

    Conservative by design: any requested task mutation is treated as
    commitment-affecting; evidence-informed refinements happen later.
    """
    text = " ".join([update.message, *update.blockers, *update.risks]).lower()
    actions = update.requested_actions
    return {
        "critical_blocker": "critical" in text,
        "action_affects_commitment": any(a.action_type.value in COMMITMENT_ACTIONS for a in actions),
        "significant_priority_change": any(
            a.action_type == models.ActionType.UPDATE_TASK
            and isinstance(a.values.get("priority"), str)
            and a.values["priority"] in IMPACT_PRIORITIES
            for a in actions
        ),
        "high_impact_reassignment": any(a.action_type == models.ActionType.ASSIGN_TASK for a in actions),
    }


def route_update(update: models.ProjectUpdate, days_remaining: int | None) -> str:
    """Deterministic Python routing; the LLM never decides approval need.

    Calls ``business_rules.needs_human_approval`` (single source of truth).
    """
    flags = update_risk_flags(update)
    if rules.needs_human_approval(
        critical_blocker=flags["critical_blocker"],
        deadline_days_remaining=days_remaining,
        action_affects_commitment=flags["action_affects_commitment"],
        significant_priority_change=flags["significant_priority_change"],
        high_impact_reassignment=flags["high_impact_reassignment"],
    ):
        return ROUTE_REVIEW
    return ROUTE_NORMAL


def merge_evidence(
    task: models.TaskAnalysis | WorkerFailure | None,
    people: models.PeopleAnalysis | WorkerFailure | None,
    deadline: models.DeadlineAnalysis | WorkerFailure | None,
) -> EvidenceBundle:
    """Merge worker outputs; failures become explicit unavailable entries."""
    bundle = EvidenceBundle()
    for name, value in (("task", task), ("people", people), ("deadline", deadline)):
        if isinstance(value, WorkerFailure):
            bundle.unavailable.append(f"{name}: {value.reason}")
        elif value is not None:
            setattr(bundle, name, value)
        else:
            bundle.unavailable.append(f"{name}: no result returned")
    return bundle


def render_evidence_text(
    bundle: EvidenceBundle,
    update: models.ProjectUpdate,
    as_of_date: Any = None,
    task_rows: list[dict[str, Any]] | None = None,
    prior_memory: dict[str, Any] | None = None,
) -> str:
    """Render deterministic writer input from structured evidence (no guessing).

    ``as_of_date`` is the explicit reference day (UTC calendar date); absolute
    task deadlines are listed alongside it so the writer never needs to guess
    what "today" is. Relative ``days_remaining`` figures are kept as well.
    ``prior_memory`` is the durable summary from the last accepted run:
    background context only, always verified against current evidence.
    """
    as_of = as_of_date if as_of_date is not None else rules.utc_today()
    lines = [
        f"Reference date (as_of_date, UTC): {as_of.isoformat()}. Treat this as today; do not guess the date.",
        f"Project: {update.project_id}. User message: {update.message}",
        f"Requested actions: {[a.action_type.value for a in update.requested_actions] or 'none'}",
    ]
    if prior_memory:
        lines.append(
            f"Last known status ({prior_memory.get('updated_at')}): {prior_memory.get('summary')} — "
            "background only; verify against current evidence, do not repeat blindly."
        )
    if task_rows:
        lines.append("--- TASK DEADLINES (absolute ISO dates, authoritative) ---")
        for row in sorted(task_rows, key=lambda r: str(r.get("id"))):
            lines.append(
                f"{row.get('id')}: deadline {row.get('deadline') or 'none'}, "
                f"status {row.get('status')}, priority {row.get('priority')}, "
                f"completion {row.get('completion_percent')}%"
            )
    for name in ("task", "people", "deadline"):
        value = getattr(bundle, name)
        lines.append(f"--- {name.upper()} EVIDENCE ---")
        lines.append(value.model_dump_json() if value is not None else "UNAVAILABLE: do not claim facts for this area.")
    if bundle.unavailable:
        lines.append("Missing evidence (state explicitly, do not infer): " + "; ".join(bundle.unavailable))
    lines.append("Health guidance is authoritative from deterministic rules; thresholds: red<=2d/2-overdue, yellow<=7d.")
    return "\n".join(lines)


# --- LLM seam (single faking point; verified SDK 0.23.x API) ---


async def run_agent_step(agent: Any, content: str, output_type: Any, session: Any = None, context: Any = None) -> Any:
    """Run one agent via ``Runner.run`` and return its typed output.

    Uses ``RunResult.final_output_as`` (installed SDK 0.23.x API).
    ``session`` (SQLiteSession) and ``context`` (AppContext) are threaded
    from the runtime layer; both default to None for direct/test use.
    The model provider comes from ``provider.get_run_config`` (Mistral
    Chat Completions when configured, SDK defaults otherwise).
    Raises on transport or validation failure so chains degrade safely.
    """
    result = await agents_sdk.Runner.run(
        agent, content, session=session, context=context, run_config=provider.get_run_config()
    )
    if output_type is None:
        try:
            return result.final_output_as(str)
        except Exception:
            return str(result)
    return result.final_output_as(output_type)


# --- Tool binding: workflow-owned connection captured in closures ---


def bind_read_tools(conn: sqlite3.Connection) -> dict[str, Any]:
    """Wrap Phase 2 read tools as SDK function tools bound to this run's conn.

    The connection lives only in closures — never in prompts or results.
    Signatures stay SDK-compatible; Phase 2 tool code is untouched.
    """

    def get_project_tasks(project_id: str) -> str:
        """Retrieve all task records for a project."""
        return tools.get_project_tasks(conn, project_id).model_dump_json()

    def get_task(task_id: str) -> str:
        """Retrieve one task record by id."""
        return tools.get_task(conn, task_id).model_dump_json()

    def get_team_members(project_id: str) -> str:
        """Retrieve team members with availability and workload."""
        return tools.get_team_members(conn, project_id).model_dump_json()

    def get_project_deadline(project_id: str) -> str:
        """Retrieve a project deadline with days remaining."""
        return tools.get_project_deadline(conn, project_id).model_dump_json()

    bound = {
        "get_project_tasks": agents_sdk.function_tool(get_project_tasks),
        "get_task": agents_sdk.function_tool(get_task),
        "get_team_members": agents_sdk.function_tool(get_team_members),
        "get_project_deadline": agents_sdk.function_tool(get_project_deadline),
    }
    return bound


WORKER_TOOL_MAP = {
    "task_worker": ("get_project_tasks", "get_task"),
    "people_worker": ("get_team_members",),
    "deadline_worker": ("get_project_deadline", "get_project_tasks"),
}

WORKER_OUTPUT_TYPES = {
    "task_worker": models.TaskAnalysis,
    "people_worker": models.PeopleAnalysis,
    "deadline_worker": models.DeadlineAnalysis,
}


def bind_worker_agents(conn: sqlite3.Connection) -> dict[str, Any]:
    """Give each specialist a copy of its agent with read-only tools attached.

    Base agents in ``agents.py`` are never mutated (copies via replace).
    Write tools are never attached to analysis agents.
    """
    read_tools = bind_read_tools(conn)
    bound: dict[str, Any] = {}
    for key, names in WORKER_TOOL_MAP.items():
        base = project_agents.AGENTS[key]
        bound[key] = dataclasses.replace(base, tools=[read_tools[n] for n in names])
    return bound


async def run_task_analysis(conn: sqlite3.Connection, project_id: str, session: Any = None, context: Any = None) -> models.TaskAnalysis | WorkerFailure:
    """Run the Task Worker (read tools bound); failure stays explicit."""
    try:
        agent = bind_worker_agents(conn)["task_worker"]
        result = await run_agent_step(agent, f"Analyze tasks for project {project_id}.", models.TaskAnalysis, session, context)
        return result if isinstance(result, models.TaskAnalysis) else WorkerFailure("task", "invalid worker output")
    except Exception as exc:
        return WorkerFailure("task", f"worker failed: {type(exc).__name__}")


async def run_people_analysis(conn: sqlite3.Connection, project_id: str, session: Any = None, context: Any = None) -> models.PeopleAnalysis | WorkerFailure:
    """Run the People Worker (read tools bound); failure stays explicit."""
    try:
        agent = bind_worker_agents(conn)["people_worker"]
        result = await run_agent_step(agent, f"Analyze team for project {project_id}.", models.PeopleAnalysis, session, context)
        return result if isinstance(result, models.PeopleAnalysis) else WorkerFailure("people", "invalid worker output")
    except Exception as exc:
        return WorkerFailure("people", f"worker failed: {type(exc).__name__}")


async def run_deadline_analysis(conn: sqlite3.Connection, project_id: str, session: Any = None, context: Any = None) -> models.DeadlineAnalysis | WorkerFailure:
    """Run the Deadline Worker (read tools bound); failure stays explicit."""
    try:
        agent = bind_worker_agents(conn)["deadline_worker"]
        result = await run_agent_step(agent, f"Analyze deadline risk for project {project_id}.", models.DeadlineAnalysis, session, context)
        return result if isinstance(result, models.DeadlineAnalysis) else WorkerFailure("deadline", "invalid worker output")
    except Exception as exc:
        return WorkerFailure("deadline", f"worker failed: {type(exc).__name__}")


# --- PATTERN: Parallelization (CODE-orchestrated, asyncio.gather) ---


async def gather_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    include: tuple[str, ...] = ("task", "people", "deadline"),
    session: Any = None,
    context: Any = None,
) -> EvidenceBundle:
    """Run independent specialists concurrently; merge without fabrication."""
    runners: dict[str, Callable[[], Awaitable[Any]]] = {
        "task": lambda: run_task_analysis(conn, project_id, session, context),
        "people": lambda: run_people_analysis(conn, project_id, session, context),
        "deadline": lambda: run_deadline_analysis(conn, project_id, session, context),
    }
    selected = [name for name in ("task", "people", "deadline") if name in include]
    results = await asyncio.gather(*(runners[name]() for name in selected), return_exceptions=True)
    merged: dict[str, Any] = {}
    for name, res in zip(selected, results):
        merged[name] = res if not isinstance(res, BaseException) else WorkerFailure(name, f"worker raised {type(res).__name__}")
    return merge_evidence(merged.get("task"), merged.get("people"), merged.get("deadline"))


# --- PATTERN: Orchestrator-Workers (LLM-orchestrated, agent-as-tool) ---


def attach_workers_to_orchestrator(worker_agents: dict[str, Any]) -> Any:
    """Attach specialists to a copy of the Project Orchestrator via as_tool.

    Verified SDK 0.23.x: ``Agent.as_tool(tool_name, tool_description)``.
    The orchestrator LLM decides which specialists to invoke; Python never
    delegates safety decisions to it. Base agents are not mutated.
    """
    descriptions = {
        "task_worker": "Analyze project tasks, blockers, overdue and critical work.",
        "people_worker": "Analyze team ownership, availability, and workload.",
        "deadline_worker": "Analyze project deadline proximity and schedule risk.",
    }
    names = {"task_worker": "task_analysis", "people_worker": "people_analysis", "deadline_worker": "deadline_analysis"}
    worker_tools = [worker_agents[key].as_tool(names[key], descriptions[key]) for key in descriptions if key in worker_agents]
    return dataclasses.replace(project_agents.AGENTS["project_orchestrator"], tools=worker_tools)


def make_worker_output_collector() -> tuple[dict[str, Any], dict[str, Callable[[Any], str]]]:
    """Build per-worker output extractors that stash structured JSON.

    Lets the orchestrated path capture each worker's typed output exactly
    once (no double execution) while the model still receives readable text.
    Testable without live runs using fake run-results.
    """
    collector: dict[str, Any] = {}

    def _extractor(key: str, model_cls: Any) -> Callable[[Any], str]:
        def extract(run_result: Any) -> str:
            parsed = run_result.final_output_as(model_cls)
            payload = parsed.model_dump_json()
            collector[key] = payload
            return payload

        return extract

    return collector, {key: _extractor(key, cls) for key, cls in WORKER_OUTPUT_TYPES.items()}


async def run_orchestrated_analysis(
    conn: sqlite3.Connection, project_id: str, update: models.ProjectUpdate,
    session: Any = None, context: Any = None,
) -> tuple[str, EvidenceBundle]:
    """LLM-led delegation: orchestrator calls worker tools; evidence captured once."""
    worker_agents = bind_worker_agents(conn)
    collector, extractors = make_worker_output_collector()
    descriptions = {
        "task_worker": "Analyze project tasks, blockers, overdue and critical work.",
        "people_worker": "Analyze team ownership, availability, and workload.",
        "deadline_worker": "Analyze project deadline proximity and schedule risk.",
    }
    names = {"task_worker": "task_analysis", "people_worker": "people_analysis", "deadline_worker": "deadline_analysis"}
    worker_tools = [
        worker_agents[key].as_tool(names[key], descriptions[key], custom_output_extractor=extractors[key])
        for key in descriptions
    ]
    orchestrator = dataclasses.replace(project_agents.AGENTS["project_orchestrator"], tools=worker_tools)
    summary = await run_agent_step(
        orchestrator, f"Coordinate analysis for project {project_id}. Update: {update.message}", None, session, context
    )
    bundle = EvidenceBundle()
    for key, model_cls in WORKER_OUTPUT_TYPES.items():
        short = {"task_worker": "task", "people_worker": "people", "deadline_worker": "deadline"}[key]
        if key in collector:
            setattr(bundle, short, model_cls.model_validate_json(collector[key]))
        else:
            bundle.unavailable.append(f"{short}: orchestrator did not invoke this specialist")
    return (summary if isinstance(summary, str) else str(summary), bundle)


# --- PATTERN: Evaluator-Optimizer (CODE-orchestrated bound, max 2) ---


def apply_authoritative_facts(
    draft: Any, task_rows: list[dict[str, Any]] | None, as_of: Any,
    days_remaining: int | None = None,
) -> Any:
    """Overlay deterministic facts onto a draft; writer prose otherwise kept.

    Constructs ``health``, ``overdue_tasks``, ``blockers``, and factual
    ``risks`` entries from database rows (never model prose); the writer's
    own risks and recommendations are preserved verbatim after exact-dedupe,
    and evidence citations are extended to the cited fact ids. A draft
    claiming YELLOW while the deterministic rules say RED is corrected —
    health is a rule output, not model judgment. Non-draft input or missing
    rows pass through untouched so the evaluator/guardrail judge them.
    Model validation failure likewise leaves the draft for the guardrail.
    """
    if not isinstance(draft, models.ProjectStatus) or task_rows is None:
        return draft
    try:
        overdue = rules.derive_overdue_task_ids(task_rows, as_of)
        blockers = rules.derive_blocker_entries(task_rows)
        factual_risks = rules.derive_risk_entries(task_rows, as_of)
        merged_risks = factual_risks + [r for r in draft.risks if r not in factual_risks]
        cited = set(overdue) | rules.derive_material_task_ids(task_rows, as_of)
        health = models.Health(rules.derive_health(
            has_critical_blocker=rules.has_critical_blocked(task_rows),
            blocked_count=sum(1 for t in task_rows if rules.is_blocked(t)),
            overdue_count=len(overdue),
            critical_count=sum(1 for t in task_rows if t.get("priority") == "critical"),
            days_remaining=days_remaining,
        ))
        return draft.model_copy(update={
            "health": health,
            "overdue_tasks": overdue,
            "blockers": blockers,
            "risks": merged_risks,
            "evidence_task_ids": sorted(set(draft.evidence_task_ids) | cited),
        })
    except Exception:
        return draft


async def revise_with_evaluator(
    make_draft: Callable[[str | None], Awaitable[Any]],
    evaluate: Callable[[models.ProjectStatus], Awaitable[models.EvaluationResult]],
) -> tuple[models.ProjectStatus | None, RevisionOutcome]:
    """Writer → evaluator loop capped at MAX_REVISIONS; exhaustion stays safe."""
    feedback: str | None = None
    draft: Any = None
    for iteration in range(1, MAX_REVISIONS + 1):
        try:
            draft = await make_draft(feedback)
        except Exception as exc:
            return None, RevisionOutcome(False, iteration - 1, error=f"draft failed: {type(exc).__name__}")
        if not isinstance(draft, models.ProjectStatus):
            return None, RevisionOutcome(False, iteration - 1, error="draft failed validation")
        try:
            verdict = await evaluate(draft)
        except Exception as exc:
            return None, RevisionOutcome(False, iteration, error=f"evaluation failed: {type(exc).__name__}")
        if not isinstance(verdict, models.EvaluationResult):
            return None, RevisionOutcome(False, iteration, error="evaluation produced invalid output")
        if verdict.passed:
            return draft, RevisionOutcome(True, iteration)
        parts = [verdict.revision_instructions, *verdict.issues, *verdict.missing_evidence]
        feedback = "; ".join(p for p in parts if p) or "Revise unsupported claims and add missing evidence."
    if isinstance(draft, models.ProjectStatus):
        safe = draft.model_copy(update={"needs_human_approval": True})
        return safe, RevisionOutcome(False, MAX_REVISIONS, warning="Evaluator rejected all drafts; safest draft returned for human review.")
    return None, RevisionOutcome(False, MAX_REVISIONS, warning="No valid draft produced.")


# --- WF-01: Project Health Update (PATTERN: Prompt Chaining, CODE-orchestrated) ---


async def health_update(conn: sqlite3.Connection, project_id: str, user_id: str, message: str, session: Any = None, context: Any = None) -> HealthUpdateResult:
    """End-to-end health workflow: screen → intake → route → evidence → writer → evaluator → screen.

    NORMAL route uses parallel direct workers; REVIEW route uses orchestrated
    delegation (each worker executes at most once per run). Read-only: WF-01
    never mutates project data. Blocked input stops before Intake; rejected
    output is revised once at most, then reported as a safe failure.
    Runs inside trace "WF-01 Project Health Update" (SDD Section 8.4).
    """
    import guardrails

    with agents_sdk.trace("WF-01 Project Health Update"):
        return await _health_update_inner(conn, project_id, user_id, message, session, context)


async def _health_update_inner(conn: sqlite3.Connection, project_id: str, user_id: str, message: str, session: Any, context: Any) -> HealthUpdateResult:
    import guardrails

    screening = guardrails.screen_input(message)
    if screening.blocked:
        return HealthUpdateResult(
            None, ROUTE_NORMAL, None,
            error=f"input blocked: {'; '.join(screening.reasons)}",
            blocked=True,
        )
    force_review = screening.needs_review
    try:
        intake_raw = await run_agent_step(project_agents.AGENTS["intake"], message, models.ProjectUpdate, session, context)
    except Exception as exc:
        return HealthUpdateResult(None, ROUTE_NORMAL, None, error=f"intake failed: {type(exc).__name__}")
    if not isinstance(intake_raw, models.ProjectUpdate):
        return HealthUpdateResult(None, ROUTE_NORMAL, None, error="intake produced invalid output")
    update = intake_raw.model_copy(update={"project_id": project_id, "user_id": user_id})

    deadline_info = tools.get_project_deadline(conn, project_id)
    days = deadline_info.data.get("days_remaining") if deadline_info.ok else None
    route = ROUTE_REVIEW if force_review else route_update(update, days)

    # Authoritative date facts (deterministic Python, never the model):
    # explicit reference day plus the stored rows overdue classification
    # is derived from. Unavailable rows mean no construction (writer value
    # stands and the output guardrail judges it).
    as_of = rules.utc_today()
    rows_res = tools.get_project_tasks(conn, project_id)
    task_rows = rows_res.data if rows_res.ok else None
    try:
        prior_memory = database.get_project_memory(conn, project_id)
    except database.DatabaseError:
        prior_memory = None  # auxiliary context; its absence never fails the run

    try:
        if route == ROUTE_REVIEW:
            _, evidence = await run_orchestrated_analysis(conn, project_id, update, session, context)
            evidence_path = "orchestrated"
        else:
            evidence = await gather_evidence(conn, project_id, session=session, context=context)
            evidence_path = "parallel"
    except Exception as exc:
        return HealthUpdateResult(None, route, None, error=f"evidence stage failed: {type(exc).__name__}")

    evidence_text = render_evidence_text(evidence, update, as_of_date=as_of, task_rows=task_rows,
                                          prior_memory=prior_memory)

    async def make_draft(feedback: str | None) -> models.ProjectStatus:
        prompt = evidence_text if feedback is None else f"{evidence_text}\nREVISION FEEDBACK: {feedback}"
        raw = await run_agent_step(project_agents.AGENTS["status_writer"], prompt, models.ProjectStatus, session, context)
        # Factual fields are constructed BEFORE evaluation on every draft:
        # the evaluator judges evidence-grounded content, never raw prose.
        return apply_authoritative_facts(raw, task_rows, as_of, days)

    async def evaluate(draft: models.ProjectStatus) -> models.EvaluationResult:
        return await run_agent_step(
            project_agents.AGENTS["evaluator"],
            f"Draft: {draft.model_dump_json()}\nEvidence: {evidence_text}",
            models.EvaluationResult, session, context,
        )

    draft, outcome = await revise_with_evaluator(make_draft, evaluate)
    if outcome.error or draft is None:
        return HealthUpdateResult(None, route, evidence, outcome.iterations, False, error=outcome.error)

    # Factual fields were already constructed inside every loop draft above,
    # so the evaluator judged evidence-grounded content throughout.

    # Final review decision: authoritative rows, acceptance, and evidence
    # completeness decide — never LLM worker output alone. The preliminary
    # route is preserved (it also selects the evidence path); health-review
    # only adds review, never removes it. WF-02 mutation approval is a
    # separate mechanism and is untouched by this flag.
    task_rows_available = task_rows is not None
    health_review = rules.needs_health_review(
        critical_blocked_rows=rules.has_critical_blocked(task_rows) if task_rows_available else False,
        accepted=outcome.accepted,
        rows_available=task_rows_available,
        workers_complete=not evidence.unavailable,
        deadline_days_remaining=days,
        action_affects_commitment=update_risk_flags(update)["action_affects_commitment"],
    )
    final_route = ROUTE_REVIEW if (route == ROUTE_REVIEW or health_review) else ROUTE_NORMAL
    final_status = draft.model_copy(update={"needs_human_approval": final_route == ROUTE_REVIEW})

    # Output guardrail: nothing ungrounded is presented as final. One bounded
    # writer retry on guardrail feedback, then a safe failure (never a fix-up
    # by invention, never an infinite loop).
    ground = _output_screening(conn, project_id, final_status, as_of.isoformat())
    if ground.blocked:
        retry_prompt = (
            f"{evidence_text}\nOUTPUT GUARDRAIL FEEDBACK: {'; '.join(ground.reasons)}. "
            "Remove or evidence every flagged claim."
        )
        try:
            retry = await run_agent_step(project_agents.AGENTS["status_writer"], retry_prompt, models.ProjectStatus, session, context)
        except Exception:
            retry = None
        retry = apply_authoritative_facts(retry, task_rows, as_of, days)
        if isinstance(retry, models.ProjectStatus) and not _output_screening(
            conn, project_id, retry, as_of.isoformat()
        ).blocked:
            retry = retry.model_copy(update={"needs_human_approval": True})
            return HealthUpdateResult(retry, ROUTE_REVIEW, evidence, outcome.iterations + 1, False,
                                      "Output guardrail required a revision; review before trusting.",
                                      evidence_path=evidence_path)
        return HealthUpdateResult(
            None, final_route, evidence, outcome.iterations, False,
            warning="Output guardrail blocked the final status; no trustworthy status available.",
            error=f"output guardrail blocked: {'; '.join(ground.reasons)}",
            evidence_path=evidence_path,
        )
    if outcome.accepted and draft is not None:
        # Bonus (durable memory): record accepted assessments only. Rejected,
        # provisional, or failed runs never pollute long-term memory, and a
        # memory-write failure never fails the health run itself.
        try:
            health_value = draft.health.value if isinstance(draft.health, models.Health) else str(draft.health)
            database.upsert_project_memory(
                conn, project_id,
                f"{health_value} {draft.progress_percent}% | "
                f"blockers: {len(draft.blockers)} | overdue: {', '.join(draft.overdue_tasks) or 'none'}",
                health_value, as_of.isoformat(),
            )
        except database.DatabaseError:
            pass
    return HealthUpdateResult(final_status, final_route, evidence, outcome.iterations, outcome.accepted, outcome.warning,
                                evidence_path=evidence_path)


def _output_screening(
    conn: sqlite3.Connection,
    project_id: str,
    status: models.ProjectStatus,
    reference_date: str | None = None,
) -> Any:
    """Run the output guardrail against live project facts."""
    import guardrails

    try:
        tasks = database.list_tasks_by_project(conn, project_id)
        members = database.list_team_members(conn, project_id)
        project = database.get_project(conn, project_id)
    except database.DatabaseError:
        return guardrails.ScreeningResult(
            verdict=guardrails.Verdict.BLOCK,
            reasons=["project facts unavailable for output validation"],
            guidance="Retry when project data is reachable.",
        )
    known_tasks = {t["id"] for t in tasks}
    completed = {t["id"] for t in tasks if t["status"] == "completed"}
    return guardrails.check_status_output(
        status,
        known_task_ids=known_tasks,
        known_member_ids={m["id"] for m in members},
        deadline=project["deadline"] if project else None,
        completed_task_ids=completed,
        reference_date=reference_date,
    )


# --- WF-02: Task Coordination (chain + CODE-orchestrated policy, no HITL yet) ---


def _wf02_flags(task: dict | None, action_type: str, values: dict[str, Any]) -> dict[str, bool]:
    """Policy inputs for one requested action from live task facts."""
    critical = bool(task) and (task.get("priority") == "critical" or bool(task.get("blocker")))
    if action_type == "assign_task":
        return {
            "critical_blocker": critical,
            "action_affects_commitment": True,
            "significant_priority_change": False,
            "high_impact_reassignment": True,
        }
    if action_type == "update_task":
        new_prio = values.get("priority")
        old_prio = task.get("priority") if task else None
        sig = (
            isinstance(new_prio, str)
            and new_prio != old_prio
            and (new_prio in IMPACT_PRIORITIES or old_prio in IMPACT_PRIORITIES)
        )
        return {
            "critical_blocker": critical and values.get("status") == "completed",
            "action_affects_commitment": True,
            "significant_priority_change": bool(sig),
            "high_impact_reassignment": False,
        }
    return {
        "critical_blocker": False,
        "action_affects_commitment": action_type == "create_task",
        "significant_priority_change": False,
        "high_impact_reassignment": False,
    }


async def coordinate_task(
    conn: sqlite3.Connection,
    project_id: str,
    user_id: str,
    action_type: str,
    task_id: str | None = None,
    values: dict[str, Any] | None = None,
    store: Any = None,
) -> CoordinationResult:
    """Inspect → authorize → policy → execute-or-hold. Runs inside trace "WF-02 Task Coordination"."""
    with agents_sdk.trace("WF-02 Task Coordination"):
        return await _coordinate_inner(conn, project_id, user_id, action_type, task_id, values, store)


async def _coordinate_inner(
    conn: sqlite3.Connection,
    project_id: str,
    user_id: str,
    action_type: str,
    task_id: str | None = None,
    values: dict[str, Any] | None = None,
    store: Any = None,
) -> CoordinationResult:
    """Inspect → authorize → policy → execute-or-hold.

    REVIEW actions create a real approval request when ``store`` is given
    (otherwise the legacy hold-only PendingReview is returned, unchanged).
    NORMAL executions are output-validated before success is reported.
    """
    import approval as approval_mod
    import guardrails

    values = dict(values or {})
    if action_type not in {a.value for a in models.ActionType}:
        return CoordinationResult(error=f"unknown action_type: {action_type}")
    try:
        task = database.get_task(conn, task_id) if task_id else None
        if action_type in {"update_task", "assign_task"} and task is None:
            return CoordinationResult(
                action_result=models.ActionResult(
                    action_type=models.ActionType(action_type),
                    target_id=task_id or "?",
                    success=False,
                    message=f"task {task_id} not found; nothing was executed.",
                )
            )
        scope_project = project_id if action_type in {"create_task", "create_project_comment"} else (task or {}).get("project_id", project_id)
        member = database.get_team_member(conn, user_id)
        member_role = member["role"] if member is not None and member["project_id"] == scope_project else None
        is_owner = bool(task) and task.get("owner_id") == user_id
        if not guardrails.is_action_permitted(member_role=member_role, is_owner=is_owner, action_type=action_type):
            return CoordinationResult(
                action_result=models.ActionResult(
                    action_type=models.ActionType(action_type),
                    target_id=task_id or project_id,
                    success=False,
                    message="not permitted for the requester's project role; nothing was executed.",
                )
            )
        days: int | None = None
        if action_type != "create_project_comment":
            dl = tools.get_project_deadline(conn, project_id if action_type == "create_task" else task["project_id"])
            days = dl.data.get("days_remaining") if dl.ok else None
        flags = _wf02_flags(task, action_type, values)
        if action_type == "update_task" and values.get("status") == "completed" and not rules.can_mark_completed(
            True, task["completion_percent"], task["completion_percent"] == 100
        ):
            # Validity precedes authorization: insufficient evidence fails fast.
            return CoordinationResult(
                action_result=models.ActionResult(
                    action_type=models.ActionType.UPDATE_TASK, target_id=task_id or "?",
                    success=False, message="completion evidence insufficient; nothing was executed.",
                )
            )
        needs_review = rules.needs_human_approval(
            critical_blocker=flags["critical_blocker"],
            deadline_days_remaining=days,
            action_affects_commitment=flags["action_affects_commitment"],
            significant_priority_change=flags["significant_priority_change"],
            high_impact_reassignment=flags["high_impact_reassignment"],
        )
        if needs_review:
            reason = approval_mod.approval_reason(flags, days)
            if store is None:
                return CoordinationResult(
                    route=ROUTE_REVIEW,
                    pending=PendingReview(action_type, task_id, values, f"{reason}; mutation held."),
                )
            evidence = f"task={task} deadline_days={days} requester={user_id}"
            created = approval_mod.create_approval_request(
                store, conn, project_id=scope_project, task_id=task_id, action_type=action_type,
                requested_values=values, requester=user_id, reason=reason, evidence=evidence,
            )
            if not created.success or created.request is None:
                return CoordinationResult(error=created.message)
            return CoordinationResult(
                route=ROUTE_REVIEW,
                pending=PendingReview(action_type, task_id, values,
                                      f"{reason}; mutation held as {created.request.id}.", created.request.id),
            )

        if action_type == "create_task":
            title = values.get("title", "")
            res = tools.create_task(conn, project_id, title, owner_id=values.get("owner_id"))
        elif action_type == "update_task":
            res = tools.update_task(conn, task_id or "", status=values.get("status"), priority=values.get("priority"), deadline=values.get("deadline"))
        elif action_type == "assign_task":
            res = tools.assign_task(conn, task_id or "", values.get("user_id", ""))
        else:
            res = tools.create_project_comment(conn, project_id, values.get("content", ""))

        if res.status != ToolStatus.SUCCESS:
            return CoordinationResult(
                action_result=models.ActionResult(
                    action_type=models.ActionType(action_type), target_id=_target_of(action_type, task_id, res),
                    success=False, message=f"{res.status.value}: {res.message}",
                )
            )
        target = _target_of(action_type, task_id, res)
        candidate = models.ActionResult(
            action_type=models.ActionType(action_type), target_id=target,
            success=True, confirmation=json.dumps(res.data)[:500],
            message=f"{action_type} confirmed for {target}.",
        )
        # Output safety: success stands only with tool confirmation behind it.
        safety = guardrails.check_action_output(candidate, tool_confirmed=True)
        if safety.blocked:
            return CoordinationResult(
                action_result=models.ActionResult(
                    action_type=models.ActionType(action_type), target_id=target,
                    success=False, message=f"output safety blocked: {'; '.join(safety.reasons)}",
                )
            )
        return CoordinationResult(action_result=candidate)
    except Exception as exc:
        return CoordinationResult(error=f"coordination failed: {type(exc).__name__}")


def _target_of(action_type: str, task_id: str | None, res: tools.ToolResult) -> str:
    if isinstance(res.data, dict) and res.data.get("id"):
        return str(res.data["id"])
    return task_id or "?"
