"""Agent definitions for the AI Project Operations Agent (Project 4).

Phase 3: definitions only. Each agent pairs SDK ``Agent`` construction
(verified against installed ``openai-agents`` 0.23.x:
``Agent(name, instructions, model, output_type, tools)``) with a prompt
file under ``prompts/`` and a Pydantic output contract from ``models.py``.

Deliberately NOT in this phase (see plan): tool binding, orchestration,
routing, parallelization, revision loops, sessions, context, guardrails,
tracing, and UI. Agents are constructed with no function tools; the
workflow/runtime phase will attach safe tool wrappers around the
connection-taking functions in ``tools.py`` without changing their
signatures or creating a global database connection.

The Project Orchestrator has ``output_type=None`` on purpose: its product
is a coordination summary merged by workflow code into the (non-Pydantic)
evidence bundle (plan contradiction #2). All other agents map to the
existing Phase 1 models; no new models are introduced here.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

import models
import provider


def _import_agents_sdk() -> Any:
    """Import the installed ``openai-agents`` package despite our SDD-mandated filename.

    This module must stay named ``agents.py`` (SDD Section 8.1), which shadows
    the SDK's top-level ``agents`` package on ``sys.path``. The guarded import
    below temporarily lifts our own module and repo-root path entries aside,
    imports the real SDK, registers it as ``agents_sdk`` for this and later
    phases, then restores everything. No behavior or architecture change.
    """
    root = Path(__file__).resolve().parent
    saved_path = sys.path[:]
    me = sys.modules.pop(__name__, None)
    try:
        sys.path[:] = [p for p in saved_path if Path(p or os.getcwd()).resolve() != root]
        sdk = importlib.import_module("agents")
        sys.modules.setdefault("agents_sdk", sdk)
        return sdk
    finally:
        sys.path[:] = saved_path
        if me is not None:
            sys.modules[__name__] = me


Agent = _import_agents_sdk().Agent
AgentOutputSchema = _import_agents_sdk().AgentOutputSchema
ModelSettings = _import_agents_sdk().ModelSettings

# Agents that carry function tools must actually invoke them: several
# free-tier models otherwise answer from prior knowledge and skip the
# tool call entirely (observed live). "required" only affects tool-bearing
# agents; Intake/Writer/Evaluator have no tools and keep default behavior.
TOOL_MANDATORY_AGENTS = frozenset({"project_orchestrator", "task_worker", "people_worker", "deadline_worker"})

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Single configurable model for every agent (see .env.example: LLM_MODEL).
# Default is the OpenRouter free router; no secrets live here — change the
# model in one place via the environment.
MODEL_NAME: str = provider.get_model_name()

PROMPT_FILES: dict[str, str] = {
    "intake": "intake_agent.txt",
    "project_orchestrator": "project_orchestrator.txt",
    "task_worker": "task_worker.txt",
    "people_worker": "people_worker.txt",
    "deadline_worker": "deadline_risk_worker.txt",
    "status_writer": "status_writer.txt",
    "evaluator": "evaluator.txt",
}

OUTPUT_TYPES: dict[str, Any] = {
    "intake": models.ProjectUpdate,
    "task_worker": models.TaskAnalysis,
    "people_worker": models.PeopleAnalysis,
    "deadline_worker": models.DeadlineAnalysis,
    "project_orchestrator": None,
    "status_writer": models.ProjectStatus,
    "evaluator": models.EvaluationResult,
}


def load_prompt(agent_key: str) -> str:
    """Load and validate the prompt file for an agent key."""
    try:
        filename = PROMPT_FILES[agent_key]
    except KeyError:
        raise KeyError(f"Unknown agent key: {agent_key}") from None
    text = (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Prompt file {filename} is empty.")
    return text


def build_agent(agent_key: str, model: str = MODEL_NAME) -> Agent:
    """Construct one agent with its prompt and output contract (no tools bound).

    Output contracts are wrapped as non-strict schemas: our Phase 1 models
    use ``extra="forbid"`` and free-form ``dict`` values, which the SDK's
    strict-schema validator rejects before any network call. Pydantic
    validation of the returned JSON still applies, so the contract is
    unchanged — only provider-side strict enforcement is off (required for
    the OpenRouter free pool, whose models vary in strict-mode support).
    """
    names = {
        "intake": "Intake Agent",
        "project_orchestrator": "Project Orchestrator",
        "task_worker": "Task Worker",
        "people_worker": "People Worker",
        "deadline_worker": "Deadline/Risk Worker",
        "status_writer": "Status Writer",
        "evaluator": "Evaluator",
    }
    if agent_key not in names:
        raise KeyError(f"Unknown agent key: {agent_key}")
    output_type = OUTPUT_TYPES[agent_key]
    if output_type is not None:
        output_type = AgentOutputSchema(output_type, strict_json_schema=False)
    kwargs: dict[str, Any] = {}
    if agent_key in TOOL_MANDATORY_AGENTS:
        kwargs["model_settings"] = ModelSettings(tool_choice="required")
    return Agent(
        name=names[agent_key],
        instructions=load_prompt(agent_key),
        model=model,
        output_type=output_type,
        **kwargs,
    )


intake_agent: Agent = build_agent("intake")
project_orchestrator: Agent = build_agent("project_orchestrator")
task_worker: Agent = build_agent("task_worker")
people_worker: Agent = build_agent("people_worker")
deadline_worker: Agent = build_agent("deadline_worker")
status_writer: Agent = build_agent("status_writer")
evaluator: Agent = build_agent("evaluator")

AGENTS: dict[str, Agent] = {
    "intake": intake_agent,
    "project_orchestrator": project_orchestrator,
    "task_worker": task_worker,
    "people_worker": people_worker,
    "deadline_worker": deadline_worker,
    "status_writer": status_writer,
    "evaluator": evaluator,
}
