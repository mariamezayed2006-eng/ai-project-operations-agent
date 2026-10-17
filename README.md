# AI Project Operations Agent

An agentic project-operations assistant built with the **OpenAI Agents SDK**.
It turns a short natural-language project update into a grounded,
management-ready status by running parallel specialist analysis, and it gates
every high-impact action behind deterministic safety rules and explicit human
approval.

The guiding principle: **the LLM proposes, deterministic Python disposes.**

- No direct LLM database access — all reads and writes go through typed tools.
- A successful write is never claimed without a tool confirmation.
- No side effect happens before a human approves it.
- Health, overdue, blockers, and risks are derived from database rows, not from
  model judgement.

> Synthetic demo data only. No real project, customer, or personal data is used.

---

## Overview

You send a free-text update such as *"Weekly check: the gateway fix is waiting
on the vendor."* The agent:

1. screens the input for prompt-injection / bypass attempts,
2. extracts a structured `ProjectUpdate`,
3. routes the case with deterministic Python rules,
4. fans out to task / people / deadline specialists (in parallel, or via an
   orchestrator depending on the route),
5. merges their evidence into a bundle,
6. drafts a structured `ProjectStatus`,
7. has an independent Evaluator accept or request one revision (hard cap: 2),
8. validates the output with an output guardrail,
9. and either shows the status or holds a requested action for human approval.

## Architecture

```text
Gradio UI (app.py)
  -> Input guardrail            (guardrails.py)
  -> Intake Agent -> ProjectUpdate
  -> deterministic risk router  (business_rules.py)
  -> Project Orchestrator  --agent-as-tool-->  Task | People | Deadline workers
       (read-only tools; NORMAL route fans out with asyncio.gather())
  -> evidence bundle            (workflows.py)
  -> Status Writer -> ProjectStatus
  -> Evaluator  (accept / revise, max 2)     (prompts/evaluator.txt)
  -> Output guardrail
  -> UI status   OR   human approval pause -> protected tools -> ActionResult
```

Cross-cutting: request-scoped SQLite connections (`runtime.py`), a frozen
`AppContext` carrying identity only, file-backed `SQLiteSession` for
conversation continuity, and `trace()` around both workflows.

## Features

- **7 single-purpose agents** — Intake, Project Orchestrator, Task, People,
  Deadline/Risk, Status Writer, Evaluator.
- **8 typed domain tools** — 4 read-only (`get_project_tasks`, `get_task`,
  `get_team_members`, `get_project_deadline`) and 4 protected writers
  (`create_task`, `update_task`, `assign_task`, `create_project_comment`).
- **7 Pydantic structured outputs** with strict validation.
- **Guardrails** — input screening, output grounding/consistency checks, and
  tool-input validation.
- **Human-in-the-loop approval** — `PENDING → APPROVED → EXECUTED/FAILED` (or
  `REJECTED`), pause-before-mutation, live revalidation, stored-values-only
  execution.
- **All five orchestration patterns** — prompt chaining, routing,
  parallelization, orchestrator-workers, evaluator-optimizer.
- **Authoritative health classification** — a draft cannot downgrade a
  deterministic RED.
- **Fail-closed** — every model/tool/validation failure becomes an explicit
  unavailable/error result, never a fabricated one.
- **Durable project memory** — accepted runs persist a deterministic summary for
  later context (labelled as background, never as fact).

## Requirements

- Python 3.11+ (developed and tested on 3.14).
- Windows (commands below use PowerShell), macOS/Linux fine with the obvious
  shell changes.
- [Ollama](https://ollama.com/) for the default local backend (no cloud key).
- The pinned dependencies in `requirements.txt`:

  ```text
  openai-agents==0.23.1
  gradio==6.27.0
  pydantic==2.13.5
  pytest==9.1.1
  python-dotenv==1.2.3
  ```

## Setup (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python seed_database.py            # creates data/projectops.db (synthetic data)
Copy-Item .env.example .env        # local Ollama config (contains no secrets)
```

## Ollama configuration (default backend)

The default backend is a local Ollama model — no cloud key, no rate limits.

```powershell
ollama serve                       # separate terminal
ollama pull llama3.2               # one-time download
```

`.env.example` already selects it:

```dotenv
LLM_PROVIDER=ollama
LLM_MODEL=llama3.2:latest
OLLAMA_BASE_URL=http://localhost:11434/v1
```

`LLM_PROVIDER`, `LLM_MODEL`, and `OLLAMA_BASE_URL` are read live from the
environment; a `.env` file is loaded at startup but explicit environment
variables always win. Ollama ignores auth, so the client uses a non-secret
`"ollama"` placeholder key.

### Optional cloud backends

Provider selection is environment-driven and provider-independent safety means
the guardrails/approvals are identical on every backend:

| `LLM_PROVIDER` | Backend | Key |
|----------------|---------|-----|
| `ollama` *(default)* | local Ollama | none |
| `openrouter` | OpenRouter | `OPENROUTER_API_KEY` |
| `mistral` | Mistral | `MISTRAL_API_KEY` |
| `openai` | OpenAI SDK defaults | `OPENAI_API_KEY` |
| `auto` | first configured cloud fallback | — |

Add the matching key to `.env` and set `LLM_PROVIDER` (or leave it on `ollama`).

## Launch

```powershell
python app.py                      # Gradio demo on http://127.0.0.1:7860
```

Three tabs:

1. **Health** — pick a project, type an update, click *Analyze*. Shows health,
   progress, blockers, risks, overdue tasks, recommended actions, cited
   evidence, and a run-trace panel.
2. **Task Actions** — request `create_task`, `update_task`, `assign_task`, or
   `create_project_comment`. High-impact requests return a **PENDING REVIEW —
   NOT executed** panel instead of mutating anything.
3. **Approvals** — select a pending request, read its evidence summary, then
   *Approve* (revalidate → execute → verify) or *Reject* (zero mutation).

### Interface & theme

The demo uses a deep-navy + latte theme (background `#0A1220`, cards `#111D30`,
accent `#C6A982`) applied through Gradio's theme API plus a small custom
stylesheet; the palette is defined once in `app.py` (`PALETTE`, `build_theme`,
`CUSTOM_CSS`). This is presentation only — every screen is driven by the same
runtime, guardrails, and approval logic. Semantic colors are reserved for real
states (green healthy, amber warning/review, red critical).

