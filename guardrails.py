"""Guardrails for the AI Project Operations Agent (Project 4).

Phase 5 safety layer (PDD Section 9, SDD Section 6.1):

- ``screen_input`` runs before Intake: deterministic intent-pattern checks
  (grouped detectors, not a bare keyword list) yielding allow / block / review.
- ``check_status_output`` runs before a ProjectStatus is presented as final.
- ``check_action_output`` validates an ActionResult against tool confirmation.
- ``is_action_permitted`` is the demo-safe role policy (assumption: the PDD
  names roles but defines no matrix; see docstring).

No model calls, no secrets, no chain-of-thought anywhere in this module.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from models import StrictModel

ID_RE = r"[TU]\d{3}"
DATE_RE = r"\d{4}-\d{2}-\d{2}"


class Verdict(str, Enum):
    """Guardrail outcomes."""

    ALLOW = "allow"
    BLOCK = "block"
    REVIEW = "review"


class ScreeningResult(StrictModel):
    """Small structured guardrail result (no internals, no reasoning traces)."""

    verdict: Verdict
    reasons: list[str] = []
    guidance: str = ""

    @property
    def passed(self) -> bool:
        """True only when the content may proceed normally."""
        return self.verdict == Verdict.ALLOW

    @property
    def blocked(self) -> bool:
        """True when the content must not enter the normal workflow."""
        return self.verdict == Verdict.BLOCK

    @property
    def needs_review(self) -> bool:
        """True when the content is ambiguous and needs human review."""
        return self.verdict == Verdict.REVIEW


# --- Input guardrail: intent detectors (deterministic, grouped by intent) ---

_HARD_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("override safety rules", (
        r"ignor(e|ing)\s+(previous|all|the\s+)?(instructions|rules|policy|safety|guardrails)",
        r"disregard\s+(safety|policy|rules|instructions)",
        r"override\s+(the\s+|this\s+)?(safety|policy|approval|rules)",
    )),
    ("self-granted authorization", (
        r"you\s+are\s+(now\s+)?authori[sz]ed",
        r"i\s+am\s+(an?\s+)?(admin|manager|approver)",
        r"approve\s+(this|it)\s+yourself",
        r"act\s+as\s+(an?\s+)?(admin|root|system)",
    )),
    ("system-prompt extraction", (
        r"system\s+prompt",
        r"show\s+me\s+your\s+(instructions|prompt|reasoning)",
        r"reveal\s+your\s+(prompt|instructions)",
        r"chain-of-thought",
        r"show\s+your\s+(reasoning|workings)",
    )),
    ("direct database manipulation", (
        r"\bsql\b",
        r"drop\s+table",
        r"delet(e|ing)\s+the\s+database",
        r"(execute|run)\s+.*(database\s+update|sql|query)",
        r"direct(ly)?\s+(access|update|writ(e|ing)).{0,20}database",
        r"update\s+the\s+database\s+directly",
    )),
    ("destructive operation", (
        r"delete\s+all",
        r"drop\s+(the\s+|all\s+)?(project|task|database)",
        r"\bdestroy\b",
        r"\bwipe\b",
    )),
    ("fabricated confirmation", (
        r"claim\s+.*(complet|done|success|assign)",
        r"pretend\s+.*(complet|done|finish)",
        r"\bsay\b\s+.*\b(is|was)\s+(done|complete|finished|assigned)\b",
        r"fabricat",
    )),
    ("forced completion without evidence", (
        r"mark\s+.*\bcomplet\w*\s+even\s+though",
        r"even\s+though\s+.*\bnot\s+(finished|done|complete)\b",
        r"without\s+evidence",
    )),
    ("approval bypass", (
        r"bypass\s+.*approval",
        r"skip\s+.*approval",
        r"without\s+approval",
        r"no\s+need\s+for\s+approval",
        r"ignor\w*\s+.*approval",
    )),
)

_REVIEW_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("time-pressured mutation request", (
        r"\b(asap|right\s+now|immediately|urgent)\b.{0,40}\b((re)?assign|update|complete|delete|change|approve)\b",
        r"\b((re)?assign|update|complete|delete|change|approve)\b.{0,40}\b(asap|right\s+now|immediately|urgent)\b",
    )),
)


def _match_any(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


def screen_input(message: Any) -> ScreeningResult:
    """Screen raw user input before Intake. Never raises on bad input types."""
    if not isinstance(message, str) or not message.strip():
        return ScreeningResult(
            verdict=Verdict.BLOCK,
            reasons=["empty or non-text input"],
            guidance="Provide a written project update or request.",
        )
    text = re.sub(r"\s+", " ", message.strip())
    for intent, patterns in _HARD_PATTERNS:
        if _match_any(text, patterns) is not None:
            return ScreeningResult(
                verdict=Verdict.BLOCK,
                reasons=[f"unsafe intent: {intent}"],
                guidance="Rephrase as a normal project update without override, bypass, or extraction instructions.",
            )
    for intent, patterns in _REVIEW_PATTERNS:
        if _match_any(text, patterns) is not None:
            return ScreeningResult(
                verdict=Verdict.REVIEW,
                reasons=[f"ambiguous intent: {intent}"],
                guidance="The request will proceed through human review before any high-impact action.",
            )
    return ScreeningResult(verdict=Verdict.ALLOW)


# --- Output guardrail ---

_COMMITMENT_RE = re.compile(r"\b(guarantee|promise|definitely|assure)\b", re.IGNORECASE)
_MUTATION_RE = re.compile(r"\b(assigned|reassigned|updated|created|deleted|executed)\b", re.IGNORECASE)
_AUTH_RE = re.compile(r"\b(approved|authorized|approval\s+granted)\b", re.IGNORECASE)
_COMPLETION_RE = re.compile(r"\bcomplet\w*|finished|\bis\s+done\b|\bwas\s+done\b", re.IGNORECASE)


def _id_tokens(texts: list[str]) -> tuple[set[str], set[str]]:
    tasks, members = set(), set()
    for text in texts:
        for token in re.findall(rf"\b{ID_RE}\b", text):
            (tasks if token.startswith("T") else members).add(token)
    return tasks, members


def check_status_output(
    status: Any,
    *,
    known_task_ids: set[str],
    known_member_ids: set[str] = frozenset(),  # type: ignore[assignment]
    deadline: str | None = None,
    completed_task_ids: set[str] = frozenset(),  # type: ignore[assignment]
    confirmed_mutations: tuple[str, ...] = (),
    approval_state: str | None = None,
    reference_date: str | None = None,
) -> ScreeningResult:
    """Validate a ProjectStatus before it is presented as trustworthy.

    ``reference_date`` is the system-provided as-of day (ISO): deterministic
    risk entries cite it, so it is exempt from the foreign-date rule. Any
    other date unlike the project deadline still fails.
    """
    problems: list[str] = []
    texts = [*status.blockers, *status.risks, *status.recommended_actions]

    unknown_evidence = [t for t in status.evidence_task_ids if t not in known_task_ids]
    if unknown_evidence:
        problems.append(f"evidence references unknown tasks: {sorted(unknown_evidence)}")
    unknown_overdue = [t for t in status.overdue_tasks if t not in known_task_ids]
    if unknown_overdue:
        problems.append(f"overdue list references unknown tasks: {sorted(unknown_overdue)}")

    text_tasks, text_members = _id_tokens(texts)
    unknown_text_tasks = sorted(text_tasks - known_task_ids)
    if unknown_text_tasks:
        problems.append(f"text references unknown tasks: {unknown_text_tasks}")
    unknown_members = sorted(text_members - set(known_member_ids))
    if unknown_members:
        problems.append(f"text references unknown members: {unknown_members}")

    for text in texts:
        for match in _COMPLETION_RE.finditer(text):
            window = text[max(0, match.start() - 40): match.end() + 40]
            refs = set(re.findall(rf"\bT\d{{3}}\b", window))
            unproven = sorted(refs - set(completed_task_ids))
            if refs and unproven:
                problems.append(f"unsupported completion claim for {unproven}")
                break

    if status.progress_percent == 100 and (status.blockers or status.overdue_tasks):
        problems.append("contradiction: 100% progress with open blockers/overdue work")

    if deadline is not None:
        for day in set(re.findall(rf"\b{DATE_RE}\b", " ".join(texts))):
            if day != deadline and day != reference_date:
                problems.append(f"unsupported deadline promise: {day}")
                break

    if _MUTATION_RE.search(" ".join(texts)) and not confirmed_mutations:
        problems.append("claims a mutation without a confirmed tool result")
    if _COMMITMENT_RE.search(" ".join(texts)):
        problems.append("unsafe commitment language (guarantee/promise)")
    if _AUTH_RE.search(" ".join(texts)) and approval_state != "approved":
        problems.append("claims authorization without an approval record")
    if (status.blockers or status.overdue_tasks) and not status.evidence_task_ids:
        problems.append("task claims without required evidence references")

    if problems:
        return ScreeningResult(
            verdict=Verdict.BLOCK,
            reasons=problems,
            guidance="Remove or evidence every flagged claim; do not present guesses as facts.",
        )
    return ScreeningResult(verdict=Verdict.ALLOW)


def check_action_output(action_result: Any, *, tool_confirmed: bool) -> ScreeningResult:
    """Validate an ActionResult: LLM output alone never establishes success."""
    if action_result.success and not tool_confirmed:
        return ScreeningResult(
            verdict=Verdict.BLOCK,
            reasons=["unconfirmed mutation: success claimed without a tool confirmation"],
            guidance="Report the action as not executed until a domain tool confirms it.",
        )
    return ScreeningResult(verdict=Verdict.ALLOW)


# --- Demo-safe role policy (assumption; PDD names roles but defines no matrix) ---


def is_action_permitted(*, member_role: str | None, is_owner: bool, action_type: str) -> bool:
    """Return True when the requester's role may attempt the action.

    - "Project Manager" (case-insensitive): all actions.
    - Task owner: comment, create, update, and assign-requests on owned work
      (approval policy still applies on top).
    - Other members / unknown users: project comments only.
    Membership/role always comes from stored records or the caller — never
    from user-message claims such as "I am an admin".
    """
    if member_role is not None and member_role.strip().lower() == "project manager":
        return True
    if action_type == "create_project_comment":
        return member_role is not None
    if action_type == "create_task":
        return member_role is not None
    return is_owner
