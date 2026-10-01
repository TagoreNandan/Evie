"""
Shared Tool-Calling Router Core for Evie (02_TRD.md Section 2).

One pipeline, two front doors: POST /chat and POST /voice/command both call
route_message(message, channel, db_path, context).

    message
      -> planner   (Claude tool-calling when configured; parse_user_intent() fallback)
      -> proposed tool name + arguments (plain data, never code)
      -> registry allow-check (code definition AND tool_registry row)
      -> Pydantic argument validation
      -> sensitive / confirmation gates
      -> handler
      -> responder (reply + structured ui payload)

The planner runs once per user message on the user's text only; tool results are
never fed back into it, so text inside retrieved data cannot trigger another action.
"""

import datetime
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from pydantic import ValidationError

from services import llm_client
from services.conversational_router import parse_user_intent
from services.tool_registry import (
    LLM_EXCLUDED_TOOLS,
    PROMO_TOPICS,
    ToolContext,
    ToolSpec,
    get_executable_tool,
    llm_tool_definitions,
)

logger = logging.getLogger(__name__)

CHANNELS = ("chat", "voice")
PROPOSAL_TTL_MINUTES = 15

# Deterministic fallback: existing intent -> (registered tool, entity keys it accepts)
INTENT_TOOL_MAP = {
    "wake_greeting": ("get_daily_briefing", ()),
    "greeting": ("get_capabilities", ()),
    "calendar_propose": ("propose_calendar_event", ()),
    "security_score": ("get_security_score", ()),
    "combined_briefing": ("get_activity_briefing", ("time_range",)),
    "github_secrets_by_repo": ("get_secret_findings_by_repository", ()),
    "github_secrets": ("get_secret_findings", ("repo_name",)),
    "github_prs": ("get_unreviewed_prs", ("repo_name",)),
    "gmail_important": ("get_important_emails", ()),
    "gmail_top_promotional_sender": ("get_top_promotional_senders", ()),
    "gmail_phishing": ("get_phishing_alerts", ()),
    "gmail_promotional": ("get_promotional_emails", ()),
    "gmail_summary": ("get_gmail_summary", ()),
    "github_repos": ("get_github_repositories", ()),
    "github_commits": ("get_github_activity", ("repo_name", "time_range")),
    "general_conversational": ("get_monitoring_overview", ()),
}

# Intents whose capability is intentionally not registered (AGENTS.md: paused).
PAUSED_INTENTS = {
    "drs_ask": "Research and decision-support questions are paused in this version of Evie, so I didn't run that.",
}

VOICE_VERIFICATION_REPLY = (
    "Voice verification isn't available right now, so I didn't do anything. "
    "You can request it from the dashboard or chat, where it will ask for your confirmation."
)

# Safe, user-facing replies for speaker-verification failure reasons (all fail closed).
SPEAKER_FAILURE_REPLIES = {
    "NOT_ENROLLED": "That action needs your enrolled voice. Enroll your voice from the dashboard first - nothing was done.",
    "SPEAKER_MISMATCH": "I couldn't verify your voice for that action, so I didn't do anything.",
    "NO_VOICE_SAMPLE": "I didn't get a voice sample to verify you, so I didn't do anything.",
    "INVALID_AUDIO": "I couldn't use that voice sample to verify you, so I didn't do anything.",
}


@dataclass
class KeywordPlan:
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    rejection: Optional[str] = None


# --- Planning ---

