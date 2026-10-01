"""
FastAPI Server & Briefing Delivery Triggers for Evie Assistant.

Implements BRIEF-02, BRIEF-03, BRIEF-04 & Section 2 of 02_TRD.md:
- POST /webhooks/github: Real-time GitHub webhook listener with mandatory HMAC signature verification & idempotency.
- GET /briefing/wake: Wake-triggered morning briefing report.
- GET /briefing/on-demand (and /report/full): On-demand briefing report query.
- POST /calendar/propose & POST /calendar/confirm: Two-step confirmation-gated calendar scheduling endpoints.
"""

import hmac
import hashlib
import os
import re
import sqlite3
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict

import keyring_utils
import redaction
import drs
import tone
from services.briefing import generate_briefing, init_github_events_table

app = FastAPI(title="Evie Assistant API", version="1.0.0")
DB_PATH = "evie.db"

if os.path.exists("ui"):
    app.mount("/static", StaticFiles(directory="ui"), name="static")


@app.get("/")
def read_root():
    """
    Serve Evie Web UI single-page application (UI-01).
    """
    if os.path.exists("ui/index.html"):
        return FileResponse("ui/index.html")
    return {"message": "Evie Assistant API Running"}


def init_db(db_path: str = DB_PATH) -> None:
    """
    Initialize all SQLite database tables for Evie backend.
    """
    init_github_events_table(db_path)
    drs.init_drs_table(db_path)
    tone.init_user_config_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_token TEXT UNIQUE,
                action_type TEXT NOT NULL,
                event_summary TEXT,
                start_time TEXT,
                end_time TEXT,
                description TEXT,
                event_id TEXT,
                confirmed_by_user INTEGER DEFAULT 0,
                status TEXT DEFAULT 'proposed',
                executed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS secret_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line_number INTEGER,
                pattern_type TEXT,
                redacted_preview TEXT,
                status TEXT DEFAULT 'open',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS breach_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_checked TEXT NOT NULL,
                breach_name TEXT,
                breach_date TEXT,
                data_classes TEXT,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                acknowledged INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exposure_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT,
                exposure_type TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twofa_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                twofa_enabled INTEGER,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

    from services.tool_registry import sync_tool_registry
    sync_tool_registry(db_path)
    from services.voice_pipeline import init_voice_command_log_table
    init_voice_command_log_table(db_path)
    from services.computer_control_log import SqliteStepLog, init_computer_control_steps_table
    init_computer_control_steps_table(db_path)


@app.on_event("startup")
def startup_event():
    init_db(DB_PATH)


# --- GitHub Webhook Endpoint (BRIEF-02) ---

@app.post("/webhooks/github")
async def github_webhook(request: Request):
    """
    Real-time GitHub webhook listener.
    Fails closed: rejects every delivery unless github_webhook_secret is configured in the
    keyring AND the X-Hub-Signature-256 HMAC-SHA256 signature verifies.
    Enforces idempotency using X-GitHub-Delivery header.
    Stores minimal metadata only (no raw JSON payload).
    """
    body_bytes = await request.body()
    headers = request.headers

    # 1. Mandatory HMAC Signature Verification (fail closed)
    webhook_secret = keyring_utils.get_credential("evie_assistant", "github_webhook_secret")
    if not webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEBHOOK_SECRET_NOT_CONFIGURED: deliveries are rejected until github_webhook_secret is set in the keyring",
        )
    signature_header = headers.get("X-Hub-Signature-256")
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="UNAUTHORIZED_WEBHOOK: Missing or invalid signature header",
        )
    received_sig = signature_header[7:]
    expected_sig = hmac.new(
        webhook_secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received_sig, expected_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="UNAUTHORIZED_WEBHOOK: Invalid HMAC signature",
        )
    signature_verified = True  # only reachable after compare_digest succeeded

    # 2. Idempotency Verification via X-GitHub-Delivery
    delivery_id = headers.get("X-GitHub-Delivery")
    if delivery_id:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM github_events WHERE delivery_id = ?", (delivery_id,))
            if cursor.fetchone()[0] > 0:
                return {"status": "already_processed", "delivery_id": delivery_id}

    # 3. Data Minimization Payload Parsing
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INVALID_PAYLOAD: Failed to parse JSON body",
        )

    event_type = headers.get("X-GitHub-Event", "unknown")
    repo_name = payload.get("repository", {}).get("full_name") or payload.get("repository", {}).get("name", "unknown")
    actor = payload.get("sender", {}).get("login", "unknown")

    if event_type == "push":
        commits_count = len(payload.get("commits", []))
        summary = f"{commits_count} commit(s) pushed to {payload.get('ref', 'repo')}"
    elif event_type == "pull_request":
        action = payload.get("action", "updated")
        pr_num = payload.get("number", "")
        summary = f"PR #{pr_num} {action}"
    else:
        summary = f"GitHub event {event_type} received"

    # 4. Insert Minimal Event Record & Webhook Delivery Tracking
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO github_events (delivery_id, repo_name, event_type, actor, summary, notified_realtime, included_in_briefing)
            VALUES (?, ?, ?, ?, ?, 1, 0)
            """,
            (delivery_id, repo_name, event_type, actor, summary),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                repo_name TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                signature_valid INTEGER NOT NULL
            );
            """
        )
        if delivery_id:
            conn.execute(
                """
                INSERT OR IGNORE INTO github_webhook_deliveries (delivery_id, event_type, repo_name, signature_valid)
                VALUES (?, ?, ?, ?)
                """,
                (delivery_id, event_type, repo_name, 1 if signature_verified else 0),
            )
        conn.commit()

    # 5. Process Webhook Event Immediately into Database Activity & Secret Findings
    from services.github_intelligence import process_github_webhook_event
    event_payload = dict(payload)
    event_payload["event_type"] = event_type
    webhook_res = process_github_webhook_event(event_payload, DB_PATH)

    # 6. Trigger Real-time Briefing Aggregation
    briefing_report = generate_briefing(DB_PATH, trigger="webhook", repo_name=repo_name)
    return {
        "status": "processed",
        "delivery_id": delivery_id,
        "event_summary": summary,
        "webhook_processing": webhook_res,
        "briefing": briefing_report,
    }


# --- Dashboard State Endpoint (Phase C) ---

@app.get("/dashboard/state")
def get_dashboard_state_endpoint():
    """
    Structured snapshot endpoint for Evie dashboard UI state.
    Provides overall status (Good / Needs Attention / Critical), system section summaries,
    security findings, recent activity, and clear heuristic disclaimers for phishing flags.
    """
    from services.dashboard_state import get_dashboard_state
    try:
        return get_dashboard_state(DB_PATH)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"DASHBOARD_STATE_FAILED: {str(e)}",
        )


