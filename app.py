"""Gradio demo UI for the AI Project Operations Agent (Project 4).

Thin presentation over the safety-preserving runtime layer. Every action
flows through ``runtime`` -> ``workflows`` -> guardrails, tools, and
approvals — the UI adds no bypasses. Pending approvals are labeled as NOT
executed until a human approves and the tool confirms.

The visual theme is a deep-navy product console with warm latte accents
(see ``PALETTE`` / ``build_theme`` / ``CUSTOM_CSS``). Styling only: the
handler functions below are the same safety-preserving logic as before.

Run: ``python app.py`` (seeds the synthetic demo database on first launch).
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import date
from typing import Any

import gradio as gr

import approval
import business_rules as rules
import database
import runtime
import seed_database

STORE = approval.ApprovalStore()
DB_PATH = runtime.DEFAULT_DB_PATH


# --- Visual system: deep navy + latte -----------------------------------------

PALETTE = {
    "bg": "#0A1220",        # main application background
    "card": "#111D30",      # cards and panels
    "surface": "#243650",   # secondary surfaces / navigation
    "accent": "#C6A982",    # primary latte accent
    "cream": "#E8D8C2",     # warm cream highlights
    "text": "#F2F0EB",      # main text
    "muted": "#A9B4C5",     # secondary text
    "border": "#2B3A50",    # borders and dividers
    "ok": "#22C55E",        # healthy / success
    "warn": "#F59E0B",      # warning
    "crit": "#EF4444",      # critical / error
}

# Health value -> (label, semantic tone). Colors are used only for real states.
HEALTH_META: dict[str, tuple[str, str]] = {
    "green": ("Healthy", "ok"),
    "yellow": ("Warning", "warn"),
    "red": ("Critical", "crit"),
}

TASK_HEADERS = ["ID", "Task", "Status", "Priority", "Owner", "Deadline", "Progress", "Blocker"]

CUSTOM_CSS = """
.gradio-container { background: #0A1220 !important; }
.gradio-container .prose { color: #F2F0EB; }

.app-header { padding: 4px 2px 18px; border-bottom: 1px solid #2B3A50; margin-bottom: 20px; }
.app-header .eyebrow { color: #C6A982; text-transform: uppercase; letter-spacing: 2.4px; font-size: 11px; font-weight: 700; }
.app-header h1 { color: #E8D8C2; font-size: 26px; font-weight: 700; margin: 8px 0 6px; letter-spacing: .2px; }
.app-header .sub { color: #A9B4C5; font-size: 13.5px; margin: 0; max-width: 900px; }

.section-head { margin: 2px 0 10px; border-left: 3px solid #243650; padding-left: 10px; }
.section-title { color: #E8D8C2; font-size: 15px; font-weight: 650; letter-spacing: .2px; }
.section-sub { color: #A9B4C5; font-size: 12.5px; margin: 3px 0 0; }

.panel { background: #111D30; border: 1px solid #2B3A50; border-radius: 12px; padding: 16px 18px; }

.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 12px; }
.metric-card { background: #111D30; border: 1px solid #2B3A50; border-radius: 10px; padding: 14px 16px; }
.metric-label { color: #A9B4C5; font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; font-weight: 700; }
.metric-value { color: #F2F0EB; font-size: 23px; font-weight: 700; margin-top: 7px; line-height: 1.1; }
.metric-value.ok { color: #22C55E; } .metric-value.warn { color: #F59E0B; }
.metric-value.crit { color: #EF4444; } .metric-value.accent { color: #C6A982; }
.metric-sub { color: #A9B4C5; font-size: 12px; margin-top: 5px; }

.progress-track { height: 8px; background: #16233a; border: 1px solid #2B3A50; border-radius: 6px; overflow: hidden; margin-top: 10px; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #C6A982, #E8D8C2); }

.status-pill { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .5px; text-transform: uppercase; }
.status-pill.ok { background: rgba(34,197,94,.14); color: #22C55E; border: 1px solid rgba(34,197,94,.40); }
.status-pill.warn { background: rgba(245,158,11,.14); color: #F59E0B; border: 1px solid rgba(245,158,11,.40); }
.status-pill.crit { background: rgba(239,68,68,.14); color: #EF4444; border: 1px solid rgba(239,68,68,.42); }
.status-pill.accent { background: rgba(198,169,130,.14); color: #C6A982; border: 1px solid rgba(198,169,130,.42); }
.status-pill.neutral { background: rgba(169,180,197,.12); color: #A9B4C5; border: 1px solid #2B3A50; }

.banner { border: 1px solid #2B3A50; border-left-width: 3px; background: #111D30; border-radius: 10px; padding: 14px 16px; margin: 4px 0; }
.banner.ok { border-left-color: #22C55E; }
.banner.warn { border-left-color: #F59E0B; }
.banner.crit { border-left-color: #EF4444; }
.banner.accent { border-left-color: #C6A982; }
.banner.neutral { border-left-color: #2B3A50; }
.banner .b-title { color: #E8D8C2; font-weight: 650; font-size: 15px; }
.banner .b-sub { color: #A9B4C5; font-size: 13px; margin-top: 4px; }

.stage-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.stage { display: flex; align-items: center; gap: 8px; background: #111D30; border: 1px solid #2B3A50; border-radius: 8px; padding: 8px 12px; }
.stage .dot { width: 8px; height: 8px; border-radius: 50%; background: #3b4d68; flex: 0 0 auto; }
.stage.done .dot { background: #22C55E; } .stage.active .dot { background: #C6A982; }
.stage.review .dot { background: #F59E0B; } .stage.failed .dot { background: #EF4444; }
.stage .s-name { color: #F2F0EB; font-size: 12.5px; font-weight: 650; }
.stage .s-state { color: #A9B4C5; font-size: 10.5px; text-transform: uppercase; letter-spacing: .6px; }

.req-row { display: flex; justify-content: space-between; gap: 16px; padding: 8px 0; border-bottom: 1px solid #2B3A50; }
.req-row:last-child { border-bottom: none; }
.req-row .k { color: #A9B4C5; font-size: 11.5px; text-transform: uppercase; letter-spacing: .7px; font-weight: 650; }
.req-row .v { color: #F2F0EB; font-size: 13.5px; text-align: right; }

.hint { color: #A9B4C5; font-size: 12.5px; }
"""


def build_theme() -> Any:
    """Deep-navy + latte theme. Unknown keys are filtered for version safety."""
    base = gr.themes.Base()
    known = set(dir(base))
    navy = PALETTE["bg"]
    text = PALETTE["text"]
    values = {
        "body_background_fill": navy,
        "body_background_fill_dark": navy,
        "body_text_color": text,
        "body_text_color_dark": text,
        "body_text_color_subdued": PALETTE["muted"],
        "body_text_color_subdued_dark": PALETTE["muted"],
        "background_fill_primary": PALETTE["card"],
        "background_fill_primary_dark": PALETTE["card"],
        "background_fill_secondary": PALETTE["surface"],
        "background_fill_secondary_dark": PALETTE["surface"],
        "block_background_fill": PALETTE["card"],
        "block_background_fill_dark": PALETTE["card"],
        "block_border_color": PALETTE["border"],
        "block_border_color_dark": PALETTE["border"],
        "block_border_width": "1px",
        "block_radius": "10px",
        "block_padding": "16px",
        "block_label_background_fill": PALETTE["surface"],
        "block_label_background_fill_dark": PALETTE["surface"],
        "block_label_text_color": PALETTE["muted"],
        "block_label_text_color_dark": PALETTE["muted"],
        "block_title_text_color": PALETTE["cream"],
        "block_title_text_color_dark": PALETTE["cream"],
        "border_color_primary": PALETTE["border"],
        "border_color_primary_dark": PALETTE["border"],
        "border_color_accent": PALETTE["accent"],
        "border_color_accent_dark": PALETTE["accent"],
        "color_accent": PALETTE["accent"],
        "color_accent_soft": "#1b2a42",
        "color_accent_soft_dark": "#1b2a42",
        "link_text_color": PALETTE["accent"],
        "link_text_color_dark": PALETTE["accent"],
        "link_text_color_hover": PALETTE["cream"],
        "link_text_color_hover_dark": PALETTE["cream"],
        "link_text_color_visited": PALETTE["accent"],
        "link_text_color_visited_dark": PALETTE["accent"],
        "button_primary_background_fill": PALETTE["accent"],
        "button_primary_background_fill_dark": PALETTE["accent"],
        "button_primary_background_fill_hover": PALETTE["cream"],
        "button_primary_background_fill_hover_dark": PALETTE["cream"],
        "button_primary_text_color": "#0A1220",
        "button_primary_text_color_dark": "#0A1220",
        "button_primary_text_color_hover": "#0A1220",
        "button_primary_text_color_hover_dark": "#0A1220",
        "button_primary_border_color": PALETTE["accent"],
        "button_primary_border_color_dark": PALETTE["accent"],
        "button_secondary_background_fill": PALETTE["surface"],
        "button_secondary_background_fill_dark": PALETTE["surface"],
        "button_secondary_background_fill_hover": "#2e4463",
        "button_secondary_background_fill_hover_dark": "#2e4463",
        "button_secondary_text_color": text,
        "button_secondary_text_color_dark": text,
        "button_secondary_border_color": PALETTE["border"],
        "button_secondary_border_color_dark": PALETTE["border"],
        "button_cancel_background_fill": "#1a2233",
        "button_cancel_background_fill_dark": "#1a2233",
        "button_cancel_text_color": "#F0C7C7",
        "button_cancel_text_color_dark": "#F0C7C7",
        "button_cancel_border_color": PALETTE["border"],
        "button_cancel_border_color_dark": PALETTE["border"],
        "button_large_radius": "8px",
        "button_medium_radius": "8px",
        "button_small_radius": "8px",
        "input_background_fill": "#0C1526",
        "input_background_fill_dark": "#0C1526",
        "input_background_fill_focus": "#0C1526",
        "input_background_fill_focus_dark": "#0C1526",
        "input_border_color": PALETTE["border"],
        "input_border_color_dark": PALETTE["border"],
        "input_border_color_focus": PALETTE["accent"],
        "input_border_color_focus_dark": PALETTE["accent"],
        "input_border_color_hover": "#3a4d68",
        "input_border_color_hover_dark": "#3a4d68",
        "input_placeholder_color": PALETTE["muted"],
        "input_placeholder_color_dark": PALETTE["muted"],
        "input_radius": "8px",
        "table_even_background_fill": "#0F1A2C",
        "table_even_background_fill_dark": "#0F1A2C",
        "table_odd_background_fill": PALETTE["card"],
        "table_odd_background_fill_dark": PALETTE["card"],
        "table_border_color": PALETTE["border"],
        "table_border_color_dark": PALETTE["border"],
        "table_text_color": text,
        "table_text_color_dark": text,
        "table_radius": "10px",
        "checkbox_background_color": "#0C1526",
        "checkbox_background_color_dark": "#0C1526",
        "checkbox_background_color_selected": PALETTE["accent"],
        "checkbox_background_color_selected_dark": PALETTE["accent"],
        "checkbox_border_color": PALETTE["border"],
        "checkbox_border_color_dark": PALETTE["border"],
        "shadow_drop": "0 1px 2px rgba(0,0,0,0.45)",
        "shadow_drop_lg": "0 10px 26px rgba(0,0,0,0.38)",
        "slider_color": PALETTE["accent"],
        "loader_color": PALETTE["accent"],
    }
    return base.set(**{k: v for k, v in values.items() if k in known})


# --- Small HTML helpers -------------------------------------------------------


def _e(value: Any) -> str:
    """HTML-escape any dynamic value before it touches the page."""
    return html.escape(str(value))


def _pill(text: Any, tone: str) -> str:
    return f'<span class="status-pill {tone}">{_e(text)}</span>'


def _section(title: str, sub: str = "") -> str:
    sub_html = f'<p class="section-sub">{_e(sub)}</p>' if sub else ""
    return f'<div class="section-head"><div class="section-title">{_e(title)}</div>{sub_html}</div>'


def _banner(tone: str, title: str, sub: str) -> str:
    return (
        f'<div class="banner {tone}"><div class="b-title">{_e(title)}</div>'
        f'<div class="b-sub">{_e(sub)}</div></div>'
    )


def _card(label: str, value_html: str, sub: str, tone: str = "neutral", bar: int | None = None) -> str:
    value_cls = "metric-value" if tone == "neutral" else f"metric-value {tone}"
    bar_html = ""
    if bar is not None:
        width = max(0, min(100, int(bar)))
        bar_html = f'<div class="progress-track"><div class="progress-fill" style="width:{width}%"></div></div>'
    return (
        f'<div class="metric-card"><div class="metric-label">{_e(label)}</div>'
        f'<div class="{value_cls}">{value_html}</div>'
        f'<div class="metric-sub">{_e(sub)}</div>{bar_html}</div>'
    )


def _render_stages(stages: list[tuple[str, str, str]]) -> str:
    """Render actual workflow stages. Each stage is (name, state, note)."""
    parts = []
    for name, state, note in stages:
        parts.append(
            f'<div class="stage {state}"><span class="dot"></span>'
            f'<span class="s-name">{_e(name)}</span>'
            f'<span class="s-state">{_e(note)}</span></div>'
        )
    return f'<div class="stage-row">{"".join(parts)}</div>'


# --- Read-only data snapshot (real values, never fabricated) -------------------


def _snapshot(project_id: str) -> dict[str, Any]:
    """Read-only DB facts for the dashboard, derived with the backend's own rules."""
    today = rules.utc_today()
    try:
        with runtime.request_connection(DB_PATH) as conn:
            project = database.get_project(conn, project_id)
            tasks = database.list_tasks_by_project(conn, project_id)
            members = database.list_team_members(conn, project_id)
    except database.DatabaseError:
        project, tasks, members = None, [], []

    status_counts = {"todo": 0, "in_progress": 0, "blocked": 0, "completed": 0}
    for task in tasks:
        if task.get("status") in status_counts:
            status_counts[task["status"]] += 1
    overdue = rules.derive_overdue_task_ids(tasks, today)
    blocked_ids = [str(t["id"]) for t in tasks if t.get("id") and rules.is_blocked(t)]
    critical_ids = [str(t["id"]) for t in tasks if t.get("id") and t.get("priority") == "critical"]
    completion = [int(t.get("completion_percent") or 0) for t in tasks]
    avg_completion = round(sum(completion) / len(completion)) if completion else 0

    deadline = (project or {}).get("deadline")
    days_remaining: int | None = None
    if deadline:
        try:
            days_remaining = (date.fromisoformat(str(deadline)) - today).days
        except ValueError:
            days_remaining = None

    return {
        "project_id": project_id,
        "project_name": (project or {}).get("name", project_id),
        "tasks": tasks,
        "members": members,
        "total": len(tasks),
        "status_counts": status_counts,
        "overdue": overdue,
        "blocked_ids": blocked_ids,
        "critical_ids": critical_ids,
        "has_critical_blocked": rules.has_critical_blocked(tasks),
        "avg_completion": avg_completion,
        "deadline": deadline,
        "days_remaining": days_remaining,
    }


def tasks_rows(project_id: str) -> list[list[Any]]:
    """Read-only task rows for the demo table (real database values)."""
    snapshot = _snapshot(project_id)
    rows: list[list[Any]] = []
    for task in snapshot["tasks"]:
        rows.append([
            task.get("id", ""),
            task.get("title", ""),
            str(task.get("status", "")),
            str(task.get("priority", "")),
            task.get("owner_id") or "—",
            task.get("deadline") or "—",
            f'{int(task.get("completion_percent") or 0)}%',
            "yes" if rules.is_blocked(task) else "—",
        ])
    return rows


def _render_metrics(snapshot: dict[str, Any], result: Any = None) -> str:
    """Metric cards: health/progress from the analysis, all counts from the database."""
    status = getattr(result, "status", None) if result is not None else None

    if status is not None:
        label, tone = HEALTH_META.get(status.health.value, ("Unknown", "neutral"))
        health_value = _pill(label, tone)
        health_sub = "latest grounded analysis"
        progress_value = status.progress_percent
        progress_sub = "reported by analysis"
        progress_tone = "accent"
    else:
        health_value = _pill("Not analyzed", "neutral")
        health_sub = "run a health update"
        progress_value = snapshot["avg_completion"]
        progress_sub = "avg task completion"
        progress_tone = "neutral"

    overdue = snapshot["overdue"]
    overdue_tone = "crit" if len(overdue) >= rules.RED_OVERDUE_COUNT else ("warn" if overdue else "neutral")
    blocked_tone = "crit" if snapshot["has_critical_blocked"] else ("warn" if snapshot["blocked_ids"] else "neutral")
    critical_tone = "warn" if snapshot["critical_ids"] else "neutral"

    days = snapshot["days_remaining"]
    if days is None:
        deadline_tone, deadline_sub = "neutral", "no deadline stored"
    elif days <= rules.RED_DEADLINE_DAYS:
        deadline_tone, deadline_sub = "crit", f"{days} day(s) remaining"
    elif days <= rules.YELLOW_DEADLINE_DAYS:
        deadline_tone, deadline_sub = "warn", f"{days} day(s) remaining"
    else:
        deadline_tone, deadline_sub = "neutral", f"{days} day(s) remaining"

    cards = [
        _card("Project health", health_value, health_sub),
        _card("Progress", f"{progress_value}%", progress_sub, progress_tone, bar=progress_value),
        _card("Total tasks", str(snapshot["total"]), _e(snapshot["project_name"])),
        _card("Overdue", str(len(overdue)), "past deadline, not completed", overdue_tone),
        _card("Blocked", str(len(snapshot["blocked_ids"])), "blocked or flagged work", blocked_tone),
        _card("Critical priority", str(len(snapshot["critical_ids"])), "high-impact tasks", critical_tone),
        _card("Deadline", snapshot["deadline"] or "—", deadline_sub, deadline_tone),
    ]
    return (
        f'<div class="metric-grid">{"".join(cards)}</div>'
        f'<p class="hint" style="margin-top:10px;">Live task data read from the demo database '
        f'(read-only). Health and progress come from the latest analysis when one has run.</p>'
    )


def _health_banner(result: Any) -> str:
    """Honest health banner: analysis result, or an explicit not-analyzed state."""
    if result is None:
        return _banner("neutral", "Awaiting analysis",
                       "Select a project and run a health update to classify it.")
    if getattr(result, "blocked", False):
        return _banner("crit", "Input blocked by guardrail",
                       result.error or "No analysis ran and nothing changed.")
    if getattr(result, "status", None) is None:
        return _banner("warn", "Could not produce a trustworthy status",
                       result.error or result.warning or "unknown failure")
    status = result.status
    label, tone = HEALTH_META.get(status.health.value, ("Unknown", "neutral"))
    sub = f"Progress {status.progress_percent}% · route: {result.route}"
    if status.needs_human_approval:
        sub += " · review flagged"
    out = _banner(tone, f"Project health: {label}", sub)
    if status.needs_human_approval:
        out += _banner("warn", "Human review required",
                       "This assessment is flagged for a human to review. Health analysis is "
                       "read-only — no action was executed and nothing changed.")
    return out


def _health_activity(result: Any) -> str:
    """Render the stages that actually ran for WF-01 (never inferred)."""
    if getattr(result, "blocked", False):
        stages = [
            ("Request", "failed", "blocked"),
            ("Analysis", "pending", "not run"),
            ("Evaluation", "pending", "not run"),
            ("Human review", "pending", "n/a"),
            ("Result", "failed", "no output"),
        ]
    else:
        evidence = getattr(result, "evidence", None)
        if evidence is None:
            analysis_state, analysis_note = "failed", "no evidence"
        elif evidence.unavailable:
            analysis_state, analysis_note = "review", "partial evidence"
        else:
            analysis_state, analysis_note = "done", "complete"
        engine = {"parallel": "parallel workers", "orchestrated": "orchestrator-led"}.get(
            getattr(result, "evidence_path", "none"), "no engine")

        accepted = bool(getattr(result, "accepted", False))
        iterations = getattr(result, "iterations", 0)
        eval_state = "done" if accepted else "review"
        eval_note = f"{iterations}/2 revisions"

        needs_review = bool(getattr(result.status, "needs_human_approval", False)) if getattr(result, "status", None) else False
        review_state = "review" if needs_review else "pending"
        review_note = "required" if needs_review else "not required"

        if getattr(result, "error", None) or getattr(result, "status", None) is None:
            result_state, result_note = "failed", "no trustworthy status"
        elif getattr(result, "warning", None):
            result_state, result_note = "review", "provisional draft"
        else:
            result_state, result_note = "done", "status available"

        stages = [
            ("Request", "done", "accepted"),
            ("Analysis", analysis_state, engine),
            ("Evaluation", eval_state, eval_note),
            ("Human review", review_state, review_note),
            ("Result", result_state, result_note),
        ]
    return _section("Workflow activity", "Stages that actually executed for this run.") + _render_stages(stages)


# --- Existing renderers (kept verbatim; used by tests and the UI) -------------


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


def _run_health(project_id: str, user_id: str, message: str, session_id: str) -> Any:
    return asyncio.run(runtime.run_health_update(
        DB_PATH, session_id=session_id or runtime.new_session_id(),
        user_id=user_id, project_id=project_id, message=message))


def health_fn(project_id: str, user_id: str, message: str, session_id: str) -> str:
    """Run WF-01; transport failures surface as honest errors, never statuses."""
    try:
        result = _run_health(project_id, user_id, message, session_id)
    except Exception as exc:
        return f"⚠️ **Request failed safely:** {type(exc).__name__}. Nothing was changed."
    return format_health(result) + "\n" + format_run_trace(result)


def _action_values(action_type: str, title: str, owner_id: str, new_owner: str,
                   status: str, priority: str, deadline: str, content: str) -> dict[str, Any]:
    """Map raw form fields to the request values WF-02 expects (unchanged semantics).

    Hidden Gradio components arrive as ``None``, so every field is coerced to
    a trimmed string first; this changes no behavior for string inputs.
    """
    title = (title or "").strip()
    owner_id = (owner_id or "").strip()
    new_owner = (new_owner or "").strip()
    deadline = (deadline or "").strip()
    content = (content or "").strip()
    status = status or ""
    priority = priority or ""
    if action_type == "create_task":
        return {"title": title, "owner_id": owner_id or None}
    if action_type == "update_task":
        values: dict[str, Any] = {}
        if status:
            values["status"] = status
        if priority:
            values["priority"] = priority
        if deadline:
            values["deadline"] = deadline
        return values
    if action_type == "assign_task":
        return {"user_id": new_owner}
    return {"content": content}


def _request_action(project_id: str, user_id: str, session_id: str, action_type: str,
                    task_id: str, values: dict[str, Any]) -> Any:
    return asyncio.run(runtime.run_coordination(
        DB_PATH, STORE, session_id=session_id or runtime.new_session_id(),
        user_id=user_id, project_id=project_id, action_type=action_type,
        task_id=(task_id or "").strip() or None, values=values))


def _format_action_result(result: Any, project_id: str) -> str:
    """Structured, honest text for a WF-02 envelope (exact wording preserved)."""
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


def action_fn(project_id: str, user_id: str, session_id: str, action_type: str,
              task_id: str, title: str, owner_id: str, new_owner: str,
              status: str, priority: str, deadline: str, content: str) -> str:
    """Request a task action; review-required work returns pending, never executes."""
    values = _action_values(action_type, title, owner_id, new_owner, status, priority, deadline, content)
    try:
        result = _request_action(project_id, user_id, session_id, action_type, task_id, values)
    except Exception as exc:
        return f"⚠️ **Request failed safely:** {type(exc).__name__}. Nothing was changed."
    return _format_action_result(result, project_id)


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


# --- New UI handlers (polish layer; same backend calls) -----------------------


def health_run(project_id: str, user_id: str, message: str, session_id: str) -> tuple[str, str, str, str, str]:
    """Run WF-01 once and return (metrics, banner, analysis, activity, trace)."""
    try:
        result = _run_health(project_id, user_id, message, session_id)
    except Exception as exc:
        err = f"⚠️ **Request failed safely:** {type(exc).__name__}. Nothing was changed."
        snapshot = _snapshot(project_id)
        return (_render_metrics(snapshot), _banner("warn", "Request failed safely", str(exc)),
                err, "", "")
    snapshot = _snapshot(project_id)
    return (
        _render_metrics(snapshot, result),
        _health_banner(result),
        format_health(result),
        _health_activity(result),
        format_run_trace(result),
    )


def _action_banner(result: Any, project_id: str) -> str:
    if result is None:
        return _banner("neutral", "No action requested",
                       "Choose an action, fill the relevant fields, then request it.")
    if result.error is not None:
        return _banner("crit", "Request rejected safely", result.error)
    if result.pending is not None:
        pending = result.pending
        return _banner("warn", "Pending review — NOT executed",
                       f"{pending.action_type} on {pending.task_id or project_id}: {pending.reason}. "
                       "Decide in the Approvals tab; nothing has changed in the database.")
    action = result.action_result
    if action is not None and action.success:
        return _banner("ok", "Executed and verified", action.message)
    detail = action.message if action is not None else "unknown failure"
    return _banner("crit", "Not executed", detail)


def _action_activity(result: Any) -> str:
    if result is None:
        return ""
    pending = result.pending is not None
    if result.error is not None:
        exec_state, result_state = "failed", "failed"
    elif pending:
        exec_state, result_state = "pending", "pending"
    elif result.action_result is not None and result.action_result.success:
        exec_state, result_state = "done", "done"
    else:
        exec_state, result_state = "failed", "failed"
    review_state = "review" if pending else "pending"
    return _section("Workflow activity", "Stages that actually executed for this request.") + _render_stages([
        ("Request", "done", "accepted"),
        ("Policy check", "done", "deterministic"),
        ("Human review", review_state, "required" if pending else "not required"),
        ("Execution", exec_state, "confirmed" if exec_state == "done" else exec_state),
        ("Result", result_state, "verified" if result_state == "done" else result_state),
    ])


def action_run(project_id: str, user_id: str, session_id: str, action_type: str,
               task_id: str, title: str, owner_id: str, new_owner: str,
               status: str, priority: str, deadline: str, content: str) -> tuple[str, str, str, Any]:
    """Request one action once and return (banner, result text, activity, tasks table)."""
    values = _action_values(action_type, title, owner_id, new_owner, status, priority, deadline, content)
    try:
        result = _request_action(project_id, user_id, session_id, action_type, task_id, values)
    except Exception as exc:
        return (_banner("crit", "Request failed safely", f"{type(exc).__name__}. Nothing was changed."),
                f"⚠️ **Request failed safely:** {type(exc).__name__}. Nothing was changed.",
                "", gr.update(value=tasks_rows(project_id)))
    text = _format_action_result(result, project_id)
    return (_action_banner(result, project_id), text, _action_activity(result),
            gr.update(value=tasks_rows(project_id)))


def toggle_action_fields(action_type: str) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    """Show only the fields that apply to the selected action (no semantic change)."""
    show = {
        "create_task": ("title", "owner_id"),
        "update_task": ("task_id", "status", "priority", "deadline"),
        "assign_task": ("task_id", "new_owner"),
        "create_project_comment": ("content",),
    }
    fields = show.get(action_type, ())
    return (
        gr.update(visible="task_id" in fields),
        gr.update(visible="title" in fields),
        gr.update(visible="owner_id" in fields),
        gr.update(visible="new_owner" in fields),
        gr.update(visible="status" in fields),
        gr.update(visible="priority" in fields),
        gr.update(visible="deadline" in fields),
        gr.update(visible="content" in fields),
    )


def approval_panel(request_id: str) -> str:
    """Styled detail card for one approval request (real stored fields only)."""
    req = STORE.get(request_id) if request_id else None
    if req is None:
        return ('<div class="panel"><p class="hint">Select a pending request to review its '
                'evidence and decide. Nothing executes without your explicit approval.</p></div>')
    tone = {
        "pending_review": "warn", "approved": "accent",
        "rejected": "warn", "executed": "ok", "failed": "crit",
    }.get(req.status.value, "neutral")
    rows = [
        ("Request", req.id),
        ("Action", req.action_type),
        ("Target", req.task_id or req.project_id),
        ("Requester", req.requester),
        ("Status", _pill(req.status.value, tone)),
        ("Requested values", json.dumps(req.requested_values)),
        ("Reason", req.reason),
        ("Evidence", req.evidence or "—"),
    ]
    body = "".join(
        f'<div class="req-row"><span class="k">{_e(k)}</span><span class="v">{v if k == "Status" else _e(v)}</span></div>'
        for k, v in rows
    )
    stages = _approval_stages(req)
    return f'<div class="panel">{_section("Pending request", "Proposal only — not yet executed.")}{body}{stages}</div>'


def _approval_stages(req: Any) -> str:
    status = req.status.value
    decided = status != "pending_review"
    if status == "executed":
        exec_state, exec_note, result_state = "done", "confirmed", "done"
    elif status == "failed":
        exec_state, exec_note, result_state = "failed", "not executed", "failed"
    elif status == "rejected":
        exec_state, exec_note, result_state = "pending", "not executed", "pending"
    elif status == "approved":
        exec_state, exec_note, result_state = "active", "running", "pending"
    else:
        exec_state, exec_note, result_state = "pending", "awaiting approval", "pending"
    return _section("Approval flow", "States that actually occurred for this request.") + _render_stages([
        ("Request", "done", "held"),
        ("Policy review", "done", "deterministic"),
        ("Human decision", "done" if decided else "active", status),
        ("Execution", exec_state, exec_note),
        ("Result", result_state, "verified" if result_state == "done" else result_state),
    ])


def select_approval(request_id: str) -> tuple[Any, Any, Any]:
    """Show the selected pending request; ignore programmatic clears (None).

    After a decision the dropdown is updated with ``value=None``; that must not
    wipe the executed/rejected panel and its confirmation banner.
    """
    if not request_id or STORE.get(request_id) is None:
        return gr.update(), gr.update(), gr.update()
    return approval_panel(request_id), "", ""


def decide_run(request_id: str, approver: str, decision: str) -> tuple[str, str, str, Any]:
    """Approve/reject through the tested decision path; refresh the panel and list."""
    if not request_id:
        return (approval_panel(""),
                _banner("warn", "No request selected", "Choose a pending request first."),
                "Select a pending request first.",
                gr.update())
    text = decide_fn(request_id, approver, decision)
    req = STORE.get(request_id)
    status = req.status.value if req is not None else "unknown"
    tone = {"executed": "ok", "failed": "crit", "rejected": "warn"}.get(status, "warn")
    title = {
        "executed": "Executed after approval",
        "failed": "Approval failed — nothing changed",
        "rejected": "Rejected — no mutation",
    }.get(status, "Decision recorded")
    remaining = approvals_fn()
    # Clear the selection without clobbering the panel/banner: the change
    # handler treats a None selection as a no-op.
    return (approval_panel(request_id), _banner(tone, title, text.split("\n")[0]),
            text, gr.update(choices=remaining, value=None))


def refresh_approvals() -> tuple[Any, str, str, str]:
    """Reload the pending list, auto-selecting the first pending request."""
    ids = approvals_fn()
    first = ids[0] if ids else None
    return gr.update(choices=ids, value=first), approval_panel(first or ""), "", ""


def health_project_change(project_id: str) -> tuple[str, str, Any]:
    """On project change: refresh metrics + banner, and reset the task table."""
    return _render_metrics(_snapshot(project_id)), _health_banner(None), gr.update(value=tasks_rows(project_id))


def action_project_change(project_id: str) -> tuple[Any, Any]:
    """On project change: refresh member choices and the task table."""
    members = _member_choices(project_id)
    return (gr.update(choices=members, value=members[0] if members else None),
            gr.update(value=tasks_rows(project_id)))


# --- UI construction ----------------------------------------------------------


def _header() -> str:
    return (
        '<div class="app-header">'
        '<div class="eyebrow">AI Project Operations Agent</div>'
        '<h1>Project Operations Console</h1>'
        '<p class="sub">Grounded project health analysis with deterministic guardrails and human approval. '
        'Pending actions are <strong>not executed</strong> until a human approves and the tool confirms.</p>'
        '</div>'
    )


def _health_tab(projects: list[str]) -> None:
    default_project = projects[0]
    with gr.Row():
        with gr.Column(scale=4, min_width=320):
            with gr.Group(elem_classes="panel"):
                gr.HTML(_section("New analysis", "Turn a short project update into a grounded status."))
                project = gr.Dropdown(projects, value=default_project, label="Project")
                user = gr.Dropdown(_member_choices(default_project), value="U001", label="User")
                message = gr.Textbox(
                    label="Project update", lines=4,
                    placeholder="e.g. Weekly check: gateway fix is waiting on the vendor.")
                with gr.Accordion("Session continuity", open=False):
                    session = gr.Textbox(value=runtime.new_session_id(), label="Session id")
                run_btn = gr.Button("Analyze project health", variant="primary", size="lg")
        with gr.Column(scale=8):
            metrics = gr.HTML(_render_metrics(_snapshot(default_project)))
            banner = gr.HTML(_health_banner(None))

    activity = gr.HTML()
    with gr.Group(elem_classes="panel"):
        gr.HTML(_section("AI project analysis",
                         "Grounded draft produced by the Status Writer and checked by the Evaluator."))
        out = gr.Markdown("_Run an analysis to see a grounded project status here._")
    with gr.Accordion("Run trace (observability)", open=False):
        trace = gr.Markdown()

    run_btn.click(health_run, [project, user, message, session],
                  [metrics, banner, out, activity, trace])
    project.change(health_project_change, project, [metrics, banner, activity])


def _actions_tab(projects: list[str]) -> None:
    default_project = projects[0]
    with gr.Row():
        with gr.Column(scale=5, min_width=320):
            with gr.Group(elem_classes="panel"):
                gr.HTML(_section("Request a task action",
                                 "High-impact requests pause for human approval instead of executing."))
                a_project = gr.Dropdown(projects, value=default_project, label="Project")
                a_user = gr.Dropdown(_member_choices(default_project), value="U001", label="Requester")
                a_type = gr.Dropdown(
                    ["create_project_comment", "create_task", "update_task", "assign_task"],
                    value="create_project_comment", label="Action")
                a_task = gr.Textbox(label="Task id", visible=False,
                                    placeholder="e.g. T002 (for update/assign)")
                a_title = gr.Textbox(label="Title", visible=False, placeholder="New task title")
                a_owner = gr.Textbox(label="Owner id (optional)", visible=False, placeholder="e.g. U003")
                a_new_owner = gr.Textbox(label="New owner id", visible=False, placeholder="e.g. U003")
                a_status = gr.Dropdown(["", "todo", "in_progress", "blocked", "completed"],
                                       value="", label="Status", visible=False)
                a_priority = gr.Dropdown(["", "low", "medium", "high", "critical"],
                                         value="", label="Priority", visible=False)
                a_deadline = gr.Textbox(label="Deadline YYYY-MM-DD", visible=False)
                a_content = gr.Textbox(label="Comment", lines=2)
                with gr.Accordion("Session continuity", open=False):
                    a_session = gr.Textbox(value=runtime.new_session_id(), label="Session id")
                a_btn = gr.Button("Request action", variant="primary", size="lg")
        with gr.Column(scale=7):
            a_banner = gr.HTML(_banner("neutral", "No action requested",
                                       "Choose an action, fill the relevant fields, then request it."))
            a_out = gr.Markdown()
            a_activity = gr.HTML()

    with gr.Group(elem_classes="panel"):
        gr.HTML(_section("Project tasks",
                         "Live read-only view of the selected project's tasks."))
        a_tasks = gr.Dataframe(headers=TASK_HEADERS, value=tasks_rows(default_project),
                               interactive=False, wrap=True, max_height=420)

    a_type.change(toggle_action_fields, a_type,
                  [a_task, a_title, a_owner, a_new_owner, a_status, a_priority, a_deadline, a_content])
    a_btn.click(action_run,
                [a_project, a_user, a_session, a_type, a_task, a_title, a_owner,
                 a_new_owner, a_status, a_priority, a_deadline, a_content],
                [a_banner, a_out, a_activity, a_tasks])
    a_project.change(action_project_change, a_project, [a_user, a_tasks])


def _approvals_tab() -> None:
    pending_ids = approvals_fn()
    first = pending_ids[0] if pending_ids else None
    with gr.Row():
        with gr.Column(scale=5, min_width=320):
            with gr.Group(elem_classes="panel"):
                gr.HTML(_section("Human review",
                                 "Approve executes after live revalidation; reject mutates nothing."))
                pending = gr.Dropdown(pending_ids, value=first, label="Pending requests")
                refresh = gr.Button("Refresh list", variant="secondary")
                approver = gr.Textbox(value="U001", label="Approver")
                with gr.Row():
                    approve_btn = gr.Button("Approve → revalidate → execute", variant="primary")
                    reject_btn = gr.Button("Reject (zero mutation)", variant="cancel")
        with gr.Column(scale=7):
            detail = gr.HTML(approval_panel(first or ""))
            d_banner = gr.HTML()
            d_out = gr.Markdown()

    refresh.click(refresh_approvals, None, [pending, detail, d_banner, d_out])
    pending.change(select_approval, pending, [detail, d_banner, d_out])
    approve_btn.click(lambda r, a: decide_run(r, a, "approve"),
                      [pending, approver], [detail, d_banner, d_out, pending])
    reject_btn.click(lambda r, a: decide_run(r, a, "reject"),
                     [pending, approver], [detail, d_banner, d_out, pending])


def build_ui() -> gr.Blocks:
    """Construct the demo interface (no side effects until launched)."""
    ensure_db()
    projects = _project_choices()
    with gr.Blocks(title="AI Project Operations Agent") as demo:
        gr.HTML(_header())
        with gr.Tabs():
            with gr.Tab("Health"):
                _health_tab(projects)
            with gr.Tab("Task Actions"):
                _actions_tab(projects)
            with gr.Tab("Approvals"):
                _approvals_tab()
    return demo


if __name__ == "__main__":
    ensure_db()
    build_ui().launch(theme=build_theme(), css=CUSTOM_CSS)
