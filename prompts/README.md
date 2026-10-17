# Prompt templates — public demo set

These are **generic public demonstration templates**. They are intentionally
short and are **not** the production prompts used in the private build of this
project. They exist so a public clone runs end to end.

## How they are loaded

`agents.py` reads one file per agent at import time:

```python
PROMPTS_DIR  = Path(__file__).resolve().parent / "prompts"
PROMPT_FILES = {
    "intake": "intake_agent.txt",
    "project_orchestrator": "project_orchestrator.txt",
    "task_worker": "task_worker.txt",
    "people_worker": "people_worker.txt",
    "deadline_worker": "deadline_risk_worker.txt",
    "status_writer": "status_writer.txt",
    "evaluator": "evaluator.txt",
}

agents.load_prompt(agent_key)  # returns the file text, raises if empty
```

Every file must exist and be non-empty, or `load_prompt` raises. To use your
own wording, edit these files — no code change is required.

| key | file | structured output |
|-----|------|-------------------|
| `intake` | `intake_agent.txt` | `ProjectUpdate` |
| `project_orchestrator` | `project_orchestrator.txt` | none (plain summary) |
| `task_worker` | `task_worker.txt` | `TaskAnalysis` |
| `people_worker` | `people_worker.txt` | `PeopleAnalysis` |
| `deadline_worker` | `deadline_risk_worker.txt` | `DeadlineAnalysis` |
| `status_writer` | `status_writer.txt` | `ProjectStatus` |
| `evaluator` | `evaluator.txt` | `EvaluationResult` |

## Where safety actually lives

None of the safety guarantees come from these text files. They are enforced by
deterministic Python regardless of what a prompt says:

- `business_rules.py` — health thresholds, approval and completion rules.
- `guardrails.py` — input screening, output validation, tool-input checks.
- `tools.py` — validated, protected function tools (the only write path).
- `approval.py` — pause-before-mutation approval lifecycle.

The prompts only shape *wording and specialist selection*; the safety checks
still run, so a weak or generic prompt cannot bypass them.