# --- Wake-triggered & On-Demand Briefing Endpoints (BRIEF-03, BRIEF-04) ---

@app.get("/briefing/wake")
def briefing_wake():
    """
    Wake-triggered briefing report (e.g. morning greeting).
    """
    try:
        return generate_briefing(DB_PATH, trigger="wake")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="BRIEFING_GENERATION_FAILED",
        )


@app.get("/briefing/on-demand")
@app.get("/report/full")
def briefing_on_demand():
    """
    On-demand briefing report query ("what's the report").
    """
    try:
        return generate_briefing(DB_PATH, trigger="on_demand")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="BRIEFING_GENERATION_FAILED",
        )


# --- Calendar Confirmation Models & Endpoint Stubs (CAL-01, CAL-02) ---

class CalendarProposeRequest(BaseModel):
    action_type: str = Field(..., description="Action type: create, update, or delete")
    summary: str = Field(..., max_length=255)
    start_time: str
    end_time: str
    description: str = Field("", max_length=2000)
    event_id: str | None = None


class CalendarConfirmRequest(BaseModel):
    proposal_token: str
    confirmed: bool


@app.post("/calendar/propose")
def calendar_propose_endpoint(req: CalendarProposeRequest):
    """
    Propose a calendar action. Returns a proposal token and confirmation prompt.
    Does NOT call Google Calendar API.
    """
    from integrations.calendar_actions import propose_calendar_action
    try:
        return propose_calendar_action(
            db_path=DB_PATH,
            action_type=req.action_type,
            summary=req.summary,
            start_time=req.start_time,
            end_time=req.end_time,
            description=req.description,
            event_id=req.event_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="PROPOSAL_CREATION_FAILED")


@app.post("/calendar/confirm")
def calendar_confirm_endpoint(req: CalendarConfirmRequest):
    """
    Confirm and execute a proposed calendar action after atomic SQLite claim.
    """
    from integrations.calendar_actions import confirm_calendar_action
    try:
        return confirm_calendar_action(
            db_path=DB_PATH,
            proposal_token=req.proposal_token,
            confirmed=req.confirmed,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="CALENDAR_EXECUTION_FAILED")
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="CALENDAR_EXECUTION_FAILED")


# --- Gmail Cleanup Proposal Endpoints (MAIL-01) ---

class GmailCleanupProposeRequest(BaseModel):
    sender_domain: str = Field(..., description="Domain name to unsubscribe or cleanup")
    action_type: str = Field("unsubscribe_recommendation", description="Action type: unsubscribe_recommendation or delete_newsletter")


class GmailCleanupConfirmRequest(BaseModel):
    proposal_token: str
    confirmed: bool


@app.post("/gmail/propose_cleanup")
def gmail_propose_cleanup_endpoint(req: GmailCleanupProposeRequest):
    """
    Propose a Gmail unsubscribe or cleanup action. Returns proposal token.
    Does NOT execute deletion or unsubscribe automatically.
    """
    from services.gmail_intelligence import propose_gmail_cleanup_action
    try:
        return propose_gmail_cleanup_action(
            db_path=DB_PATH,
            sender_domain=req.sender_domain,
            action_type=req.action_type,
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GMAIL_PROPOSAL_FAILED")


@app.post("/gmail/confirm_cleanup")
def gmail_confirm_cleanup_endpoint(req: GmailCleanupConfirmRequest):
    """
    Confirm and claim a proposed Gmail cleanup action.
    """
    from services.gmail_intelligence import confirm_gmail_cleanup_action
    try:
        return confirm_gmail_cleanup_action(
            db_path=DB_PATH,
            proposal_token=req.proposal_token,
            confirmed=req.confirmed,
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GMAIL_CONFIRMATION_FAILED")


# --- Journal & Memory Endpoints (MEM-01, MEM-02) ---


class JournalEntryRequest(BaseModel):
    entry_date: str = Field(..., description="ISO date YYYY-MM-DD")
    content: str = Field(..., min_length=1)
    source_data: Dict[str, Any] | None = None


@app.post("/journal/entry")
def create_journal_entry_endpoint(req: JournalEntryRequest):
    """
    Ingest and index a daily journal entry (MEM-01).
    """
    import memory
    try:
        return memory.add_journal_entry(
            entry_date=req.entry_date,
            content=req.content,
            source_data=req.source_data,
            db_path=DB_PATH,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="JOURNAL_INGESTION_FAILED")


@app.get("/journal/search")
def search_journal_entries_endpoint(
    q: str,
    start_date: str | None = None,
    end_date: str | None = None,
    top_k: int = 5,
):
    """
    Search journal entries using vector RAG recall and optional date filtering (MEM-02).
    """
    import memory
    try:
        return memory.search_journal_entries(
            query=q,
            top_k=top_k,
            start_date=start_date,
            end_date=end_date,
            db_path=DB_PATH,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="JOURNAL_SEARCH_FAILED")


# --- Phase 4 Security Score & Remediation Endpoints (SCORE-01, SCORE-02) ---

@app.get("/score")
def get_security_score_endpoint():
    """
    Get current Security Health Score and historical trend (SCORE-01).
    Reads security_score_current — nothing else.
    """
    import security_score
    try:
        return security_score.get_current_security_score(DB_PATH)
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SCORE_CALCULATION_FAILED")


@app.get("/findings")
def list_findings_endpoint():
    """
    List open security findings across all categories.
    """
    from services.finding_actions import list_open_findings
    return list_open_findings(DB_PATH)


class FindingResolveRequest(BaseModel):
    finding_type: str = Field("secret", description="Finding type: secret, exposure, dependency, breach")
    status: str = Field("resolved", description="Resolution status: resolved or false_positive")


@app.post("/findings/{finding_id}/resolve")
def resolve_finding_endpoint(finding_id: int, req: FindingResolveRequest = FindingResolveRequest()):
    """
    Mark a finding resolved/false-positive, then immediately recompute the canonical score.
    """
    from services.finding_actions import resolve_finding
    finding_type = req.finding_type.lower().strip()
    try:
        score = resolve_finding(DB_PATH, finding_type, finding_id, req.status)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if score is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FINDING_NOT_FOUND")
    return {"finding_id": finding_id, "finding_type": finding_type, "status": req.status, "score": score}


@app.get("/remediation/{finding_type}/{finding_id}")
def get_remediation_guide_endpoint(finding_type: str, finding_id: int):
    """
    Get step-by-step remediation guide for a specific finding (SCORE-02).
    """
    import remediation
    try:
        return remediation.get_remediation_guide(finding_type, finding_id, DB_PATH)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="REMEDIATION_GUIDE_FAILED")


# --- Phase 5 DRS & Personality Tone Endpoints (DRS-01, TONE-01) ---

class DRSAskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Decision Research Support query")


class ToneConfigRequest(BaseModel):
    tone_preference: str = Field(..., description="Tone preference: neutral, casual, formal, humorous")


@app.post("/drs/ask")
def drs_ask_endpoint(req: DRSAskRequest):
    """
    Execute evidence-based Decision Research Support query with web links & disclaimer (DRS-01).
    """
    try:
        return drs.ask_drs_question(req.question, db_path=DB_PATH)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DRS_QUERY_FAILED")


@app.post("/config/tone")
def set_tone_preference_endpoint(req: ToneConfigRequest):
    """
    Update assistant conversational tone preference (TONE-01).
    """
    try:
        updated_tone = tone.set_tone_preference(req.tone_preference, db_path=DB_PATH)
        return {"status": "success", "tone_preference": updated_tone}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="TONE_CONFIG_FAILED")


