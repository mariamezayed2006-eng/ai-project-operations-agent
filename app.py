"""Gradio demo UI for the AI Project Operations Agent (Project 4).

Final phase: thin presentation over the safety-preserving runtime layer.
Every action flows through ``runtime`` → ``workflows`` → guardrails,
tools, and approvals — the UI adds no bypasses. Pending approvals are
labeled as NOT executed until a human approves and the tool confirms.

Run: ``python app.py`` (seeds the synthetic demo database on first launch).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import gradio as gr

import approval
import database
import runtime
import seed_database

STORE = approval.ApprovalStore()
DB_PATH = runtime.DEFAULT_DB_PATH


def ensure_db() -> None:
    """Seed the synthetic demo database when missing or empty; migrate schema otherwise.

    ``init_db`` is idempotent (CREATE TABLE IF NOT EXISTS), so running it on
    a pre-existing file safely adds tables introduced after the file was
    created (e.g. ``project_memory``) without touching existing rows. Seeding
    still recreates only missing/empty databases.
    """
    if not DB_PATH.exists():
        seed_database.seed(DB_PATH)
        return
    conn = database.get_connection(DB_PATH)
    try:
        database.init_db(conn)
        if not database.list_projects(conn):
            conn.close()
            seed_database.seed(DB_PATH)
            return
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _project_choices() -> list[str]:
    return [p["id"] for p in runtime.list_projects(DB_PATH)] or ["P001"]


def _member_choices(project_id: str) -> list[str]:
    try:
        return [m["id"] for m in runtime.list_members(DB_PATH, project_id)] or ["U001"]
    except Exception:
        return ["U001"]


def format_health(result: Any) -> str:
    """Render a WF-01 envelope as honest markdown (no invented facts)."""
    if result.blocked:
        return f"⛔ **Input blocked by guardrail.**\n\n{result.error}\n\nNo analysis ran and nothing changed."
    if result.error is not None or result.status is None:
        return f"⚠️ **Could not produce a trustworthy status.**\n\n{result.error or result.warning}"
    s = result.status
    lines = [
        f"## Health: {s.health.value.upper()}",
        f"**Progress:** {s.progress_percent}%   **Route:** {result.route}",
        "",
        f"**Blockers:** {'; '.join(s.blockers) if s.blockers else 'none'}",
        f"**Risks:** {'; '.join(s.risks) if s.risks else 'none'}",
        f"**Overdue:** {', '.join(s.overdue_tasks) if s.overdue_tasks else 'none'}",
        f"**Recommended:** {'; '.join(s.recommended_actions) if s.recommended_actions else 'none'}",
        f"**Evidence:** {', '.join(s.evidence_task_ids) if s.evidence_task_ids else 'none cited'}",
    ]
    if result.evidence is not None and result.evidence.unavailable:
        lines.append(f"**Unavailable evidence:** {'; '.join(result.evidence.unavailable)}")
    if s.needs_human_approval:
        lines.append("\n🟡 **Needs human approval** — high-impact items are held for review.")
    if result.warning:
        lines.append(f"\n⚠️ {result.warning}")
    if not result.accepted:
        lines.append("\n_Evaluator did not fully pass this draft; treat it as provisional._")
    return "\n".join(lines)


def format_run_trace(result: Any) -> str:
    """Render which workflow path and patterns actually executed (observability only)."""
    engine = "parallel workers (asyncio.gather)" if result.evidence_path == "parallel" else (
        "orchestrator-led delegation (agent-as-tool)" if result.evidence_path == "orchestrated"
        else "no evidence engine (chain stopped early)")
    return "\n".join([
        "---",
        "**Run trace:** WF-01 Project Health Update — "
        "Intake → deterministic routing → evidence → Status Writer → Evaluator "
        f"(revisions used: {result.iterations}/2) → output guardrail.",
        f"**Route:** {result.route} · **Evidence engine:** {engine}.",
        "**Decisions:** code owns routing, iteration cap, and factual fields; "
        "LLM owns specialist selection, synthesis, and wording.",
    ])


def health_fn(project_id: str, user_id: str, message: str, session_id: str) -> str:
    """Run WF-01; transport failures surface as honest errors, never statuses."""
    try:
        result = asyncio.run(runtime.run_health_update(
            DB_PATH, session_id=session_id or runtime.new_session_id(),
            user_id=user_id, project_id=project_id, message=message))
    except Exception as exc:
        return f"⚠️ **Request failed safely:** {type(exc).__name__}. Nothing was changed."
    return format_health(result) + "\n" + format_run_trace(result)


def action_fn(project_id: str, user_id: str, session_id: str, action_type: str,
              task_id: str, title: str, owner_id: str, new_owner: str,
              status: str, priority: str, deadline: str, content: str) -> str:
    """Request a task action; review-required work returns pending, never executes."""
    values: dict[str, Any] = {}
    if action_type == "create_task":
        values = {"title": title.strip(), "owner_id": owner_id.strip() or None}
    elif action_type == "update_task":
        if status:
            values["status"] = status
        if priority:
            values["priority"] = priority
        if deadline.strip():
            values["deadline"] = deadline.strip()
    elif action_type == "assign_task":
        values = {"user_id": new_owner.strip()}
    else:
        values = {"content": content.strip()}
    try:
        result = asyncio.run(runtime.run_coordination(
            DB_PATH, STORE, session_id=session_id or runtime.new_session_id(),
            user_id=user_id, project_id=project_id, action_type=action_type,
            task_id=task_id.strip() or None, values=values))
    except Exception as exc:
        return f"⚠️ **Request failed safely:** {type(exc).__name__}. Nothing was changed."
    if result.error is not None:
        return f"⚠️ **Request rejected safely:** {result.error}"
    if result.pending is not None:
        pending = result.pending
        return (
            f"⏳ **PENDING REVIEW — NOT executed.**\n\n"
            f"Request {pending.request_id or '(hold)'}: {pending.action_type} on "
            f"{pending.task_id or project_id}.\n\nReason: {pending.reason}\n\n"
            "Decide in the Approvals tab. Nothing has changed in the database."
        )
    action = result.action_result
    if action is not None and action.success:
        return f"✅ **Executed and verified.**\n\n{action.message}\n\nConfirmation: {action.confirmation}"
    detail = action.message if action is not None else "unknown failure"
    return f"❌ **Not executed.**\n\n{detail}"


def approvals_fn() -> list[str]:
    """List pending approval ids for the reviewer dropdown."""
    return [rid for rid, req in STORE.requests.items() if req.status == approval.ApprovalStatus.PENDING]


def approval_detail(request_id: str) -> str:
    """Show one request's evidence summary, or a safe message when unknown."""
    req = STORE.get(request_id) if request_id else None
    if req is None:
        return "Select a pending request."
    return approval.pending_summary(req)