def keyword_plan(message: str, db_path: str, conversation: Dict[str, Any]) -> KeywordPlan:
    """Map the existing keyword intent classifier onto registered tools."""
    # General OS command: an explicit open/launch/start + allow-listed app (TRD Section 3)
    from services.os_adapter import find_app_request
    app_name = find_app_request(message)
    if app_name:
        return KeywordPlan(tool_name="open_app", arguments={"app_name": app_name})

    # Computer control / Assistant Intelligence integration (Step 13)
    from services.computer_control.container import get_container
    container = get_container()
    if container:
        ctx_builder = container.assistant_context_builder
        assistant_ctx = ctx_builder.build_context(
            session_id=conversation.get("session_id", "default_session"),
            user_utterance=message,
            channel="chat"
        )
        intelligence = container.assistant_intelligence_engine
        decision = intelligence.reason(message, assistant_ctx)

        from services.computer_control.assistant_intelligence import AssistantDecisionKind
        if decision.decision == AssistantDecisionKind.COMPUTER_GOAL:
            return KeywordPlan(tool_name="computer_control", arguments={"command": decision.goal or message})
        elif decision.decision == AssistantDecisionKind.ASK:
            return KeywordPlan(rejection=decision.response or "What document or application would you like me to use?")
        elif decision.decision == AssistantDecisionKind.CANNOT_PROCEED:
            return KeywordPlan(rejection=decision.reason or "I cannot proceed with that request.")
        elif decision.decision == AssistantDecisionKind.CONVERSATION and decision.response:
            return KeywordPlan(rejection=decision.response)

    intent_data = parse_user_intent(message, db_path=db_path)
    intent = intent_data.get("intent")
    entities = intent_data.get("entities", {})

    if intent in PAUSED_INTENTS:
        return KeywordPlan(rejection=PAUSED_INTENTS[intent])

    if intent == "contextual_followup":
        msg_lower = message.lower()
        if conversation.get("topic") in PROMO_TOPICS and ("tell" in msg_lower or "unsubscribe" in msg_lower):
            return KeywordPlan(tool_name="propose_gmail_cleanup")
        return KeywordPlan(tool_name="show_more_details", arguments={"request": message[:200]})

    from services.computer_control.classifier import classify_intent_deterministic, ClassificationKind
    class_res = classify_intent_deterministic(message)
    if class_res.kind == ClassificationKind.COMPUTER_GOAL:
        return KeywordPlan(tool_name="computer_control", arguments={"command": class_res.goal or message})

    if intent not in INTENT_TOOL_MAP:
        return KeywordPlan(tool_name="get_monitoring_overview")

    tool_name, keys = INTENT_TOOL_MAP[intent]
    arguments = {k: entities[k] for k in keys if entities.get(k) is not None}
    return KeywordPlan(tool_name=tool_name, arguments=arguments)


