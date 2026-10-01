"""
Tool Registry for Evie's Tool-Calling Router.

Implements 02_TRD.md Section 2 and 04_Backend_Schema.md Section 4:
- Every action the assistant can take is a typed ToolSpec (name, description,
  Pydantic parameter model, handler, is_sensitive, requires_confirmation).
- The SQLite tool_registry table is the allow-boundary: a tool is executable only
  when it is defined here in code AND has a row in tool_registry.
- The LLM never calls handlers. The executor in services/tool_router.py looks a
  tool up here, validates its arguments, applies the gates, and only then runs it.

Paused or undefined capabilities (DRS, tone changes, open_app, enable_2fa,
revoke_secret_finding) are intentionally not registered.
"""

import datetime
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.conversational_router import execute_grounded_query


@dataclass
class ToolContext:
    """Per-request context handed to a handler by the executor (never by the LLM)."""
    db_path: str
    channel: str
    tone: str
    conversation: Dict[str, Any]


class ToolParams(BaseModel):
    """Base for all tool parameter models: unknown arguments are rejected."""
    model_config = ConfigDict(extra="forbid")


Handler = Callable[[ToolParams, ToolContext], Dict[str, Any]]
UIBuilder = Callable[[Dict[str, Any], ToolContext], Optional[Dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params_model: type
    handler: Handler
    is_sensitive: bool = False
    requires_confirmation: bool = False
    # True when the handler itself only creates a pending proposal through an existing
    # Propose -> Confirm mechanism (calendar, Gmail cleanup); the executor may run it
    # directly. Otherwise requires_confirmation stores a generic proposal instead.
    handler_creates_proposal: bool = False
    ui_builder: Optional[UIBuilder] = None
    confirmation_prompt: Optional[Callable[[ToolParams], str]] = None

    def parameters_schema(self) -> Dict[str, Any]:
        return self.params_model.model_json_schema()


# --- Parameter models ---

RepoName = Optional[str]
REPO_FIELD = Field(None, max_length=200, pattern=r"^[A-Za-z0-9_.\-/]+$", description="Repository name or owner/repo, if the user named one")
TimeRange = Literal["recent", "today", "yesterday", "this_week", "last_month", "this_year"]


class NoParams(ToolParams):
    pass


class RepoParams(ToolParams):
    repo_name: RepoName = REPO_FIELD


class ActivityParams(ToolParams):
    repo_name: RepoName = REPO_FIELD
    time_range: TimeRange = Field("recent", description="Time window the user asked about")


class TimeRangeParams(ToolParams):
    time_range: TimeRange = Field("recent", description="Time window the user asked about")


class FindingsParams(ToolParams):
    category: Literal["all", "secrets", "breaches", "exposures", "twofa", "dependencies"] = "all"


class RemediationParams(ToolParams):
    finding_type: Literal["secret", "breach", "exposure", "twofa", "dependency"]
    finding_id: int = Field(..., ge=1)


class MemorySearchParams(ToolParams):
    query: str = Field(..., min_length=1, max_length=500)
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    top_k: int = Field(5, ge=1, le=10)


class FollowupParams(ToolParams):
    request: str = Field("show details", max_length=200, description="The user's follow-up phrasing")


class ResolveFindingParams(ToolParams):
    finding_type: Literal["secret", "exposure", "dependency", "breach"]
    id: int = Field(..., ge=1, description="ID of the finding within its finding_type")
    status: Literal["resolved", "false_positive"] = "resolved"


class CalendarProposalParams(ToolParams):
    summary: str = Field("Proposed Event", min_length=1, max_length=200)
    start_time: Optional[str] = Field(None, max_length=40, description="ISO 8601 start time")
    end_time: Optional[str] = Field(None, max_length=40, description="ISO 8601 end time")
    description: str = Field("Created via Evie chat", max_length=500)


class OpenAppParams(ToolParams):
    app_name: str = Field(
        ..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9 ]+$",
        description="Application to open. Allowed: Safari, Google Chrome, Firefox, Finder, Terminal, Visual Studio Code",
    )


class GmailCleanupParams(ToolParams):
    sender_domain: Optional[str] = Field(None, max_length=253, pattern=r"^[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


# --- Handlers ---

def _router_intent(intent: str) -> Handler:
    """Wrap an existing grounded-query branch so its reply behavior is unchanged."""
    def handler(params: ToolParams, ctx: ToolContext) -> Dict[str, Any]:
        entities = params.model_dump(exclude_none=True)
        return execute_grounded_query(
            {"intent": intent, "entities": entities},
            ctx.db_path,
            active_tone=ctx.tone,
            last_chat_context=ctx.conversation,
        )
    return handler


PROMO_TOPICS = ("promotional_emails", "gmail_summary")


def _show_more_details(params: FollowupParams, ctx: ToolContext) -> Dict[str, Any]:
    request = params.request
    # A read-only follow-up must never create cleanup proposals; that path is propose_gmail_cleanup.
    if ctx.conversation.get("topic") in PROMO_TOPICS and ("tell" in request.lower() or "unsubscribe" in request.lower()):
        request = "show details"
    return execute_grounded_query(
        {"intent": "contextual_followup", "entities": {"raw_message": request}},
        ctx.db_path,
        active_tone=ctx.tone,
        last_chat_context=ctx.conversation,
    )


def _finding_cards(findings: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    cards = []
    for f in findings.get("secrets", []):
        cards.append({"finding_type": "secret", "id": f.get("id"), "title": f"{f.get('pattern_type') or 'Candidate secret'} in {f.get('repo_name')}",
                      "detail": f"{f.get('file_path')} · {f.get('redacted_preview') or '[REDACTED]'}", "severity": "critical"})
    for f in findings.get("breaches", []):
        cards.append({"finding_type": "breach", "id": f.get("id"), "title": f"Breach: {f.get('breach_name')}",
                      "detail": f"Breach date {f.get('breach_date') or 'unknown'}", "severity": "high"})
    for f in findings.get("exposures", []):
        cards.append({"finding_type": "exposure", "id": f.get("id"), "title": f"{(f.get('source') or '').title()} exposure: {f.get('item_name') or f.get('item_id')}",
                      "detail": f.get("exposure_type") or "", "severity": "high"})
    for f in findings.get("dependencies", []):
        cards.append({"finding_type": "dependency", "id": f.get("id"), "title": f"Vulnerable dependency {f.get('package_name')} in {f.get('repo_name')}",
                      "detail": f"Severity {f.get('severity')}", "severity": (f.get("severity") or "").lower()})
    return cards


def _list_open_findings(params: FindingsParams, ctx: ToolContext) -> Dict[str, Any]:
    from services.finding_actions import list_open_findings
    findings = list_open_findings(ctx.db_path)
    if params.category != "all":
        findings = {params.category: findings.get(params.category, [])}
    if params.category == "all":
        findings.pop("twofa", None)  # 2FA gaps are audit state, not resolvable findings
    cards = _finding_cards(findings)
    counts = ", ".join(f"{len(v)} {k}" for k, v in findings.items())
    if cards:
        lines = [f"• [{c['finding_type']} #{c['id']}] {c['title']}" for c in cards[:10]]
        reply = f"Open findings ({counts}):\n" + "\n".join(lines)
    else:
        reply = f"No open findings ({counts})."
    return {"reply": reply, "findings": findings, "finding_cards": cards, "tone": ctx.tone}


def _twofa_status(params: NoParams, ctx: ToolContext) -> Dict[str, Any]:
    from services.finding_actions import get_twofa_status
    status_rows = get_twofa_status(ctx.db_path)
    if status_rows:
        lines = [f"• {r['provider']}: {'enabled' if r['twofa_enabled'] else 'NOT enabled'} (checked {str(r['checked_at'])[:10]})" for r in status_rows]
        reply = "Two-factor authentication status:\n" + "\n".join(lines)
    else:
        reply = "No 2FA audit results recorded yet."
    return {"reply": reply, "twofa_status": status_rows, "tone": ctx.tone}


def _remediation_guide(params: RemediationParams, ctx: ToolContext) -> Dict[str, Any]:
    import remediation
    try:
        guide = remediation.get_remediation_guide(params.finding_type, params.finding_id, ctx.db_path)
    except ValueError:
        return {"reply": f"No {params.finding_type} finding #{params.finding_id} was found.", "tone": ctx.tone}
    reply = f"{guide['title']} (severity: {guide['severity']}):\n" + "\n".join(guide["steps"])
    return {"reply": reply, "remediation": guide, "tone": ctx.tone}


def _dashboard_state(params: NoParams, ctx: ToolContext) -> Dict[str, Any]:
    from services.dashboard_state import get_dashboard_state
    state = get_dashboard_state(ctx.db_path)
    score = state["security_score"]
    findings = state["security_findings_summary"]
    reply = (
        f"Overall status: {state['overall_status']}. Security score {score['score']}/100 (Grade: {score['grade']}). "
        f"{findings['total_open_findings']} open finding(s)."
    )
    return {"reply": reply, "dashboard": state, "tone": ctx.tone}


def _search_memory(params: MemorySearchParams, ctx: ToolContext) -> Dict[str, Any]:
    import memory
    try:
        results = memory.search_journal_entries(
            query=params.query, top_k=params.top_k, start_date=params.start_date, end_date=params.end_date, db_path=ctx.db_path
        )
    except ValueError as e:
        return {"reply": f"Memory search could not run: {e}", "memory_results": [], "tone": ctx.tone}
    if results:
        lines = [f"• {r['entry_date']}: {r['content'][:200]}" for r in results]
        reply = f"Journal entries matching '{params.query}':\n" + "\n".join(lines)
    else:
        reply = "No matching journal memory entries found."
    return {"reply": reply, "memory_results": results, "tone": ctx.tone}


def _resolve_finding(params: ResolveFindingParams, ctx: ToolContext) -> Dict[str, Any]:
    from services.finding_actions import resolve_finding
    score = resolve_finding(ctx.db_path, params.finding_type, params.id, params.status)
    if score is None:
        return {"reply": f"No {params.finding_type} finding #{params.id} was found; nothing was changed.", "tone": ctx.tone}
    label = "a false positive" if params.status == "false_positive" else "resolved"
    reply = f"Marked {params.finding_type} finding #{params.id} as {label}. Security score is now {score['score']}/100 (Grade: {score['grade']})."
    return {"reply": reply, "score": score, "tone": ctx.tone}


def _propose_calendar_event(params: CalendarProposalParams, ctx: ToolContext) -> Dict[str, Any]:
    from integrations.calendar_actions import propose_calendar_action
    now = datetime.datetime.now()
    start = params.start_time or (now + datetime.timedelta(days=1)).isoformat()
    end = params.end_time or (datetime.datetime.fromisoformat(start) + datetime.timedelta(hours=1)).isoformat()
    try:
        prop = propose_calendar_action(
            db_path=ctx.db_path, action_type="create", summary=params.summary,
            start_time=start, end_time=end, description=params.description,
        )
    except ValueError as e:
        return {"reply": f"I couldn't prepare that calendar proposal: {e}", "tone": ctx.tone}
    return {"reply": prop["prompt"], "proposal": prop, "tone": ctx.tone}


def _propose_gmail_cleanup(params: GmailCleanupParams, ctx: ToolContext) -> Dict[str, Any]:
    from services.gmail_intelligence import propose_gmail_cleanup_action
    if params.sender_domain:
        prop = propose_gmail_cleanup_action(ctx.db_path, params.sender_domain, action_type="unsubscribe_recommendation")
        reply = f"Created an unsubscribe proposal for {prop['sender_domain']}.\n\nPlease confirm execution via POST /gmail/confirm_cleanup."
        return {"reply": reply, "proposals": [prop], "tone": ctx.tone}
    if ctx.conversation.get("topic") not in PROMO_TOPICS:
        return {"reply": "Tell me which sender domain to unsubscribe from, or ask about your promotional emails first.", "proposals": [], "tone": ctx.tone}
    # Existing follow-up branch: proposes unsubscribe for every sender domain in context.
    return execute_grounded_query(
        {"intent": "contextual_followup", "entities": {"raw_message": "unsubscribe"}},
        ctx.db_path,
        active_tone=ctx.tone,
        last_chat_context=ctx.conversation,
    )


OPEN_APP_REPLIES = {
    "APP_NOT_ALLOWED": "I can only open these apps: {allowed}.",
    "APP_NOT_INSTALLED": "{app} doesn't appear to be installed on this computer.",
    "UNSUPPORTED_OS": "Opening apps is only supported on macOS right now.",
    "LAUNCH_FAILED": "I couldn't open {app}.",
}


def _open_app(params: OpenAppParams, ctx: ToolContext) -> Dict[str, Any]:
    from services import os_adapter
    app = os_adapter.resolve_app(params.app_name)
    display = app.canonical if app else params.app_name
    try:
        opened = os_adapter.open_app(params.app_name)
    except os_adapter.OpenAppError as e:
        code = str(e)
        reply = OPEN_APP_REPLIES.get(code, OPEN_APP_REPLIES["LAUNCH_FAILED"]).format(
            allowed=", ".join(os_adapter.ALLOWED_APP_NAMES), app=display)
        return {"reply": reply, "app": {"app_name": display if app else None, "opened": False, "reason": code}, "tone": ctx.tone}
    return {"reply": f"Opening {opened}.", "app": {"app_name": opened, "opened": True}, "tone": ctx.tone}


def _ui_open_app(result: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
    app = result.get("app") or {}
    if app.get("opened") is False:
        return {"type": "notice", "data": {"reason": app.get("reason")}}
    return None


# --- Structured UI builders (03_UIUX_Design.md Section 3) ---

def _ui(ui_type: str, key: Optional[str] = None) -> UIBuilder:
    def build(result: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
        if key is None:
            return {"type": ui_type, "data": {}}
        if key not in result:
            return None
        return {"type": ui_type, "data": result[key]}
    return build


def _ui_from_context(ui_type: str) -> UIBuilder:
    """For router branches that return rows only via conversation context."""
    def build(result: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
        data = ctx.conversation.get("data")
        return {"type": ui_type, "data": data if isinstance(data, list) else []}
    return build


def _ui_secret_cards(result: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
    if "secret_findings" not in result:
        return None
    return {"type": "finding_cards", "data": _finding_cards({"secrets": result["secret_findings"] or []})}


def _ui_activity(result: Dict[str, Any], ctx: ToolContext) -> Optional[Dict[str, Any]]:
    if result.get("recent_activity"):
        return {"type": "timeline", "data": result["recent_activity"]}
    if "analytics" in result:
        return {"type": "summary", "data": {k: v for k, v in result["analytics"].items() if not isinstance(v, (list, dict))}}
    return None


def _resolve_prompt(params: ResolveFindingParams) -> str:
    label = "false positive" if params.status == "false_positive" else "resolved"
    return f"Mark {params.finding_type} finding #{params.id} as {label}?"


TOOL_SPECS: Dict[str, ToolSpec] = {}


def _register(spec: ToolSpec) -> None:
    TOOL_SPECS[spec.name] = spec


# Read-only tools
_register(ToolSpec("get_security_score", "Get the user's current canonical security health score, grade, and open-finding counts.", NoParams, _router_intent("security_score"), ui_builder=_ui("score", "score")))
_register(ToolSpec("list_open_findings", "List open security findings (leaked secrets, breaches, public exposures, 2FA gaps, vulnerable dependencies), each with its finding_type and id.", FindingsParams, _list_open_findings, ui_builder=_ui("finding_cards", "finding_cards")))
_register(ToolSpec("get_exposure_findings", "List open Google Drive / Calendar public or external-domain exposure findings.", NoParams, lambda p, c: _list_open_findings(FindingsParams(category="exposures"), c), ui_builder=_ui("finding_cards", "finding_cards")))
_register(ToolSpec("get_breach_status", "List unacknowledged Have I Been Pwned breach findings for the user's email.", NoParams, lambda p, c: _list_open_findings(FindingsParams(category="breaches"), c), ui_builder=_ui("finding_cards", "finding_cards")))
_register(ToolSpec("get_twofa_status", "Get the latest two-factor authentication audit result for each provider (Google, GitHub).", NoParams, _twofa_status, ui_builder=_ui("table", "twofa_status")))
_register(ToolSpec("get_remediation_guide", "Get step-by-step remediation guidance for a specific finding.", RemediationParams, _remediation_guide, ui_builder=_ui("steps", "remediation")))
_register(ToolSpec("get_dashboard_state", "Get the overall Good / Needs Attention / Critical status and section summaries.", NoParams, _dashboard_state, ui_builder=_ui("dashboard", "dashboard")))
_register(ToolSpec("search_memory", "Search the user's journal entries by topic and optional YYYY-MM-DD date range.", MemorySearchParams, _search_memory, ui_builder=_ui("table", "memory_results")))
_register(ToolSpec("get_secret_findings", "List open leaked-secret candidate findings from GitHub commits, optionally for one repository.", RepoParams, _router_intent("github_secrets"), ui_builder=_ui_secret_cards))
_register(ToolSpec("get_secret_findings_by_repository", "Count open secret candidate findings per repository.", NoParams, _router_intent("github_secrets_by_repo"), ui_builder=_ui("table", "repo_secrets")))
_register(ToolSpec("get_unreviewed_prs", "List open pull requests waiting for review, optionally for one repository.", RepoParams, _router_intent("github_prs"), ui_builder=_ui("table", "unreviewed_prs")))
_register(ToolSpec("get_github_repositories", "List the user's synchronized GitHub repositories.", NoParams, _router_intent("github_repos"), ui_builder=_ui_activity))
_register(ToolSpec("get_github_activity", "Get GitHub commit activity for a time window, optionally for one repository.", ActivityParams, _router_intent("github_commits"), ui_builder=_ui_activity))
_register(ToolSpec("get_activity_briefing", "Get a combined status report of recent GitHub and Gmail activity plus security findings.", TimeRangeParams, _router_intent("combined_briefing")))
_register(ToolSpec("get_daily_briefing", "Get the morning briefing (overnight GitHub events, today's calendar, security findings). Marks overnight events as delivered.", NoParams, _router_intent("wake_greeting"), ui_builder=_ui("briefing", "briefing")))
_register(ToolSpec("get_gmail_summary", "Summarize the Gmail inbox: important, promotional, and phishing-alert counts.", NoParams, _router_intent("gmail_summary"), ui_builder=_ui("summary", "gmail_summary")))
_register(ToolSpec("get_important_emails", "List important emails.", NoParams, _router_intent("gmail_important"), ui_builder=_ui("table", "important_emails")))
_register(ToolSpec("get_phishing_alerts", "List emails flagged as possible phishing (heuristic risk indicators, not proof).", NoParams, _router_intent("gmail_phishing"), ui_builder=_ui_from_context("alerts")))
_register(ToolSpec("get_promotional_emails", "List recent promotional / newsletter emails.", NoParams, _router_intent("gmail_promotional"), ui_builder=_ui_from_context("table")))
_register(ToolSpec("get_top_promotional_senders", "List the senders that send the most promotional emails.", NoParams, _router_intent("gmail_top_promotional_sender"), ui_builder=_ui("table", "top_senders")))
_register(ToolSpec("get_capabilities", "Describe what Evie can do (use for greetings and 'help').", NoParams, _router_intent("greeting")))
_register(ToolSpec("get_monitoring_overview", "Summarize what Evie currently monitors (repositories, inbox, security posture).", NoParams, _router_intent("general_conversational")))
_register(ToolSpec("show_more_details", "Show more detail about the previous answer (next page, those items, which ones).", FollowupParams, _show_more_details))

# General OS command (TRD Section 3): non-sensitive, allow-listed apps only, via services/os_adapter.py
_register(ToolSpec(
    "open_app", "Open (launch or bring to front) one allow-listed desktop application: Safari, Google Chrome, Firefox, Finder, Terminal, or Visual Studio Code. Cannot run commands or scripts.",
    OpenAppParams, _open_app, ui_builder=_ui_open_app,
))

# Computer control (docs/07 Section 37): one session per verbatim command, fast path + code-owned gate inside.
# Deterministic keyword route ONLY: the LLM planner neither sees nor may call it (set_text provenance requires the
# user's verbatim utterance). Risk is decided per action inside the session (docs/04 Section 4).
class ComputerControlParams(ToolParams):
    command: str = Field(..., min_length=1, max_length=2000, description="The user's verbatim computer-control request")


from services.computer_control.response_policy import determine_execution_response


def format_goal_reply(res_status: str, verification_summary: Optional[str] = None, question: Optional[str] = None, reason: str = "") -> str:
    decision = determine_execution_response(res_status, verification_summary, question, reason)
    return decision.user_reply



def _computer_control(params: ComputerControlParams, ctx: ToolContext) -> Dict[str, Any]:
    from services.computer_control.container import get_container, initialize_production_container
    from services.computer_control_router import get_runtime

    container = get_container()
    runtime = get_runtime()
    worker = container.worker if container else (runtime.worker if (runtime and hasattr(runtime, "worker")) else None)

    if worker is None and runtime is None:
        return {
            "reply": "Computer control isn't available here.",
            "computer_control": {
                "status": "UNAVAILABLE",
                "reason": "NO_COMPUTER_CONTROL_RUNTIME"
            },
            "tone": ctx.tone
        }

    if not container:
        container = initialize_production_container(worker=worker)

    session_id = ctx.conversation.get("session_id", "default_session")
    if ctx.channel == "voice":
        res = container.voice_adapter.process_voice_input(params.command, session_id=session_id)
    else:
        res = container.chat_adapter.process_chat_input(params.command, session_id=session_id)

    reply_text = format_goal_reply(
        res.status,
        verification_summary=getattr(res, "verification_summary", None),
        question=res.question,
        reason=res.reason
    )

    return {
        "reply": reply_text,
        "computer_control": res.model_dump(),
        "tone": ctx.tone
    }


_register(ToolSpec(
    "computer_control", "Carry out one computer-control command (switch app, type, scroll, align, arrow keys) on "
    "this Mac through a gated, verified session.", ComputerControlParams, _computer_control,
))
LLM_EXCLUDED_TOOLS = frozenset({"computer_control"})

# Confirmation-gated / sensitive tools
_register(ToolSpec(
    "resolve_finding", "Mark a security finding as resolved or a false positive. Requires explicit user confirmation.",
    ResolveFindingParams, _resolve_finding, is_sensitive=True, requires_confirmation=True,
    ui_builder=_ui("score", "score"), confirmation_prompt=_resolve_prompt,
))
_register(ToolSpec(
    "propose_calendar_event", "Propose creating a Google Calendar event. Only creates a proposal the user must confirm.",
    CalendarProposalParams, _propose_calendar_event, is_sensitive=True, requires_confirmation=True,
    handler_creates_proposal=True, ui_builder=_ui("proposal", "proposal"),
))
_register(ToolSpec(
    "propose_gmail_cleanup", "Propose unsubscribing from a promotional sender domain. Only creates proposals the user must confirm.",
    GmailCleanupParams, _propose_gmail_cleanup, is_sensitive=True, requires_confirmation=True,
    handler_creates_proposal=True, ui_builder=_ui("gmail_proposals", "proposals"),
))


# --- tool_registry table (04_Backend_Schema.md Section 4) ---

def init_tool_registry_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_registry (
                tool_name TEXT PRIMARY KEY,
                description TEXT,
                parameters_schema TEXT,
                is_sensitive INTEGER DEFAULT 0,
                requires_confirmation INTEGER DEFAULT 0
            );
            """
        )
        conn.commit()


def sync_tool_registry(db_path: str) -> None:
    """
    Provision tool_registry rows from the code definitions (called at startup/init).
    Rows without a code definition are left in place but can never execute.
    """
    init_tool_registry_table(db_path)
    with sqlite3.connect(db_path) as conn:
        for spec in TOOL_SPECS.values():
            conn.execute(
                """
                INSERT INTO tool_registry (tool_name, description, parameters_schema, is_sensitive, requires_confirmation)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tool_name) DO UPDATE SET
                    description = excluded.description,
                    parameters_schema = excluded.parameters_schema,
                    is_sensitive = excluded.is_sensitive,
                    requires_confirmation = excluded.requires_confirmation
                """,
                (spec.name, spec.description, json.dumps(spec.parameters_schema(), sort_keys=True),
                 int(spec.is_sensitive), int(spec.requires_confirmation)),
            )
        conn.commit()


def get_registered_rows(db_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Active tool_registry rows. On a database that has never had the table, it is
    provisioned once; an existing table is only read, so removed rows stay removed.
    """
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tool_registry'").fetchone()
    if not exists:
        sync_tool_registry(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tool_registry").fetchall()
    return {r["tool_name"]: dict(r) for r in rows}


def get_executable_tool(name: str, db_path: str) -> Optional[tuple]:
    """
    Return (spec, row) only when the tool exists in code AND in tool_registry.
    """
    if not isinstance(name, str):
        return None
    spec = TOOL_SPECS.get(name)
    row = get_registered_rows(db_path).get(name)
    if spec is None or row is None:
        return None
    return spec, row


def llm_tool_definitions(db_path: str) -> List[Dict[str, Any]]:
    """
    Tool schemas shown to the planner LLM: only tools that are executable.
    """
    rows = get_registered_rows(db_path)
    return [
        {"name": spec.name, "description": spec.description, "input_schema": spec.parameters_schema()}
        for name, spec in sorted(TOOL_SPECS.items())
        if name in rows and name not in LLM_EXCLUDED_TOOLS
    ]
