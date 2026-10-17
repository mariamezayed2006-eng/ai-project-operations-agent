# Requirement Traceability Checklist — AI Project Operations Agent (Project 4)

Key: `[x]` = implemented and validated. All phases complete: foundation,
tools, agents, workflows, safety, final runtime/UI, and verification.
Live-model demo runs additionally need a reachable backend (local Ollama by
default; cloud fallbacks optional). The suite itself is offline-safe.
File names are the homes defined in `IMPLEMENTATION_PLAN.md` §2. Test files live in `tests/`.

## Functional requirements (PDD §6.1)

- [x] FR-01 Accept project updates (Gradio input + session id) → `app.py` → `tests/test_app.py`
- [x] FR-02 Extract structured data (`ProjectUpdate` contract + Intake agent definition; live extraction needs API key) → `agents.py` + `models.py` → `tests/test_agents.py`, `tests/test_workflows.py`
- [x] FR-03 Inspect project data (task/person/deadline reads) → `tools.py` (4 read tools, Phase 2 done; worker use comes in Phase 3/4) → `tests/test_tools.py`
- [x] FR-04 Route critical cases (deterministic routing in `workflows.py` via `business_rules.py`; pending-review state only, HITL execution still Phase 5) → `tests/test_workflows.py` + `tests/test_business_rules.py`
- [x] FR-05 Parallelize independent analysis (`asyncio.gather` in `gather_evidence`) → `tests/test_workflows.py`
- [x] FR-06 Coordinate specialists (orchestrator agent-as-tool wiring + delegation; NORMAL path uses parallel engine, REVIEW path uses orchestrated delegation, each worker runs at most once) → `tests/test_workflows.py`
- [x] FR-07 Generate structured status (Writer step inside WF-01 chain; UI presentation still later) → `tests/test_workflows.py`
- [x] FR-08 Evaluate and revise (bounded loop, max 2, safe exhaustion) → `tests/test_workflows.py`
- [x] FR-09 Protect actions (tool boundary + approval lifecycle, pause-before-mutation, revalidation, role policy, UI enforcement via `app.py` approval tab) → `tools.py` + `approval.py` + `guardrails.py` → `tests/test_tools.py`, `tests/test_approval.py`, `tests/test_app.py` + scenarios 8, 11
- [x] FR-10 Session continuity (`SQLiteSession` file-backed in `sessions.db`, threaded into every agent step) → `runtime.py` + `workflows.py` → `tests/test_runtime.py`
- [x] FR-11 Traces (`trace()` around WF-01/WF-02; offline-safe, export skipped without key) → `workflows.py` + `runtime.py` → `tests/test_demo_scenarios.py` (sc. 12)
- [x] FR-12 Graceful recovery (tool layer Phase 2 + workflow envelopes Phase 4 + guardrail/approval failure handling Phase 5; full demo validation still later) → `workflows.py` + `tools.py` + `guardrails.py` + `approval.py` → `tests/test_tools.py`, `tests/test_workflows.py`, `tests/test_approval.py`

## Non-functional (PDD §6.2) / Safety (PDD §9, SDD §6, §9)

- [x] Safety: no LLM bypass of auth/approval (deterministic rules + role policy + approval lifecycle) → `business_rules.py` + `tools.py` + `approval.py` + `guardrails.py` → `tests/test_business_rules.py`, `tests/test_approval.py`
- [x] Grounding: claims need evidence IDs → `models.py` (`ProjectStatus.evidence_task_ids`) + Evaluator + output guardrail (wired into WF-01/WF-02) → `tests/test_guardrails.py` + scenario 9
- [x] Reliability: tool/model/validation/empty failures handled at every layer → `tools.py` + `workflows.py` + `guardrails.py` + `approval.py` → `tests/test_tools.py`, `tests/test_workflows.py`, `tests/test_demo_scenarios.py` (sc. 10)
- [x] Observability: trace covers WF-01/WF-02; session ids thread through runs → `workflows.py` + `runtime.py` → sc. 12 check
- [x] Maintainability: rules/tools/agents/workflows/safety/runtime separated → file layout in plan §2
- [x] Usability: Gradio shows health/blockers/risks/overdue/actions/evidence/approval state with honest pending wording → `app.py` → `tests/test_app.py` + scenario 11
- [x] Security: synthetic data + env keys only → `seed_database.py` + `.env.example` → repo audit (no `.db`/`.env` committed; approval store process-scoped by design)
- [x] Determinism: safety math in Python; role/approval/guardrail decisions never from LLM claims → `business_rules.py` + `guardrails.py` + `approval.py` → `tests/test_business_rules.py`, `tests/test_approval.py`
- [x] Authoritative health: final classification derived via `derive_health` from DB rows (a YELLOW draft over RED-rule evidence is corrected, never presented) → `workflows.py` + `business_rules.py` → `tests/test_workflows.py`
- [x] Schema migration: `ensure_db()` runs idempotent `init_db()` on existing files so `project_memory` is added without reseeding or data loss → `app.py` + `database.py` → `tests/test_app.py`
- [x] Input guardrail (deterministic intent screening pre-Intake; allow/block/review) → `guardrails.py` (wired into WF-01) → `tests/test_guardrails.py` + scenario 7
- [x] Output guardrail (grounding/contradiction/commitment checks pre-response + action validation) → `guardrails.py` (wired into WF-01/WF-02) → `tests/test_guardrails.py` + scenario 9
- [x] Human approval: pending→approved/rejected→executed/failed lifecycle, pause-before-mutation, revalidation, immutability → `approval.py` + `workflows.py` → `tests/test_approval.py` + scenario 11
- [x] No fabricated completion / no unsupported deadline / no direct LLM writes (tool-layer part done in Phase 2; evaluator/guardrail part still later) → `business_rules.py` + `tools.py` + `database.py` → `tests/test_tools.py` + scenario 8

