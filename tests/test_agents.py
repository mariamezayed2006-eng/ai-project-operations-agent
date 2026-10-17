"""Phase 3 tests: agent definitions, prompts, and output contracts.

Construction/configuration only — no live model or API calls.
"""

import re
from pathlib import Path

import pytest

import agents
import models
from agents import AGENTS, MODEL_NAME, OUTPUT_TYPES, PROMPT_FILES, build_agent, load_prompt

EXPECTED_KEYS = {
    "intake",
    "project_orchestrator",
    "task_worker",
    "people_worker",
    "deadline_worker",
    "status_writer",
    "evaluator",
}

FORBIDDEN_RE = re.compile(r"smarterp", re.IGNORECASE)
COT_RE = re.compile(r"chain-of-thought|chain of thought|hidden reasoning|show your work|think step by step", re.IGNORECASE)
SQL_RE = re.compile(r"sqlite3|PRAGMA|SELECT\s.+\sFROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM", re.IGNORECASE)


def test_all_seven_agents_constructed():
    """Registry holds exactly the 7 required agents with correct names."""
    assert set(AGENTS) == EXPECTED_KEYS
    assert [a.name for a in AGENTS.values()] == [
        "Intake Agent",
        "Project Orchestrator",
        "Task Worker",
        "People Worker",
        "Deadline/Risk Worker",
        "Status Writer",
        "Evaluator",
    ]


def test_output_type_mapping():
    """Each agent carries its Phase 1 output contract; orchestrator is intentionally untyped."""
    assert OUTPUT_TYPES == {
        "intake": models.ProjectUpdate,
        "task_worker": models.TaskAnalysis,
        "people_worker": models.PeopleAnalysis,
        "deadline_worker": models.DeadlineAnalysis,
        "project_orchestrator": None,
        "status_writer": models.ProjectStatus,
        "evaluator": models.EvaluationResult,
    }
    for key, agent in AGENTS.items():
        expected = OUTPUT_TYPES[key]
        if expected is None:
            assert agent.output_type is None
        else:
            # Non-strict wrapper around the unchanged Phase 1 contract.
            assert agent.output_type.output_type is expected
            assert agent.output_type._strict_json_schema is False


def test_no_tools_bound_yet():
    """Tool binding belongs to the workflow phase; definitions carry no tools or SQL."""
    for agent in AGENTS.values():
        assert agent.tools == []


def test_tool_bearing_agents_require_tool_calls():
    """Workers and orchestrator force invocation; tool-less agents keep defaults."""
    for key in ("task_worker", "people_worker", "deadline_worker", "project_orchestrator"):
        assert AGENTS[key].model_settings.tool_choice == "required"
    for key in ("intake", "status_writer", "evaluator"):
        assert not getattr(AGENTS[key].model_settings, "tool_choice", None)


def test_prompt_files_exist_and_load():
    """All 7 prompt files exist, are non-empty, and load through the helper."""
    assert set(PROMPT_FILES) == EXPECTED_KEYS
    for key in EXPECTED_KEYS:
        path = Path(agents.__file__).parent / "prompts" / PROMPT_FILES[key]
        assert path.is_file() and path.stat().st_size > 0
        assert load_prompt(key) == path.read_text(encoding="utf-8").strip()
    with pytest.raises(KeyError):
        load_prompt("nonexistent")
    with pytest.raises(KeyError):
        build_agent("nonexistent")


def test_single_model_configuration():
    """One model constant feeds every agent; override works without editing prompts."""
    assert isinstance(MODEL_NAME, str) and MODEL_NAME
    for agent in AGENTS.values():
        assert agent.model == MODEL_NAME
    custom = build_agent("intake", model="other-model")
    assert custom.model == "other-model"


def test_prompts_have_grounding_and_safety():
    """Every prompt states grounding and safety constraints in project terminology."""
    texts = {k: load_prompt(k) for k in EXPECTED_KEYS}
    for text in texts.values():
        assert "Grounding rules" in text and "Safety rules" in text
    assert "never invent" in texts["intake"].lower()
    assert "TaskAnalysis" in texts["task_worker"]
    assert "PeopleAnalysis" in texts["people_worker"]
    assert "DeadlineAnalysis" in texts["deadline_worker"]
    assert "RED_OVERDUE_COUNT = 2" in texts["deadline_worker"]
    assert "ProjectStatus" in texts["status_writer"]
    assert "EvaluationResult" in texts["evaluator"]
    assert "no SQLite access" in texts["project_orchestrator"]


def test_prompts_forbid_cot_and_sql():
    """No prompt requests chain-of-thought; none contains SQL or DB manipulation."""
    for key in EXPECTED_KEYS:
        text = load_prompt(key)
        assert not COT_RE.search(text), f"{key} requests chain-of-thought"
        assert not SQL_RE.search(text), f"{key} contains SQL"
    source = Path(agents.__file__).read_text(encoding="utf-8")
    assert not SQL_RE.search(source), "agents.py must not contain SQL"


def test_no_forbidden_project_references():
    """Agent layer references only Project 4 identity."""
    sources = [Path(agents.__file__).read_text(encoding="utf-8")]
    sources += [load_prompt(k) for k in EXPECTED_KEYS]
    for text in sources:
        assert not FORBIDDEN_RE.search(text)
    assert "Project 4" in sources[0]


def test_agents_module_imports_only_contracts():
    """agents.py depends on models/prompts, not on database or tools."""
    source = Path(agents.__file__).read_text(encoding="utf-8")
    assert "import database" not in source and "import tools" not in source


def test_agent_class_comes_from_installed_sdk():
    """Agent is the real SDK class (via the agents_sdk alias), not a stub."""
    import agents_sdk

    assert agents.Agent is agents_sdk.Agent
    assert agents.Agent.__module__.startswith("agents")
