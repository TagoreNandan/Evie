"""
Confirmation-Gated Calendar Actions for Evie Assistant.

Implements CAL-01, CAL-02 & Section 6 of 02_TRD.md:
Enforces mandatory two-step protocol for calendar event scheduling:
1. /calendar/propose: Validates input, creates a proposal record in SQLite (confirmed_by_user = 0, status = 'proposed'),
   returns a unique proposal token and user prompt. Zero Google Calendar API calls are made.
2. /calendar/confirm: Requires explicit user confirmation (confirmed = True), proposal TTL validation (15 min),
   and an atomic SQLite claim (status = 'claimed') before executing Google Calendar REST API v3 mutations.
"""

import uuid
import sqlite3
from datetime import datetime, timezone
import httpx

import keyring_utils
import redaction

PROPOSAL_TTL_SECONDS = 900  # 15 minutes


def init_calendar_actions_table(db_path: str = "evie.db") -> None:
    """
    Ensure calendar_actions table exists in SQLite per 04_Backend_Schema.md.
    """
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
        conn.commit()


def propose_calendar_action(
    db_path: str,
    action_type: str,
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    event_id: str | None = None,
) -> dict:
    """
    Propose a calendar creation, update, or deletion (CAL-01).
    Performs ZERO external HTTP / API calls.
    """
    init_calendar_actions_table(db_path)

    # 1. Input Validation
    action_type = str(action_type).lower().strip()
    if action_type not in ("create", "update", "delete"):
        raise ValueError(f"Invalid action_type '{action_type}'. Must be 'create', 'update', or 'delete'.")

    if not summary or len(summary.strip()) == 0:
        raise ValueError("Summary must be a non-empty string.")
    if len(summary) > 255:
        raise ValueError("Summary exceeds maximum length of 255 characters.")
    if description and len(description) > 2000:
        raise ValueError("Description exceeds maximum length of 2000 characters.")

    try:
        dt_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        dt_end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except Exception as e:
        raise ValueError(f"Invalid ISO-8601 timestamp format: {e}") from e

    if action_type in ("create", "update") and dt_end <= dt_start:
        raise ValueError("end_time must be strictly after start_time.")

    if action_type in ("update", "delete") and not event_id:
        raise ValueError(f"event_id is required for calendar action '{action_type}'.")

    # 2. Token Generation & DB Persistence
    proposal_token = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO calendar_actions (proposal_token, action_type, event_summary, start_time, end_time, description, event_id, confirmed_by_user, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'proposed', ?)
            """,
            (proposal_token, action_type, summary, start_time, end_time, description, event_id, now_iso),
        )
        conn.commit()

    prompt = (
        f"Are you sure you want to {action_type} event '{summary}' "
        f"from {start_time} to {end_time}? Reply confirm to proceed."
    )

    return {
        "proposal_token": proposal_token,
        "status": "proposed",
        "prompt": prompt,
        "proposal_details": {
            "action_type": action_type,
            "summary": summary,
            "start_time": start_time,
            "end_time": end_time,
            "description": description,
            "event_id": event_id,
        },
    }


def confirm_calendar_action(db_path: str, proposal_token: str, confirmed: bool) -> dict:
    """
    Confirm and execute a proposed calendar action (CAL-02).
    Requires explicit confirmed = True, valid TTL (15 min), and atomic SQLite claim.
    Executes Google Calendar REST API v3 call only after successful claim.
    """
    init_calendar_actions_table(db_path)

    # 1. Proposal Lookup
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM calendar_actions WHERE proposal_token = ?", (proposal_token,))
        row = cur.fetchone()

    if not row:
        raise ValueError("Invalid or non-existent proposal token.")

    current_status = row["status"]
    if current_status != "proposed":
        raise ValueError(f"Proposal token is already in state '{current_status}'.")

    # 2. TTL Expiration Check
    created_at_str = row["created_at"]
    try:
        dt_created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        if dt_created.tzinfo is None:
            dt_created = dt_created.replace(tzinfo=timezone.utc)
    except Exception:
        dt_created = datetime.now(timezone.utc)

    now_utc = datetime.now(timezone.utc)
    age_seconds = (now_utc - dt_created).total_seconds()

    if age_seconds > PROPOSAL_TTL_SECONDS:
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE calendar_actions SET status = 'expired' WHERE proposal_token = ?", (proposal_token,))
            conn.commit()
        return {"status": "expired", "message": "Proposal expired. Please request a new proposal."}

    # 3. User Cancellation Check
    if not confirmed:
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE calendar_actions SET status = 'cancelled' WHERE proposal_token = ?", (proposal_token,))
            conn.commit()
        return {"status": "cancelled", "message": "Calendar proposal was cancelled."}

    # 4. Atomic SQLite Claim (proposed -> claimed)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE calendar_actions
            SET status = 'claimed', confirmed_by_user = 1
            WHERE proposal_token = ? AND status = 'proposed'
            """,
            (proposal_token,),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("Proposal token already claimed or processed.")

        conn.execute("UPDATE calendar_actions SET status = 'executing' WHERE proposal_token = ?", (proposal_token,))
        conn.commit()

    # 5. Credential Retrieval & Secrecy
    oauth_token = keyring_utils.get_credential("evie_assistant", "google_calendar_oauth")
    if not oauth_token:
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE calendar_actions SET status = 'execution_failed' WHERE proposal_token = ?", (proposal_token,))
            conn.commit()
        raise RuntimeError("CREDENTIAL_MISSING: Google Calendar OAuth credentials not found in OS keyring")

    # 6. Execute Google Calendar API Mutation
    action_type = row["action_type"]
    summary = row["event_summary"]
    start_time = row["start_time"]
    end_time = row["end_time"]
    description = row["description"] or ""
    event_id = row["event_id"]

    headers = {"Authorization": f"Bearer {oauth_token}", "Content-Type": "application/json"}
    base_url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

    executed_event_id = event_id

    try:
        with httpx.Client(timeout=10.0) as client:
            if action_type == "create":
                payload = {
                    "summary": summary,
                    "description": description,
                    "start": {"dateTime": start_time},
                    "end": {"dateTime": end_time},
                }
                res = client.post(base_url, headers=headers, json=payload)
                res.raise_for_status()
                if isinstance(res.json(), dict):
                    executed_event_id = str(res.json().get("id") or event_id or "created_event")
                else:
                    executed_event_id = str(event_id or "created_event")
            elif action_type == "update":
                payload = {
                    "summary": summary,
                    "description": description,
                    "start": {"dateTime": start_time},
                    "end": {"dateTime": end_time},
                }
                url = f"{base_url}/{event_id}"
                res = client.put(url, headers=headers, json=payload)
                res.raise_for_status()
            elif action_type == "delete":
                url = f"{base_url}/{event_id}"
                res = client.delete(url, headers=headers)
                res.raise_for_status()
    except Exception as e:
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE calendar_actions SET status = 'execution_failed' WHERE proposal_token = ?", (proposal_token,))
            conn.commit()
        raise RuntimeError("API_ERROR: Google Calendar API mutation failed") from e

    # 7. Record Execution Success
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE calendar_actions
            SET status = 'executed', event_id = ?, executed_at = ?
            WHERE proposal_token = ?
            """,
            (executed_event_id, now_iso, proposal_token),
        )
        conn.commit()

    return {
        "status": "executed",
        "proposal_token": proposal_token,
        "action_type": action_type,
        "event_id": executed_event_id,
        "summary": summary,
    }