## Agents (7) → `agents.py` + `prompts/*.txt` (Phase 3: definitions only, no orchestration)

- [x] Intake (no tools → `ProjectUpdate`) · [x] Orchestrator (workers as future tools; coordination summary, untyped by design) · [x] Task Worker (`get_project_tasks`, `get_task` → `TaskAnalysis`) · [x] People Worker (`get_team_members` → `PeopleAnalysis`) · [x] Deadline Worker (`get_project_deadline`, `get_project_tasks` → `DeadlineAnalysis`) · [x] Status Writer (evidence-only → `ProjectStatus`) · [x] Evaluator (draft+evidence → `EvaluationResult`)

## Tools (8) → `tools.py` over `database.py`

- [x] Read-only: `get_project_tasks`, `get_task`, `get_team_members`, `get_project_deadline` → `tests/test_tools.py`
- [x] Protected execution boundary: `create_task`, `update_task` (completion gate: stored 100% required), `assign_task` (same-project member check), `create_project_comment` → `tests/test_tools.py` (approval routing still Phase 5)

## Orchestration patterns (5) → `workflows.py`

- [x] Prompt Chaining (WF-01/WF-02 step order in `workflows.py`, typed handoffs) → `tests/test_workflows.py`
- [x] Routing (deterministic Python to pending-review path) → `tests/test_workflows.py`
- [x] Parallelization (`asyncio.gather()` in `gather_evidence`) → `tests/test_workflows.py`
- [x] Orchestrator-Workers (agent-as-tool wiring + delegated path) → `tests/test_workflows.py`
- [x] Evaluator-Optimizer (bounded loop, stop at PASS or 2) → `tests/test_workflows.py`
- [x] Code + LLM split (Python safety/routing/cap/gather; LLM selection/synthesis/wording — marked in code) → `tests/test_workflows.py`

## Models (7) → `models.py`

- [x] `ProjectUpdate` · [x] `TaskAnalysis` · [x] `PeopleAnalysis` · [x] `DeadlineAnalysis` · [x] `ProjectStatus` · [x] `EvaluationResult` · [x] `ActionResult` (Phase 1; evidence bundle stays a plain merge object in future `workflows.py`, not Pydantic — see plan §4 #2)

## Demo scenarios (12) → `tests/test_demo_scenarios.py` + Gradio manual runs

- [x] 1 happy path · [x] 2 3+ sequential tools · [x] 3 routing trigger · [x] 4 parallelization · [x] 5 orchestrator-workers · [x] 6 evaluator rejection→revision · [x] 7 input guardrail · [x] 8 tool block · [x] 9 output guardrail · [x] 10 tool-failure partial result · [x] 11 human escalation resume/reject · [x] 12 trace visible — all in `tests/test_demo_scenarios.py` (LLM steps faked at the seam); live click-paths catalogued in `docs/DEMO_SCENARIOS.md`; live-model demo needs a reachable backend (local Ollama by default, cloud fallbacks optional)

## Delivery / packaging (PDD §15 Phase 7, §16)

- [x] Setup/run/demo/test instructions accurate for the default local Ollama backend → `README.md` → `tests/test_delivery.py`
- [x] 12 demo scenarios documented with live click-paths and covering test → `docs/DEMO_SCENARIOS.md` → `tests/test_delivery.py`, `tests/test_demo_scenarios.py`
- [x] No secrets, `.db`, or session files committed; committed config example is a secret-free Ollama template → `.gitignore` + `.env.example` → `tests/test_delivery.py`
- [x] Runtime dependencies pinned → `requirements.txt` → `tests/test_delivery.py`
- [x] Tracing: `trace()` wraps WF-01/WF-02 and the in-app run-trace panel reports the executed path without a key; SDK remote trace export needs `OPENAI_API_KEY` (skipped honestly on local Ollama) → `workflows.py` + `app.py` → sc. 12, `tests/test_app.py`
- [x] Captured UI / run-trace screenshots from a real browser walkthrough (local Ollama, throwaway database) indexed in the demo catalog → `docs/screenshots/` + `docs/DEMO_SCENARIOS.md` → `tests/test_delivery.py`

## PDD §16 final checklist (acceptance gate, Phase 7)

- [x] SDK used (Agent/Runner/tools/guardrails/sessions/approvals/tracing) · [x] 7 agents · [x] 8 tools · [x] 7 structured outputs · [x] input+output+tool safety · [x] RunContext-equivalent AppContext + SQLiteSession · [x] all 5 patterns demonstrated · [x] code+LLM split · [x] human approval enforced · [x] failure paths safe · [x] Gradio usable · [x] 12 scenarios validated (faked LLM seam; live runs need a reachable backend — local Ollama by default) · [x] no secrets/data committed · [x] README + demo catalog + pinned deps (final packaging) → `tests/test_delivery.py`