def decide_fn(request_id: str, approver: str, decision: str) -> str:
    """Approve (revalidate→execute→verify) or reject (zero mutation)."""
    if not request_id:
        return "Select a pending request first."
    with runtime.request_connection(DB_PATH) as conn:
        result = approval.resolve_approval(STORE, conn, request_id, decision, approver or "approver")
    action = result.action_result if result is not None else None
    if result is not None and result.error is not None:
        return f"⚠️ {result.error}"
    if action is not None and action.success:
        return f"✅ **Executed after approval.**\n\n{action.message}"
    detail = action.message if action is not None else "no result"
    return f"**Resolved without execution.**\n\n{detail}"


def build_ui() -> gr.Blocks:
    """Construct the demo interface (no side effects until launched)."""
    ensure_db()
    projects = _project_choices()
    default_project = projects[0]
    with gr.Blocks(title="AI Project Operations Agent (Project 4)") as demo:
        gr.Markdown("# AI Project Operations Agent — Project 4\n"
                    "Grounded project analysis with guardrails and human approval. "
                    "Pending actions are **NOT executed** until approved and tool-confirmed.")
        with gr.Tab("Health"):
            project = gr.Dropdown(projects, value=default_project, label="Project")
            user = gr.Dropdown(_member_choices(default_project), value="U001", label="User")
            message = gr.Textbox(label="Project update", lines=4,
                                 placeholder="e.g. Weekly check: gateway fix is waiting on the vendor.")
            session = gr.Textbox(value=runtime.new_session_id(), label="Session id (continuity)")
            run_btn = gr.Button("Analyze")
            out = gr.Markdown()
            run_btn.click(health_fn, [project, user, message, session], out)
        with gr.Tab("Task Actions"):
            a_project = gr.Dropdown(projects, value=default_project, label="Project")
            a_user = gr.Dropdown(_member_choices(default_project), value="U001", label="Requester")
            a_session = gr.Textbox(value=runtime.new_session_id(), label="Session id")
            a_type = gr.Dropdown(["create_task", "update_task", "assign_task", "create_project_comment"],
                                 value="create_project_comment", label="Action")
            a_task = gr.Textbox(label="Task id (for update/assign)")
            a_title = gr.Textbox(label="Title (for create)")
            a_owner = gr.Textbox(label="Owner id (for create)")
            a_new_owner = gr.Textbox(label="New owner id (for assign)")
            a_status = gr.Dropdown(["", "todo", "in_progress", "blocked", "completed"], value="", label="Status")
            a_priority = gr.Dropdown(["", "low", "medium", "high", "critical"], value="", label="Priority")
            a_deadline = gr.Textbox(label="Deadline YYYY-MM-DD (for update)")
            a_content = gr.Textbox(label="Comment (for comment)", lines=2)
            a_btn = gr.Button("Request action")
            a_out = gr.Markdown()
            a_btn.click(action_fn, [a_project, a_user, a_session, a_type, a_task, a_title,
                                    a_owner, a_new_owner, a_status, a_priority, a_deadline, a_content], a_out)
        with gr.Tab("Approvals"):
            pending = gr.Dropdown(approvals_fn(), label="Pending requests")
            refresh = gr.Button("Refresh list")
            detail = gr.Markdown()
            approver = gr.Textbox(value="U001", label="Approver")
            approve_btn = gr.Button("Approve → revalidate → execute")
            reject_btn = gr.Button("Reject (zero mutation)")
            d_out = gr.Markdown()
            refresh.click(lambda: gr.update(choices=approvals_fn(), value=None), None, pending)
            pending.change(approval_detail, pending, detail)
            approve_btn.click(lambda r, a: decide_fn(r, a, "approve"), [pending, approver], d_out)
            reject_btn.click(lambda r, a: decide_fn(r, a, "reject"), [pending, approver], d_out)
    return demo


if __name__ == "__main__":
    ensure_db()
    build_ui().launch()