@app.get("/config/tone")
def get_tone_preference_endpoint():
    """
    Retrieve active assistant conversational tone preference (TONE-01).
    """
    try:
        current_tone = tone.get_tone_preference(db_path=DB_PATH)
        return {"tone_preference": current_tone}
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="TONE_CONFIG_FAILED")


from fastapi import FastAPI, HTTPException, status, BackgroundTasks

@app.post("/integrations/sync")
def sync_integrations_endpoint(background_tasks: BackgroundTasks):
    """
    Trigger real telemetry sync across active external integrations (SEC-06).
    Runs asynchronously in the background so the endpoint returns immediately.
    """
    import services.real_integrations as real_integrations
    try:
        background_tasks.add_task(real_integrations.sync_all_real_integrations, DB_PATH)
        return {"status": "sync_started", "message": "Background integration sync started"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="INTEGRATION_SYNC_FAILED")


# --- Conversational Chat Endpoint (04_Backend_Schema.md) ---

class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Conversational user message")


# Context tracking for lightweight follow-up queries ("show me those", "which ones?", "tell them", etc.)
LAST_CHAT_CONTEXT: Dict[str, Any] = {"topic": None, "data": None}


class VoiceCommandRequest(BaseModel):
    transcript: str = Field(..., min_length=1, max_length=2000, description="Speech-to-text transcript from the browser")
    # Size/format are validated by services.speaker_verification (not here) so a rejected
    # sample is never echoed back in a validation error.
    audio_wav_base64: str | None = Field(None, description="Local 16 kHz mono WAV of the same utterance, base64")


@app.post("/voice/command")
def voice_command_endpoint(req: VoiceCommandRequest):
    """
    Voice entry point (02_TRD.md Section 3): the transcript goes through the same shared
    router core as /chat. Sensitive tools run only after local speaker verification of the
    attached sample passes; otherwise they fail closed. Every command is logged in
    voice_command_log. The audio is used in memory only and discarded.
    """
    from services import speaker_verification, tool_router, voice_pipeline
    active_tone = tone.get_tone_preference(DB_PATH)
    audio = req.audio_wav_base64

    def speaker_check():
        return speaker_verification.verify(audio)

    audit: Dict[str, Any] = {}
    result = tool_router.route_message(
        req.transcript, channel="voice", db_path=DB_PATH, context=LAST_CHAT_CONTEXT,
        active_tone=active_tone, speaker_check=speaker_check, audit=audit,
    )
    voice_pipeline.log_voice_command(DB_PATH, req.transcript, result, audit)
    return result


class VoiceAutomationRequest(BaseModel):
    transcript: str | None = Field(None, description="Spoken computer-control command transcript")
    session_id: str = Field(..., description="Active voice automation session identifier")
    action: str | None = Field("command", description="Optional lifecycle action: start, command, or stop")


_voice_automation_client = None


def get_voice_automation_client():
    global _voice_automation_client
    if _voice_automation_client is None:
        from services.computer_control.voice_automation_client import VoiceAutomationClient
        _voice_automation_client = VoiceAutomationClient()
    return _voice_automation_client


@app.post("/voice/automation")
def voice_automation_endpoint(req: VoiceAutomationRequest):
    """
    Dedicated Voice Automation Endpoint.
    Direct computer control execution via signed voice_automation_service IPC over Unix domain socket.
    Completely isolated from conversational chat/LLM.
    Returns no conversational response on success (silent execution).
    Only returns spoken text when clarification is required or on execution error.
    """
    from services.computer_control.voice_automation_client import (
        AuthenticationError,
        CommandTimeoutError,
        ServiceUnavailableError,
        SessionEndedError,
        VoiceAutomationClientError,
    )
    from services import voice_pipeline

    action = (req.action or "command").lower()
    session_id = (req.session_id or "").strip()
    if not session_id:
        return {"status": "ERROR", "reply": "Session ID required."}

    client = get_voice_automation_client()

    if action == "start":
        try:
            res = client.start(session_id)
            if res.get("status") == "OK":
                return {"status": "OK", "session_id": session_id, "reply": None}
            err = res.get("error") or "Voice Automation start failed."
            return {"status": "ERROR", "reply": err}
        except ServiceUnavailableError:
            return {"status": "ERROR", "reply": "Voice Automation service unavailable."}
        except AuthenticationError:
            return {"status": "ERROR", "reply": "Voice Automation authentication failed."}
        except Exception as e:
            return {"status": "ERROR", "reply": f"Failed to start Voice Automation: {e}"}

    elif action == "stop":
        try:
            res = client.stop(session_id)
            if res.get("status") == "OK":
                return {"status": "OK", "session_id": session_id, "reply": None}
            err = res.get("error") or "Voice Automation stop failed."
            return {"status": "ERROR", "reply": err}
        except Exception as e:
            return {"status": "ERROR", "reply": f"Failed to stop Voice Automation: {e}"}

    # Default action: command
    transcript = (req.transcript or "").strip()
    if not transcript:
        return {"status": "ERROR", "reply": "I didn't catch a command."}

    session_ended = {"status": "SESSION_ENDED", "reply": "Voice automation stopped. Please start it again."}
    try:
        res = client.command(session_id, transcript, allow_autostart=False)
    except CommandTimeoutError:
        # ONE command failed; the session stays active for the next command
        return {"status": "ERROR", "reply": "That took too long, so I stopped waiting. Voice automation is still on."}
    except (SessionEndedError, ServiceUnavailableError):
        return session_ended
    except AuthenticationError:
        return {"status": "ERROR", "reply": "Voice Automation authentication failed."}
    except VoiceAutomationClientError as e:
        return {"status": "ERROR", "reply": f"Voice Automation error: {e}"}
    except Exception:
        return {"status": "ERROR", "reply": "Voice Automation command execution failed."}

    voice_pipeline.log_voice_command(
        DB_PATH,
        transcript,
        {"voice_automation_ipc": res},
        {"session_id": session_id},
    )

    status = res.get("status")
    outcome = res.get("action_outcome")
    ask_reason = res.get("ask_reason")

    if status == "ASKING" or (ask_reason and outcome != "EXECUTED"):
        return {"status": "ASKING", "reply": ask_reason or "Which option do you mean?"}

    if status == "OK" or outcome == "EXECUTED":
        return {"status": "EXECUTED", "reply": None}

    if res.get("session_failed") or res.get("error") in ("INVALID_SESSION", "SESSION_FAILED"):
        return session_ended
    err = res.get("error") or "Computer control action failed."
    if err.startswith("COMMAND_FAILED"):
        err = "That command failed, so I did nothing more. Voice automation is still on."
    return {"status": "ERROR", "reply": err}