def build_planner_context(db_path: str, conversation: Dict[str, Any], channel: str) -> str:
    """
    Minimal, non-secret context for the planner: date, channel, previous topic, and an
    index of open findings (ids and locations only - no previews, emails, or tokens).
    """
    lines = [
        f"today: {datetime.date.today().isoformat()}",
        f"channel: {channel}",
        f"previous_topic: {conversation.get('topic') or 'none'}",
        "open_findings:",
    ]
    queries = [
        ("secret", "SELECT id, repo_name, file_path, pattern_type, detected_at FROM secret_findings WHERE status = 'open' ORDER BY id DESC LIMIT 15"),
        ("exposure", "SELECT id, source, exposure_type, detected_at FROM exposure_findings WHERE status = 'open' ORDER BY id DESC LIMIT 5"),
        ("dependency", "SELECT id, repo_name, package_name, severity FROM dependency_alerts WHERE status = 'open' ORDER BY id DESC LIMIT 5"),
        ("breach", "SELECT id, breach_name, breach_date FROM breach_checks WHERE acknowledged = 0 ORDER BY id DESC LIMIT 5"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for finding_type, sql in queries:
            try:
                for r in conn.execute(sql).fetchall():
                    attrs = " ".join(f"{k}={r[k]}" for k in r.keys() if k != "id")
                    lines.append(f"- {finding_type} id={r['id']} {attrs}")
            except sqlite3.OperationalError:
                pass
    return "\n".join(lines)


# --- Responses ---

def _safe_tool_name(name: Any) -> Optional[str]:
    if isinstance(name, str) and re.fullmatch(r"[a-z0-9_]{1,64}", name):
        return name
    return None


def _rejection(reply: str, reason: str, tone: str, planner: str, tool_name: Any = None, status: str = "rejected") -> Dict[str, Any]:
    return {
        "reply": reply,
        "tone": tone,
        "ui": {"type": "notice", "data": {"reason": reason}},
        "tool": {"name": _safe_tool_name(tool_name), "planner": planner, "status": status, "reason": reason},
    }


def _finalize(result: Dict[str, Any], spec: ToolSpec, ctx: ToolContext, planner: str, status: str) -> Dict[str, Any]:
    result = dict(result)
    result.setdefault("tone", ctx.tone)
    ui = spec.ui_builder(result, ctx) if spec.ui_builder else None
    result["ui"] = ui or {"type": "text", "data": {}}
    result["tool"] = {"name": spec.name, "planner": planner, "status": status}
    return result


def _run(spec: ToolSpec, params: Any, ctx: ToolContext, planner: str) -> Dict[str, Any]:
    try:
        result = spec.handler(params, ctx)
    except Exception as e:
        logger.warning("Tool %s failed: %s", spec.name, type(e).__name__)
        return _rejection("I couldn't complete that request, so there's no result to show.", "TOOL_EXECUTION_FAILED", ctx.tone, planner, spec.name, status="failed")
    status = "proposed" if spec.handler_creates_proposal else "executed"
    return _finalize(result, spec, ctx, planner, status)


# --- Execution boundary ---

def _validate(spec: ToolSpec, arguments: Any) -> Optional[Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None
    try:
        return spec.params_model.model_validate(arguments)
    except ValidationError:
        return None


def execute_tool_call(
    tool_name: Any,
    arguments: Any,
    *,
    channel: str,
    db_path: str,
    conversation: Dict[str, Any],
    tone: str = "neutral",
    planner: str = "keyword",
    speaker_check: Optional[Callable[[], Any]] = None,
    audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    The only path from a planned tool call to a handler.
    speaker_check is a lazy callable (voice only) returning an object with .verified and
    .reason; it runs only when a sensitive tool has been selected and validated.
    audit, when given, receives the validated parameters and speaker outcome for logging.
    """
    audit = audit if audit is not None else {}
    found = get_executable_tool(tool_name, db_path)
    if found is None:
        return _rejection("I can't do that - it isn't one of the actions I'm allowed to take.", "TOOL_NOT_REGISTERED", tone, planner, tool_name)
    spec, row = found
    if planner == "llm" and spec.name in LLM_EXCLUDED_TOOLS:     # verbatim-utterance provenance (computer control)
        return _rejection("I can't do that from a paraphrased request.", "TOOL_NOT_AVAILABLE_TO_LLM", tone, planner,
                          spec.name)

    params = _validate(spec, arguments)
    if params is None:
        return _rejection("I couldn't understand the details for that request, so I didn't run it.", "INVALID_ARGUMENTS", tone, planner, spec.name)

    audit["resolved_parameters"] = params.model_dump()
    ctx = ToolContext(db_path=db_path, channel=channel, tone=tone, conversation=conversation)
    is_sensitive = spec.is_sensitive or bool(row.get("is_sensitive"))
    requires_confirmation = spec.requires_confirmation or bool(row.get("requires_confirmation"))

    # Sensitive voice actions require local speaker verification before any
    # confirmation step is shown (AGENTS.md hard rule 3). Anything else fails closed.
    speaker_verified = False
    if is_sensitive and channel == "voice":
        outcome = _check_speaker(speaker_check)
        audit["speaker_verified"] = 1 if outcome.verified else 0
        if not outcome.verified:
            reply = SPEAKER_FAILURE_REPLIES.get(outcome.reason, VOICE_VERIFICATION_REPLY)
            return _rejection(reply, outcome.reason, tone, planner, spec.name, status="blocked")
        speaker_verified = True

    if requires_confirmation and not spec.handler_creates_proposal:
        return _create_proposal(spec, params, ctx, planner, speaker_verified=speaker_verified)

    return _run(spec, params, ctx, planner)


@dataclass(frozen=True)
class _SpeakerOutcome:
    verified: bool
    reason: str


def _check_speaker(speaker_check: Optional[Callable[[], Any]]) -> Any:
    if speaker_check is None:
        return _SpeakerOutcome(False, "SPEAKER_VERIFICATION_UNAVAILABLE")
    try:
        outcome = speaker_check()
    except Exception as e:
        logger.warning("Speaker check failed: %s", type(e).__name__)
        return _SpeakerOutcome(False, "VERIFICATION_ERROR")
    if getattr(outcome, "verified", False) is not True:
        return _SpeakerOutcome(False, str(getattr(outcome, "reason", "VERIFICATION_ERROR")))
    return outcome


def route_message(
    message: str,
    channel: str,
    db_path: str,
    context: Optional[Dict[str, Any]] = None,
    active_tone: str = "neutral",
    speaker_check: Optional[Callable[[], Any]] = None,
    audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Shared router core for chat and voice.
    """
    if channel not in CHANNELS:
        raise ValueError(f"Unsupported channel '{channel}'")
    conversation = context if context is not None else {}
    message = (message or "").strip()
    if not message:
        return _rejection("I didn't catch a request.", "EMPTY_MESSAGE", active_tone, "none")

    # General OS command: explicit open/launch + allow-listed app (TRD Section 3)
    from services.os_adapter import find_app_request
    app_name = find_app_request(message)
    if app_name:
        return execute_tool_call(
            "open_app", {"app_name": app_name}, channel=channel, db_path=db_path,
            conversation=conversation, tone=active_tone, planner="keyword",
            speaker_check=speaker_check, audit=audit,
        )

    # Bounded semantic assistant intelligence boundary (Step 13)
    from services.computer_control.container import get_container
    container = get_container()
    if container:
        ctx_builder = container.assistant_context_builder
        assistant_ctx = ctx_builder.build_context(
            session_id=conversation.get("session_id", "default_session"),
            user_utterance=message,
            channel=channel
        )
        intelligence = container.assistant_intelligence_engine
        decision = intelligence.reason(message, assistant_ctx)

        from services.computer_control.assistant_intelligence import AssistantDecisionKind
        if decision.decision == AssistantDecisionKind.COMPUTER_GOAL:
            return execute_tool_call(
                "computer_control", {"command": decision.goal or message}, channel=channel, db_path=db_path,
                conversation=conversation, tone=active_tone, planner="assistant_intelligence",
                speaker_check=speaker_check, audit=audit,
            )
        elif decision.decision == AssistantDecisionKind.ASK:
            return _rejection(
                decision.response or "What document or application would you like me to use?",
                "CLARIFICATION_REQUIRED", active_tone, "assistant_intelligence"
            )
        elif decision.decision == AssistantDecisionKind.CANNOT_PROCEED:
            return _rejection(
                decision.reason or "I cannot proceed with that request.",
                "CANNOT_PROCEED", active_tone, "assistant_intelligence"
            )
        elif decision.decision == AssistantDecisionKind.CONVERSATION and decision.response:
            return {
                "reply": decision.response,
                "tone": active_tone,
                "ui": {"type": "text", "data": {}},
                "tool": {"name": None, "planner": "assistant_intelligence", "status": "conversational"},
            }


    try:
        decision = llm_client.plan_tool_call(
            message, llm_tool_definitions(db_path), build_planner_context(db_path, conversation, channel)
        )
    except llm_client.LLMUnavailable:
        decision = None

    if decision is not None:
        if decision.tool_name is None:
            return {
                "reply": decision.text,
                "tone": active_tone,
                "ui": {"type": "text", "data": {}},
                "tool": {"name": None, "planner": "llm", "status": "no_tool"},
            }
        return execute_tool_call(
            decision.tool_name, decision.tool_input, channel=channel, db_path=db_path,
            conversation=conversation, tone=active_tone, planner="llm",
            speaker_check=speaker_check, audit=audit,
        )

    plan = keyword_plan(message, db_path, conversation)
    if plan.rejection:
        return _rejection(plan.rejection, "TOOL_NOT_AVAILABLE", active_tone, "keyword")
    return execute_tool_call(
        plan.tool_name, plan.arguments, channel=channel, db_path=db_path,
        conversation=conversation, tone=active_tone, planner="keyword",
        speaker_check=speaker_check, audit=audit,
    )


# --- Generic Propose -> Confirm -> Execute (modelled on calendar_actions) ---

def init_tool_proposals_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_action_proposals (
                proposal_token TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | claimed | executed | execution_failed | cancelled | expired | rejected
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                executed_at TIMESTAMP,
                speaker_verified INTEGER NOT NULL DEFAULT 0  -- 1 only for voice proposals that passed speaker verification
            );
            """
        )
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tool_action_proposals)")}
        if "speaker_verified" not in columns:
            conn.execute("ALTER TABLE tool_action_proposals ADD COLUMN speaker_verified INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _create_proposal(spec: ToolSpec, params: Any, ctx: ToolContext, planner: str, speaker_verified: bool = False) -> Dict[str, Any]:
    init_tool_proposals_table(ctx.db_path)
    token = str(uuid.uuid4())
    created = _now()
    expires = created + datetime.timedelta(minutes=PROPOSAL_TTL_MINUTES)
    arguments = params.model_dump()
    with sqlite3.connect(ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO tool_action_proposals (proposal_token, tool_name, arguments, channel, status, created_at, expires_at, speaker_verified) "
            "VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?)",
            (token, spec.name, json.dumps(arguments), ctx.channel, created.isoformat(), expires.isoformat(), 1 if speaker_verified else 0),
        )
        conn.commit()

    prompt = spec.confirmation_prompt(params) if spec.confirmation_prompt else f"Run {spec.name}?"
    proposal = {
        "proposal_token": token,
        "tool_name": spec.name,
        "arguments": arguments,
        "prompt": prompt,
        "expires_at": expires.isoformat(),
        "confirm_endpoint": "/tools/confirm",
        "speaker_verified": bool(speaker_verified),
    }
    return {
        "reply": f"{prompt} Nothing has changed yet - please confirm to proceed.",
        "tool_proposal": proposal,
        "tone": ctx.tone,
        "ui": {"type": "tool_proposal", "data": proposal},
        "tool": {"name": spec.name, "planner": planner, "status": "proposed"},
    }


def _set_status(db_path: str, token: str, new_status: str, from_status: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE tool_action_proposals SET status = ?, executed_at = ? WHERE proposal_token = ? AND status = ?",
            (new_status, _now().isoformat(), token, from_status),
        )
        conn.commit()
        return cur.rowcount


def _proposal_outcome(status: str, reply: str, tone: str) -> Dict[str, Any]:
    return {"status": status, "reply": reply, "tone": tone, "ui": {"type": "notice", "data": {"reason": status.upper()}}}


def confirm_tool_proposal(
    db_path: str,
    proposal_token: str,
    confirmed: bool,
    tone: str = "neutral",
    conversation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Confirm or cancel a pending tool proposal. Executes at most once: expired, cancelled,
    replayed, or concurrently claimed proposals never run.
    """
    init_tool_proposals_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tool_action_proposals WHERE proposal_token = ?", (proposal_token,)).fetchone()
    if row is None:
        return _proposal_outcome("not_found", "That confirmation request doesn't exist.", tone)
    if row["status"] != "proposed":
        return _proposal_outcome("already_processed", f"That request was already {row['status']}; nothing more was done.", tone)

    if _now() > datetime.datetime.fromisoformat(row["expires_at"]):
        _set_status(db_path, proposal_token, "expired", "proposed")
        return _proposal_outcome("expired", "That request expired before it was confirmed, so nothing was done.", tone)

    if not confirmed:
        _set_status(db_path, proposal_token, "cancelled", "proposed")
        return _proposal_outcome("cancelled", "Cancelled - nothing was changed.", tone)

    # Atomic claim: only one confirmation can move the proposal out of 'proposed'.
    if not _set_status(db_path, proposal_token, "claimed", "proposed"):
        return _proposal_outcome("already_processed", "That request is already being handled; nothing more was done.", tone)

    found = get_executable_tool(row["tool_name"], db_path)
    params = _validate(found[0], json.loads(row["arguments"])) if found else None
    voice_unverified = (
        row["channel"] == "voice"
        and (found is None or found[0].is_sensitive or found[1].get("is_sensitive"))
        and row["speaker_verified"] != 1
    )
    if found is None or params is None or voice_unverified:
        _set_status(db_path, proposal_token, "rejected", "claimed")
        return _proposal_outcome("rejected", "That action is no longer allowed, so nothing was done.", tone)

    spec = found[0]
    ctx = ToolContext(db_path=db_path, channel=row["channel"], tone=tone, conversation=conversation if conversation is not None else {})
    result = _run(spec, params, ctx, planner="confirmation")
    final_status = "executed" if result["tool"]["status"] == "executed" else "execution_failed"
    _set_status(db_path, proposal_token, final_status, "claimed")
    result["status"] = final_status
    return result
