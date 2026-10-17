# Implementation Plan — AI Project Operations Agent (Project 4)

Source: the project's private PDD + SDD specification documents. Those specs
are **not included** in this public repository (PDD = requirements/acceptance;
SDD = design authority); this plan records the resulting architecture and
decisions.

## 1. Final architecture

```text
Gradio UI (app.py)
  -> Input Guardrail (guardrails.py)
  -> Intake Agent -> ProjectUpdate (agents.py, models.py)
  -> Python risk/action router (business_rules.py)
  -> Project Orchestrator --agent-as-tool--> Task Worker | People Worker | Deadline Worker
       (tools: get_project_tasks/get_task | get_team_members | get_project_deadline/get_project_tasks)
       run concurrently via asyncio.gather()
  -> Evidence bundle (workflows.py, plain dict/dataclass)
  -> Status Writer -> ProjectStatus draft
  -> Evaluator -> EvaluationResult (PASS or revise, max 2 iterations)
  -> Output Guardrail
  -> ProjectStatus in Gradio, or Human Approval pause -> protected tools
       (create_task / update_task / assign_task / create_project_comment)
       -> ActionResult only from tool confirmation
```

Cross-cutting: `RunContext` (user_id, project_id, role, db handle) per run in `workflows.py`;
`SQLiteSession` (`sessions.db`) for conversation continuity; `trace(workflow_name=...)` around
WF-01 and WF-02; SQLite (`data/projectops.db`: projects, tasks, team_members, comments) as demo
source of truth via `database.py` — agents never touch it directly.

WF-01 Project Health Update (on-demand status): input guardrail → Intake → router → Orchestrator →
3 workers → merge → Writer → Evaluator → output guardrail → UI.
WF-02 Task Coordination With Approval: validate → read task/team → Python approval check →
pause if needed → approved tool executes → verify → optional comment → `ActionResult`.

Code-owns-safety vs LLM-owns-flexibility split (SDD §4.2): critical-blocker routing, ≤2-day
escalation, and approval executability are deterministic Python; which specialists to call and final
wording are LLM decisions inside Pydantic/evidence constraints.

## 2. Repository structure (proposed — create files in phase order, not all at once)

```text
AI-Project-Operations-Agent/
├── AGENTS.md
├── .env.example            # placeholder only; real .env never committed
├── .gitignore              # ignores .env, *.db
├── docs/
│   ├── AI_Project_Operations_Agent_SDD.docx      (private spec — not published)
│   ├── AI_Project_Operations_Agent_Final_PDD.docx (private spec — not published)
│   ├── IMPLEMENTATION_PLAN.md   (this file)
│   └── TRACEABILITY.md
├── app.py                  # Phase 6 — Gradio UI only
├── agents.py               # Phase 3 — 7 agents
├── models.py               # Phase 1 — 7 Pydantic models
├── tools.py                # Phase 2 — 8 function tools
├── guardrails.py           # Phase 5 — input/output/tool checks
├── business_rules.py       # Phase 1 — approval/completion/write rules
├── database.py             # Phase 1 — SQLite access, no LLM exposure
├── workflows.py            # Phase 4 — WF-01 + WF-02 (NOT /workflows/ dir; see §4 #1)
├── seed_database.py        # Phase 1 — synthetic blocked/overdue/critical/completed/unavailable cases
├── data/.gitkeep           # runtime data/projectops.db goes here (gitignored)
├── sessions.db             # runtime, gitignored (created on first run)
├── prompts/                # Phase 3 — intake_agent.txt, project_orchestrator.txt,
│                           # task_worker.txt, people_worker.txt, deadline_risk_worker.txt,
│                           # status_writer.txt, evaluator.txt
└── tests/                  # Phase 7 (+ unit tests alongside Phases 1–5)
    ├── test_business_rules.py
    ├── test_guardrails.py
    ├── test_tools.py
    ├── test_workflows.py
    └── test_demo_scenarios.py
```

Conventions: flat root modules per SDD §8.1 (not `src/`); prompts as `.md` loaded by `agents.py`;
`pyproject.toml`/`requirements.txt` + exact commands get added in Phase 1 and recorded in AGENTS.md.