## Testing

```powershell
python -m pytest tests/ -q                       # full offline suite
python -m pytest tests/test_demo_scenarios.py -v # the 12 scenarios
```

The suite is fully offline-safe: no API calls and no running model server are
needed. It exercises the real guardrails, tools, approvals, workflows, and
runtime; only the single LLM call boundary is faked.

## Demo scenarios

The 12 required demonstration scenarios are catalogued in
[`docs/DEMO_SCENARIOS.md`](docs/DEMO_SCENARIOS.md), each with a live click-path
and the automated test that covers it.

**Automated coverage vs live evidence.** All 12 scenarios have automated tests
in `tests/test_demo_scenarios.py`. The screenshots in `docs/screenshots/` come
from a real browser walkthrough of `python app.py` on local Ollama; they show
the input guardrail, a high-impact hold, the approve and reject paths, and a
live health run with its run-trace panel. Scenarios that depend on a specific
model output or an injected failure (5, 9, 10) are verified by their tests
rather than a fixed click-path — this is stated explicitly in the catalog.

## Screenshots

| | |
|---|---|
| ![Live health run with run trace](docs/screenshots/01_health_red_run_trace.png) | ![Input guardrail block](docs/screenshots/02_input_guardrail_block.png) |
| Deterministic `RED` health + run-trace panel | Input guardrail blocks an injection |
| ![High-impact action held](docs/screenshots/04_high_impact_held_pending.png) | ![Rejected without execution](docs/screenshots/07_rejected_no_mutation.png) |
| High-impact action held pending approval | Reject path mutates nothing |

All screenshots were captured against a throwaway seeded database with
synthetic data; no credentials are visible.

## Project structure

```text
app.py             Gradio UI
agents.py          7 agent definitions + prompt loading
provider.py        backend/model selection
models.py          7 Pydantic models
business_rules.py  deterministic approval / completion / health rules
tools.py           8 domain function tools (only write path)
guardrails.py      input / output / tool-input checks
approval.py        approval lifecycle (pause before mutation)
workflows.py       WF-01 health + WF-02 coordination, 5 patterns
runtime.py         sessions, context, traced entrypoints
database.py        SQLite access (never exposed to the LLM)
seed_database.py   synthetic demo data
prompts/           public demo prompt templates
tests/             offline test suite
docs/              implementation, traceability, and demo documentation
```

## Prompt templates

`prompts/` ships **generic public demonstration templates** (not the private
production prompts). `agents.load_prompt()` reads one non-empty text file per
agent at import time; replace the files to use your own wording. See
[`prompts/README.md`](prompts/README.md). No safety logic lives in the prompts —
it is all deterministic Python, so a generic prompt cannot bypass it.

## How orchestration works (code vs LLM)

- **Code-orchestrated (deterministic):** input screening, risk/approval routing,
  health classification, factual-field construction, evaluator iteration cap,
  the `asyncio.gather()` fan-out, approval lifecycle, and every mutation gate.
- **LLM-orchestrated:** which specialists the Project Orchestrator invokes via
  agent-as-tool, evidence synthesis, status wording, and evaluation judgement —
  all inside Pydantic contracts and evidence constraints.

## Safety model

The LLM proposes; deterministic Python disposes. No direct LLM database access,
no success without tool confirmation, no mutation before approval, no fabricated
completion or evidence. See `business_rules.py`, `guardrails.py`, `approval.py`,
and `tools.py`.

## Known limitations

- **Small local models** (`llama3.2`) produce thin drafts and sometimes overclaim;
  the Evaluator, output guardrail, and fail-closed paths catch these and label
  the result provisional, but a stronger model gives steadier demos.
- **Remote tracing** (the hosted trace viewer) needs `OPENAI_API_KEY`; on local
  Ollama the SDK skips export and the in-app run-trace panel is the evidence.
- **Scenarios 5, 9, 10** depend on specific model output / injected failure and
  are validated by tests rather than a scripted live click-path.
- **No license file** is included; this is a personal portfolio project.

## Notes

- Specs and private working notes are intentionally not part of this repository.
- `docs/DEMO_SCENARIOS.md`, `docs/IMPLEMENTATION_PLAN.md`, and
  `docs/TRACEABILITY.md` document the implementation and demo evidence.