@app.post("/voice/automation/start")
def voice_automation_start_endpoint(req: VoiceAutomationRequest):
    req.action = "start"
    return voice_automation_endpoint(req)


@app.post("/voice/automation/stop")
def voice_automation_stop_endpoint(req: VoiceAutomationRequest):
    req.action = "stop"
    return voice_automation_endpoint(req)


class VoiceEnrollRequest(BaseModel):
    audio_wav_base64: str | None = Field(None, description="Local 16 kHz mono WAV reference sample, base64")
    replace_existing: bool = Field(False, description="Must be true (after an explicit UI confirmation) to overwrite an existing enrollment")


@app.get("/voice/enrollment")
def voice_enrollment_status_endpoint():
    """
    Whether a reference voice is enrolled and whether local verification is available.
    """
    from services import speaker_verification
    return speaker_verification.enrollment_status()


@app.post("/voice/enroll")
def voice_enroll_endpoint(req: VoiceEnrollRequest):
    """
    Record the local reference speaker embedding (04_Backend_Schema.md Section 5).
    Re-enrollment requires replace_existing = true, sent only after a click confirmation.
    Returns status flags only - never the embedding.
    """
    from services import speaker_verification as sv
    try:
        return sv.enroll(req.audio_wav_base64, replace_existing=req.replace_existing)
    except sv.SpeakerVerificationError as e:
        code = str(e)
        http_status = {
            "REENROLLMENT_CONFIRMATION_REQUIRED": status.HTTP_409_CONFLICT,
            sv.NO_VOICE_SAMPLE: status.HTTP_400_BAD_REQUEST,
            sv.INVALID_AUDIO: status.HTTP_400_BAD_REQUEST,
            sv.VERIFICATION_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        }.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(status_code=http_status, detail=code)


class ToolConfirmRequest(BaseModel):
    proposal_token: str = Field(..., min_length=1, max_length=64)
    confirmed: bool


@app.post("/tools/confirm")
def tool_confirm_endpoint(req: ToolConfirmRequest):
    """
    Confirm or cancel a pending tool proposal (Propose -> Confirm -> Execute).
    """
    from services import tool_router
    active_tone = tone.get_tone_preference(DB_PATH)
    return tool_router.confirm_tool_proposal(DB_PATH, req.proposal_token, req.confirmed, tone=active_tone, conversation=LAST_CHAT_CONTEXT)


class ComputerGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(..., min_length=1, max_length=500)
    goal_id: Optional[str] = Field(None, max_length=64)
    metadata: Optional[Dict[str, Any]] = None


class ComputerGoalResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., min_length=1, max_length=64)
    user_response: str = Field(..., min_length=1, max_length=500)


class ComputerGoalCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., min_length=1, max_length=64)


@app.post("/computer/goals")
def computer_goal_endpoint(req: ComputerGoalRequest):
    """
    Submits a text computer-control goal (Phase 7B / Phase 8).
    Delegates to ChatGoalAdapter -> GoalExecutionOrchestrator(source="chat").
    """
    from services.computer_control_router import get_runtime
    from services.computer_control.container import get_container, initialize_production_container

    container = get_container()
    if not container:
        runtime = get_runtime()
        worker = runtime.worker if (runtime and hasattr(runtime, "worker")) else None
        container = initialize_production_container(worker=worker)

    if not container.worker:
        return {
            "goal_id": req.goal_id or "",
            "status": "UNAVAILABLE",
            "reason": "Computer control runtime is unavailable",
            "turn_count": 0,
            "clarification_count": 0,
            "elapsed_time_s": 0.0
        }

    res = container.chat_adapter.process_chat_input(req.goal, goal_id=req.goal_id, metadata=req.metadata)
    return res.model_dump()


@app.post("/computer/goals/resume")
def computer_goal_resume_endpoint(req: ComputerGoalResumeRequest):
    """
    Resumes an active computer-control goal waiting for user clarification (Phase 7B / Phase 8).
    Delegates to ChatGoalAdapter.resume_clarification -> GoalExecutionOrchestrator.
    """
    from services.computer_control_router import get_runtime
    from services.computer_control.container import get_container, initialize_production_container

    container = get_container()
    if not container:
        runtime = get_runtime()
        worker = runtime.worker if (runtime and hasattr(runtime, "worker")) else None
        container = initialize_production_container(worker=worker)

    if not container.worker:
        return {
            "goal_id": req.goal_id or "",
            "status": "UNAVAILABLE",
            "reason": "Computer control runtime is unavailable",
            "turn_count": 0,
            "clarification_count": 0,
            "elapsed_time_s": 0.0
        }

    res = container.chat_adapter.resume_clarification(req.goal_id, req.user_response)
    return res.model_dump()


@app.post("/computer/goals/cancel")
def computer_goal_cancel_endpoint(req: ComputerGoalCancelRequest):
    """
    Cancels an active computer-control goal (Phase 7B / Phase 8).
    Delegates to ChatGoalAdapter.cancel_goal -> GoalExecutionOrchestrator.
    """
    from services.computer_control_router import get_runtime
    from services.computer_control.container import get_container, initialize_production_container

    container = get_container()
    if not container:
        runtime = get_runtime()
        worker = runtime.worker if (runtime and hasattr(runtime, "worker")) else None
        container = initialize_production_container(worker=worker)

    if not container.worker:
        return {
            "goal_id": req.goal_id or "",
            "status": "UNAVAILABLE",
            "reason": "Computer control runtime is unavailable",
            "turn_count": 0,
            "clarification_count": 0,
            "elapsed_time_s": 0.0
        }

    res = container.chat_adapter.cancel_goal(req.goal_id)
    return res.model_dump()