## 3. Implementation phases (from PDD §15.1, with entry/exit gates)

- Phase 1 — Foundation: `pyproject`/deps, `database.py` schema (projects, tasks, team_members,
  comments), `seed_database.py` (must include blocked/overdue/critical/completed/unavailable-team
  rows), `models.py` (7 models), `business_rules.py` (3 rules verbatim). Exit: seed runs, models validate.
- Phase 2 — Tools: 8 function tools with ID/field/status validation + error returns, completion and
  approval pre-checks, failure-injection hook for scenario 10. Exit: tool unit tests pass, no direct DB writes.
- Phase 3 — Agents: 7 agents with narrow tool access + 7 prompt files, Pydantic output types,
  orchestrator agent-as-tool delegation. Exit: each agent returns typed output in isolation.
- Phase 4 — Workflows: WF-01 chain + router + `asyncio.gather()` workers + merge; WF-02
  validate→check→pause→execute→verify; revision controller (max 2). Exit: traces show chain + concurrency.
- Phase 5 — Safety: input/output guardrails wired pre-Intake / pre-response, protected-tool approval
  pause/resume/reject path, role checks. Exit: injection, unsafe-completion, and unsupported-claim tests trip.
- Phase 6 — Runtime: `RunContext` plumbing, `SQLiteSession` multi-turn continuity, `trace()` on WF-01/WF-02,
  minimal Gradio (selector, update box, health/progress/blockers/risks/overdue/actions/evidence/approval
  panel + trace/pattern debug view). Backend before UI polish. Exit: manual end-to-end + approval resume works.
- Phase 7 — Verification: 12 demo scenarios runnable, full `pytest` suite (rules, guardrails, actions,
  stops), trace evidence, README + final packaging, secrets audit. Exit: PDD §16 checklist all ticked.
  Delivered: scenario catalog `docs/DEMO_SCENARIOS.md` (live click-path + covering test per scenario),
  delivery invariants in `tests/test_delivery.py`, accurate Ollama-first `README.md`, pinned
  `requirements.txt`, and in-app run-trace evidence. The SDK remote trace viewer is the one item that
  requires a cloud tracing key, so local Ollama runs show trace evidence in-app instead (documented).

## 4. Contradictions and missing details (PDD vs SDD)

1. Workflow location: SDD §8.1 structure lists flat `workflows.py`, but WF-01 §2.1 says
   "stored under /workflows" and WF-02 §2.2 implies a directory. Decision: use flat `workflows.py`
   (matches §8.1 + PDD phasing); ignore `/workflows/` wording.
2. `ProjectEvidence` status: PDD §8.3 lists it as a validated stage output, but SDD §3.3 model list
   omits it (7 models, no `ProjectEvidence`). Decision: treat evidence bundle as unvalidated
   merge dict/dataclass in `workflows.py`, not a Pydantic model (AGENTS.md records this).
3. Model-count wording: PDD §16 says "target has 3" structured outputs while §14 lists 3 and SDD §3.3
   specifies 7. Not a conflict of substance — floor is 2/3, target is 7. Decision: implement all 7.
4. Session strictness: PDD FR-10 allows "SQLiteSession or equivalent", SDD §8.4 mandates
   `SQLiteSession`. Decision: use `SQLiteSession` (satisfies both).
5. Missing severity/health math: neither doc defines green/yellow/red or `progress_percent` thresholds.
   Must be defined in Phase 1 `business_rules.py` and tested (gap, not contradiction).
6. Missing role matrix: "team member may be limited to assigned actions" (PDD §5) has no rule text.
   Must be defined as an explicit Python permission check in Phase 5 (gap).
7. Missing SDK specifics: model name, approval/interruption API shape, trace-viewer setup, and the
   scenario-10 failure-simulation mechanism are unspecified. Resolve against current SDK docs (SDD §11
   links) in Phase 2/6 and record choices here.
8. Minor: SDD structure header says `project4-ai-ops/` while repo root is `AI-Project-Operations-Agent/`;
   seed/DB filenames (`data/projectops.db`, `sessions.db`) are consistent. Decision: keep repo root name.