@app.post("/chat")
def chat_endpoint(req: ChatMessageRequest):
    """
    Conversational Evie Assistant endpoint (Table 5 of 04_Backend_Schema.md).
    Delegates to the shared tool-calling router core (services/tool_router.py), the same
    pipeline used by POST /voice/command.
    """
    global LAST_CHAT_CONTEXT
    msg_lower = req.message.lower().strip()
    active_tone = tone.get_tone_preference(DB_PATH)

    # Shared tool-calling router core (planner -> registry allow-check -> gates -> handler)
    from services import tool_router
    query_result = tool_router.route_message(req.message, channel="chat", db_path=DB_PATH, context=LAST_CHAT_CONTEXT, active_tone=active_tone)
    if query_result and "reply" in query_result:
        return query_result

    # 1. Greeting / Wake path
    greetings = ("good morning", "good day", "hey evie", "hello evie", "hi evie", "wake")
    if any(g in msg_lower for g in greetings):
        b_report = generate_briefing(DB_PATH, trigger="wake")
        reply = f"Good morning! Here is your Evie Daily Briefing ({b_report['timestamp'][:10]}):\n"
        sec = b_report.get("sections", {}).get("security_findings", {})
        reply += f"• Security Score: {sec['score']}/100 (Grade: {sec['grade']})\n"
        reply += f"• Security Findings: {sec.get('open_secrets', 0)} secrets, {sec.get('public_exposures', 0)} exposures, {sec.get('unacknowledged_breaches', 0)} breaches.\n"
        cal = b_report.get("sections", {}).get("calendar_today", {})
        reply += f"• Calendar Today: {cal.get('event_count', 0)} event(s).\n"
        gh = b_report.get("sections", {}).get("github_activity", {})
        reply += f"• Overnight GitHub Activity: {gh.get('count', 0)} event(s)."
        return {"reply": reply, "briefing": b_report, "tone": active_tone}

    # 2. On-demand Report / Briefing path
    if any(phrase in msg_lower for phrase in ("report", "summary", "briefing")) and not any(p in msg_lower for p in ("gmail", "email", "inbox", "commit", "github")):
        b_report = generate_briefing(DB_PATH, trigger="on_demand")
        sec = b_report.get("sections", {}).get("security_findings", {})
        gh = b_report.get("sections", {}).get("github_intelligence", {})
        gm = b_report.get("sections", {}).get("gmail_intelligence", {})

        reply = f"Here is your current Evie Status Report ({b_report['timestamp'][:10]}):\n\n"
        reply += f"📊 GitHub Intelligence:\n"
        reply += f"• Repositories Synchronized: {gh.get('total_repositories', 0)}\n"
        reply += f"• Commit Activity: {gh.get('commits_yesterday', 0)} yesterday, {gh.get('commits_last_month', 0)} last month, {gh.get('commits_this_year', 0)} this year\n"
        reply += f"• Open PRs: {gh.get('unreviewed_prs_count', 0)} waiting for review\n"
        reply += f"• Secret Candidates: {gh.get('open_secrets_count', 0)} high-entropy candidate(s)\n"
        if gh.get("recent_activity"):
            act = gh["recent_activity"][0]
            reply += f"• Latest Activity: [{act.get('repo_name')}] {act.get('summary')}\n"

        reply += f"\n📧 Gmail Intelligence:\n"
        reply += f"• Phishing Alerts: {gm.get('phishing_alert_count', 0)}\n"
        reply += f"• Promotional/Newsletters: {gm.get('promotional_count', 0)}\n"
        reply += f"• Important Emails: {gm.get('important_count', 0)}\n"
        if gm.get("suspicious_emails"):
            ph = gm["suspicious_emails"][0]
            reply += f"• Top Alert: From {ph.get('sender')} ('{ph.get('subject')}')\n"

        reply += f"\n🛡️ Security & Exposure Findings:\n"
        reply += f"• Security Score: {sec['score']}/100 (Grade: {sec['grade']})\n"
        reply += f"• Public Exposures: {sec.get('public_exposures', 0)}\n"
        reply += f"• Unacknowledged Breaches: {sec.get('unacknowledged_breaches', 0)}"

        return {"reply": reply, "briefing": b_report, "tone": active_tone}

    # 2b. Cross-Source Recent Changes (GitHub + Gmail)
    if any(p in msg_lower for p in ("across my github and gmail", "across github and gmail", "both github and gmail", "github and gmail", "gmail and github")) or (("changed recently" in msg_lower or "recent changes" in msg_lower) and ("github" in msg_lower or "gmail" in msg_lower)):
        from services.github_intelligence import check_github_sync_status as check_gh_status, get_repository_analytics, get_recent_activity
        from services.gmail_intelligence import check_gmail_sync_status as check_gm_status, get_inbox_intelligence_summary, get_suspicious_emails

        gh_status = check_gh_status(DB_PATH)
        gm_status = check_gm_status(DB_PATH)

        reply = "Recent Activity Across GitHub & Gmail:\n\n"

        if gh_status == "unconfigured":
            reply += "📊 GitHub: Integration unconfigured or not synchronized yet.\n"
        else:
            gh_analytics = get_repository_analytics(DB_PATH)
            gh_act = get_recent_activity(DB_PATH, days=1, strict_date=False)
            reply += f"📊 GitHub Intelligence:\n"
            reply += f"• Synchronized Repositories: {gh_analytics.get('total_repositories', 0)}\n"
            reply += f"• Commit Activity: {gh_analytics.get('commits_yesterday', 0)} yesterday, {gh_analytics.get('commits_last_month', 0)} last month\n"
            reply += f"• Open PRs: {gh_analytics.get('unreviewed_prs_count', 0)} waiting for review\n"
            if gh_act:
                reply += f"• Latest Activity: [{gh_act[0].get('repo_name')}] {gh_act[0].get('summary')}\n"

        reply += "\n"

        if gm_status == "unconfigured":
            reply += "📧 Gmail: Integration unconfigured or not synchronized yet."
        else:
            gm_summary = get_inbox_intelligence_summary(DB_PATH)
            gm_susp = get_suspicious_emails(DB_PATH)
            reply += f"📧 Gmail Intelligence:\n"
            reply += f"• Phishing Alerts: {gm_summary.get('phishing_alert_count', 0)}\n"
            reply += f"• Promotional/Newsletters: {gm_summary.get('promotional_count', 0)}\n"
            reply += f"• Important Emails: {gm_summary.get('important_count', 0)}"
            if gm_susp:
                reply += f"\n• Top Alert: From {gm_susp[0].get('sender')} ('{gm_susp[0].get('subject')}')"

        return {"reply": reply, "tone": active_tone}

    # 3. Contextual Follow-up Path ("tell them", "show me those", "which ones?", "give me more details", "tell me more about those", "show me more")
    followup_phrases = (
        "tell them", "show me those", "which ones", "give me more details",
        "tell me more about those", "tell me more", "show details", "details on those",
        "show me more", "show more", "next page", "more details"
    )
    if any(fp in msg_lower for fp in followup_phrases) and LAST_CHAT_CONTEXT.get("topic"):
        topic = LAST_CHAT_CONTEXT["topic"]
        data = LAST_CHAT_CONTEXT.get("data")

        if topic == "suspicious_emails":
            if isinstance(data, list) and data:
                items = [f"• [{str(m.get('risk_score')).upper()}] From: {m.get('sender')} | Subject: '{m.get('subject')}' | Received: {str(m.get('received_at'))[:10]}" for m in data]
                reply = f"Details on suspicious email alerts ({len(data)} total):\n" + "\n".join(items)
            else:
                reply = "No suspicious emails recorded to detail."
            return {"reply": reply, "suspicious_emails": data, "tone": active_tone}

        if topic in ("promotional_emails", "gmail_summary"):
            if "tell" in msg_lower or "unsubscribe" in msg_lower:
                from services.gmail_intelligence import propose_gmail_cleanup_action
                proposals = []
                domains = set()
                if isinstance(data, list):
                    for item in data:
                        d = item.get("sender_domain")
                        if d: domains.add(d)
                elif isinstance(data, dict):
                    for s in data.get("top_promotional_senders", []):
                        if s.get("domain"): domains.add(s["domain"])

                if domains:
                    for dom in sorted(domains):
                        prop = propose_gmail_cleanup_action(DB_PATH, dom, action_type="unsubscribe_recommendation")
                        proposals.append(prop)
                    prop_list = [f"• Proposal '{p['proposal_token'][:8]}...': Unsubscribe from {p['sender_domain']}" for p in proposals]
                    reply = f"Created {len(proposals)} unsubscribe proposal(s):\n" + "\n".join(prop_list) + "\n\nPlease confirm execution via POST /gmail/confirm_cleanup."
                else:
                    reply = "No promotional sender domains available to generate unsubscribe proposals."
                return {"reply": reply, "proposals": proposals, "tone": active_tone}
            else:
                if isinstance(data, list) and data:
                    items = [f"• From: {m.get('sender')} | Subject: '{m.get('subject')}' | Unsubscribe: {'Available' if m.get('has_unsubscribe') else 'N/A'}" for m in data[:10]]
                    reply = f"Promotional email details ({len(data)} total):\n" + "\n".join(items)
                elif isinstance(data, dict) and data.get("top_promotional_senders"):
                    items = [f"• {s['domain']} ({s['count']} email(s))" for s in data["top_promotional_senders"]]
                    reply = f"Top promotional senders:\n" + "\n".join(items)
                else:
                    reply = "No promotional emails recorded to detail."
                return {"reply": reply, "promotional_emails": data, "tone": active_tone}

        if topic == "secret_findings":
            if isinstance(data, list) and data:
                page = LAST_CHAT_CONTEXT.get("page", 1) + 1
                page_size = LAST_CHAT_CONTEXT.get("page_size", 5)
                start_idx = (page - 1) * page_size
                end_idx = start_idx + page_size
                page_items = data[start_idx:end_idx]

                if page_items:
                    items = [f"• Repo: {f.get('repo_name')} | Commit: {str(f.get('commit_sha'))[:7]} | Pattern: {f.get('secret_type') or f.get('pattern_type')} | File: {f.get('filename') or f.get('file_path')} | Preview: {f.get('redacted_value') or f.get('redacted_preview')}" for f in page_items]
                    reply = f"Secret Candidates (showing {start_idx + 1}-{min(end_idx, len(data))} of {len(data)} total):\n" + "\n".join(items)
                    if end_idx < len(data):
                        reply += f"\n\nThere are {len(data) - end_idx} more candidate findings. Say 'show me more' to view the next page."
                    LAST_CHAT_CONTEXT["page"] = page
                else:
                    reply = f"All {len(data)} secret candidates have been shown."
            else:
                reply = "No open secret candidate findings to detail."
            return {"reply": reply, "secret_findings": data, "tone": active_tone}

        if topic == "unreviewed_prs":
            if isinstance(data, list) and data:
                items = [f"• Repo: {pr.get('repo_name')} | PR #{pr.get('number')}: '{pr.get('title')}' by {pr.get('author')} ({pr.get('html_url')})" for pr in data]
                reply = f"Open unreviewed PRs details:\n" + "\n".join(items)
            else:
                reply = "No open PRs to detail."
            return {"reply": reply, "unreviewed_prs": data, "tone": active_tone}

        if topic == "github_activity":
            if isinstance(data, list) and data:
                items = [f"• [{a.get('repo_name')}] {a.get('summary')} by {a.get('author')} ({str(a.get('timestamp', ''))[:10]})" for a in data]
                reply = f"GitHub activity details:\n" + "\n".join(items)
            else:
                reply = "No recent GitHub activity to detail."
            return {"reply": reply, "github_activity": data, "tone": active_tone}

    # 4. Security Score / Findings path
    if any(phrase in msg_lower for phrase in ("score", "finding", "alert", "security health")) and not any(p in msg_lower for p in ("email", "phishing", "gmail", "secret", "exposed")):
        import security_score
        score_data = security_score.get_current_security_score(DB_PATH)
        current_score = score_data["score"]
        grade = score_data["grade"]
        active_count = score_data.get("active_findings_count", 0)
        reply = f"Your current Security Health Score is {current_score}/100 (Grade: {grade}, Active Findings: {active_count}, Trend: {score_data.get('trend', 'stable')})."
        if score_data.get("note"):
            reply += f"\nNote: {score_data['note']}"
        return {"reply": reply, "score": score_data, "tone": active_tone}

    # 5. Top Promotional Sender Query Path
    if any(phrase in msg_lower for phrase in ("most promotional", "top promotional sender", "sending me the most promotional", "who sends the most promotional", "who sends the most newsletters", "highest promotional")):
        from services.gmail_intelligence import check_gmail_sync_status, get_top_promotional_senders
        sync_status = check_gmail_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "Gmail integration has not been configured or synchronized yet. Please authorize Gmail access and trigger an integration sync to inspect promotional senders."
            return {"reply": reply, "top_senders": [], "tone": active_tone}

        top_senders = get_top_promotional_senders(DB_PATH, limit=5)
        LAST_CHAT_CONTEXT = {"topic": "promotional_emails", "data": top_senders}

        if top_senders:
            top_cnt = top_senders[0]["count"]
            tied = [s["domain"] for s in top_senders if s["count"] == top_cnt]
            if len(tied) == 1:
                reply = f"The sender sending you the most promotional emails is '{top_senders[0]['domain']}' with {top_cnt} promotional email(s)."
            else:
                reply = f"The top promotional senders tied with {top_cnt} email(s) each are: {', '.join(tied)}."
        else:
            reply = "No promotional emails or newsletter subscriptions found in synchronized inbox."
        return {"reply": reply, "top_senders": top_senders, "tone": active_tone}

    # 5b. Gmail Promotional / Newsletters / Unsubscribe Query path (Evaluated BEFORE PRs)
    if any(phrase in msg_lower for phrase in ("newsletter", "newsletters", "promotional", "promotions", "unsubscribe")):
        from services.gmail_intelligence import check_gmail_sync_status, get_promotional_emails, get_inbox_intelligence_summary
        sync_status = check_gmail_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "Gmail integration has not been configured or synchronized yet. Please authorize Gmail access and trigger an integration sync to inspect newsletters."
            return {"reply": reply, "promotional_emails": [], "tone": active_tone}

        promo = get_promotional_emails(DB_PATH)
        gmail_summary = get_inbox_intelligence_summary(DB_PATH)
        LAST_CHAT_CONTEXT = {"topic": "promotional_emails", "data": promo or gmail_summary.get("top_promotional_senders", [])}

        if promo:
            items = [f"• From: {m['sender']} | Subject: '{m['subject']}' | Unsubscribe: {'Available' if m['has_unsubscribe'] else 'N/A'}" for m in promo[:10]]
            reply = f"Inbox promotional email intelligence ({len(promo)} promotional email(s) found):\n" + "\n".join(items)
        elif gmail_summary.get("top_promotional_senders"):
            senders = gmail_summary["top_promotional_senders"]
            s_list = [f"• {s['domain']} ({s['count']} email(s))" for s in senders]
            reply = f"Top promotional senders ({gmail_summary.get('promotional_count', 0)} total promotional emails):\n" + "\n".join(s_list)
        else:
            reply = "No promotional emails or newsletter subscriptions found in synchronized inbox."
        return {"reply": reply, "promotional_emails": promo, "gmail_summary": gmail_summary, "tone": active_tone}

    # 5c. Gmail Suspicious / Phishing Emails Query path
    if any(phrase in msg_lower for phrase in ("suspicious email", "suspicious emails", "phishing", "phishing alert", "phishing email", "suspicious mail")):
        from services.gmail_intelligence import check_gmail_sync_status, get_suspicious_emails
        sync_status = check_gmail_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "Gmail integration has not been configured or synchronized yet. Please authorize Gmail access and trigger an integration sync to scan for phishing alerts."
            return {"reply": reply, "suspicious_emails": [], "tone": active_tone}

        suspicious = get_suspicious_emails(DB_PATH)
        LAST_CHAT_CONTEXT = {"topic": "suspicious_emails", "data": suspicious}
        if suspicious:
            items = [f"• [{str(s['risk_score']).upper()}] From: {s['sender']} | Subject: '{s['subject']}'" for s in suspicious]
            reply = f"Found {len(suspicious)} suspicious email alert(s):\n" + "\n".join(items)
        else:
            reply = "No suspicious emails or phishing alerts detected in synchronized inbox."
        return {"reply": reply, "suspicious_emails": suspicious, "tone": active_tone}

    # 5d. Gmail Summary Query path
    if any(phrase in msg_lower for phrase in ("gmail summary", "inbox summary", "email summary", "gmail status", "email status")) or ("email" in msg_lower or "gmail" in msg_lower):
        from services.gmail_intelligence import check_gmail_sync_status, get_inbox_intelligence_summary
        sync_status = check_gmail_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "Gmail integration has not been configured or synchronized yet. Please authorize Gmail access and trigger an integration sync to access inbox summary."
            return {"reply": reply, "gmail_summary": {}, "tone": active_tone}

        gmail_summary = get_inbox_intelligence_summary(DB_PATH)
        LAST_CHAT_CONTEXT = {"topic": "gmail_summary", "data": gmail_summary}
        reply = f"Gmail Inbox Intelligence Summary:\n• Important Emails: {gmail_summary.get('important_count', 0)}\n• Phishing Alerts: {gmail_summary.get('phishing_alert_count', 0)}\n• Promotional/Newsletters: {gmail_summary.get('promotional_count', 0)}"
        return {"reply": reply, "gmail_summary": gmail_summary, "tone": active_tone}

    # 6c1. Secret candidates by repository aggregation path
    if any(phrase in msg_lower for phrase in ("most secret candidates", "repositories have the most secret", "repos with most secrets", "secret candidates by repository", "secret candidates per repo")):
        from services.github_intelligence import check_github_sync_status, get_secret_findings_by_repository
        sync_status = check_github_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "GitHub data has not been synchronized or configured yet. Please configure a GitHub access token in your OS keyring and trigger an integration sync to scan for secret leaks."
            return {"reply": reply, "repo_secrets": [], "tone": active_tone}

        repo_secrets = get_secret_findings_by_repository(DB_PATH)
        if repo_secrets:
            items = [f"• {r['repo_name']}: {r['candidate_count']} high-entropy secret candidate(s)" for r in repo_secrets]
            reply = f"Repository Secret Candidates Breakdown ({len(repo_secrets)} repository(ies) affected):\n" + "\n".join(items)
        else:
            reply = "No open secret candidates detected in monitored repositories."
        return {"reply": reply, "repo_secrets": repo_secrets, "tone": active_tone}

    # 6c2. Secret exposure query path (Bounded pagination to 5)
    if any(phrase in msg_lower for phrase in ("secret", "secrets", "exposed", "api key", "leak")):
        from services.github_intelligence import check_github_sync_status, get_secret_findings
        sync_status = check_github_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "GitHub data has not been synchronized or configured yet. Please configure a GitHub access token in your OS keyring and trigger an integration sync to scan for secret leaks."
            return {"reply": reply, "secret_findings": [], "tone": active_tone}

        findings = get_secret_findings(DB_PATH)
        page_size = 5
        LAST_CHAT_CONTEXT = {"topic": "secret_findings", "data": findings, "page": 1, "page_size": page_size}
        if findings:
            first_page = findings[:page_size]
            finding_list = [f"• Repo: {f['repo_name']} (commit {str(f['commit_sha'])[:7]}): {f.get('secret_type') or f.get('pattern_type')} ({f.get('redacted_value') or f.get('redacted_preview')}) in {f.get('filename') or f.get('file_path')}" for f in first_page]
            reply = f"Alert: Detected {len(findings)} high-entropy secret candidate(s) across monitored repositories.\n\nShowing candidates 1-{min(page_size, len(findings))}:\n" + "\n".join(finding_list)
            if len(findings) > page_size:
                reply += f"\n\nThere are {len(findings) - page_size} additional secret candidates. Say 'show me more' to view the next page."
        else:
            reply = "No exposed secret candidates or API keys have been detected in monitored repositories."
        return {"reply": reply, "secret_findings": findings, "tone": active_tone}

    # 6. GitHub Repositories Query path
    if any(phrase in msg_lower for phrase in ("repositories", "repository", "repos", "repo list", "what repositories")) and not any(s in msg_lower for s in ("secret", "secrets")):
        from services.github_intelligence import check_github_sync_status, get_repository_analytics
        sync_status = check_github_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "GitHub data has not been synchronized or configured yet. Please configure a GitHub access token in your OS keyring and trigger an integration sync to discover repositories."
            return {"reply": reply, "analytics": {}, "tone": active_tone}

        analytics = get_repository_analytics(DB_PATH)
        repos = analytics.get("repositories", [])
        if repos:
            repo_names = [f"• {r['full_name']} ({'private' if r.get('is_private') else 'public'})" for r in repos]
            reply = f"You have {len(repos)} synchronized repository(ies):\n" + "\n".join(repo_names)
        else:
            reply = "0 synchronized repositories found."
        return {"reply": reply, "analytics": analytics, "tone": active_tone}

    # 6b. PRs waiting for review query path (Strict word boundary match)
    if (re.search(r'\bprs?\b', msg_lower) or any(phrase in msg_lower for phrase in ("pull request", "unreviewed", "waiting for review", "code review"))) and not ("propose" in msg_lower and "event" in msg_lower):
        from services.github_intelligence import check_github_sync_status, get_unreviewed_prs
        sync_status = check_github_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "GitHub data has not been synchronized or configured yet. Please configure a GitHub access token in your OS keyring and trigger an integration sync to inspect pull requests."
            return {"reply": reply, "unreviewed_prs": [], "tone": active_tone}

        unreviewed = get_unreviewed_prs(DB_PATH)
        LAST_CHAT_CONTEXT = {"topic": "unreviewed_prs", "data": unreviewed}
        if unreviewed:
            pr_list = [f"• {pr['repo_name']} PR #{pr['number']}: '{pr['title']}' by {pr['author']}" for pr in unreviewed]
            reply = f"There are {len(unreviewed)} open PR(s) waiting for review:\n" + "\n".join(pr_list)
        else:
            reply = "No open pull requests are currently waiting for review."
        return {"reply": reply, "unreviewed_prs": unreviewed, "tone": active_tone}

    # 6d. GitHub Activity & Commit Analytics Query path
    if any(phrase in msg_lower for phrase in ("github", "commit", "commits", "commits last month", "commits this year", "yesterday")):
        from services.github_intelligence import check_github_sync_status, get_repository_analytics, get_yesterday_activity, get_recent_activity
        sync_status = check_github_sync_status(DB_PATH)
        if sync_status == "unconfigured":
            reply = "GitHub data has not been synchronized or configured yet. Please configure a GitHub access token in your OS keyring and trigger an integration sync to track commit activity."
            return {"reply": reply, "analytics": {}, "tone": active_tone}

        analytics = get_repository_analytics(DB_PATH)
        activity = get_recent_activity(DB_PATH, days=1)
        LAST_CHAT_CONTEXT = {"topic": "github_activity", "data": activity}

        if "yesterday" in msg_lower:
            y_act = get_yesterday_activity(DB_PATH)
            count = y_act.get("commit_count", 0)
            items = y_act.get("items", [])
            if items:
                act_lines = [f"• [{a['repo_name']}] {a['summary']} by {a['author']}" for a in items[:5]]
                reply = f"Yesterday's GitHub Activity ({count} commit(s)):\n" + "\n".join(act_lines)
            else:
                reply = "0 commit(s) recorded yesterday across synchronized repositories."
        elif "last month" in msg_lower:
            reply = f"You made {analytics.get('commits_last_month', 0)} commit(s) last month across synchronized repositories."
        elif "this year" in msg_lower or "year" in msg_lower:
            reply = f"You made {analytics.get('commits_this_year', 0)} commit(s) this year across synchronized repositories."
        else:
            reply = f"Commit Activity Summary: {analytics.get('commits_yesterday', 0)} yesterday, {analytics.get('commits_last_month', 0)} last month, {analytics.get('commits_this_year', 0)} this year ({analytics.get('commits_total', 0)} total)."
        return {"reply": reply, "analytics": analytics, "recent_activity": activity, "tone": active_tone}

    # 7. Calendar Action Proposal path
    if "propose" in msg_lower and "event" in msg_lower:
        from integrations.calendar_actions import propose_calendar_action
        from datetime import datetime, timedelta
        now = datetime.now()
        start = (now + timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1, hours=1)).isoformat()
        prop = propose_calendar_action(
            db_path=DB_PATH,
            action_type="create",
            summary="Proposed Event",
            start_time=start,
            end_time=end,
            description="Created via Evie chat",
        )
        return {"reply": prop["prompt"], "proposal": prop, "tone": active_tone}

    # 8. Memory Recall path
    if any(phrase in msg_lower for phrase in ("recall", "memory", "doing", "journal", "diary")):
        import memory
        results = memory.search_journal_entries(query=req.message, db_path=DB_PATH)
        if results.get("results"):
            top = results["results"][0]
            reply = f"Found journal entry from {top['entry_date']}: {top['content']}"
        else:
            reply = "No matching journal memory entries found."
        return {"reply": reply, "memory_results": results, "tone": active_tone}

    # 9. Default: DRS Evidence-Based Research
    res = drs.ask_drs_question(req.message, db_path=DB_PATH)
    reply = res.get("summary", "")
    if res.get("disclaimer"):
        reply += f"\n\n*{res['disclaimer']}*"
    return {"reply": reply, "drs": res, "sources": res.get("sources", []), "tone": active_tone}


if __name__ == "__main__":
    import uvicorn
    # Local-only network boundary: bind exclusively to 127.0.0.1 (never 0.0.0.0)
    uvicorn.run(app, host="127.0.0.1", port=8000)

