"""
Gmail Intelligence & Cleanup Recommendation Service for Evie Assistant.

Implements MAIL-01:
Read-only inbox metadata intelligence, email categorization, sender frequency analytics,
and proposal-confirmation gated cleanup recommendations.
"""

import datetime
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from integrations.phishing_scan import (
    analyze_email_header,
    analyze_email_phishing,
    categorize_email_message,
    extract_domain,
    fetch_gmail_messages,
    fetch_gmail_incremental_messages,
)
from services.real_integrations import get_google_access_token


def init_gmail_tables(db_path: str) -> None:
    """
    Initialize SQLite schema for Gmail metadata cache, sender statistics, cleanup proposals,
    and incremental checkpoints.
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gmail_metadata (
                message_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_domain TEXT NOT NULL,
                category TEXT NOT NULL,      -- 'important', 'phishing_alert', 'newsletter_promotional', 'transactional'
                risk_score TEXT NOT NULL,    -- 'low', 'moderate', 'high', 'critical'
                phishing_confidence REAL,
                phishing_source TEXT,
                has_unsubscribe INTEGER DEFAULT 0,
                received_at TEXT NOT NULL
            )
        """)

        cursor.execute("PRAGMA table_info(gmail_metadata)")
        cols = [row[1] for row in cursor.fetchall()]
        if "phishing_confidence" not in cols:
            cursor.execute("ALTER TABLE gmail_metadata ADD COLUMN phishing_confidence REAL")
        if "phishing_source" not in cols:
            cursor.execute("ALTER TABLE gmail_metadata ADD COLUMN phishing_source TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gmail_cleanup_proposals (
                proposal_token TEXT PRIMARY KEY,
                sender_domain TEXT NOT NULL,
                action_type TEXT NOT NULL,   -- 'unsubscribe_recommendation', 'delete_newsletter'
                status TEXT NOT NULL,        -- 'pending', 'confirmed', 'rejected'
                created_at TEXT NOT NULL,
                executed_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gmail_sync_checkpoints (
                account_id TEXT PRIMARY KEY,
                last_history_id TEXT,
                last_synced_at TEXT NOT NULL,
                last_message_id TEXT
            )
        """)
        conn.commit()


def check_gmail_sync_status(db_path: str) -> str:
    """
    Distinguishes Gmail synchronization state:
    - 'unconfigured': if no Google token exists AND no gmail_metadata has been synchronized.
    - 'synced': if Google token exists or metadata exists in database.
    """
    init_gmail_tables(db_path)
    token = get_google_access_token()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM gmail_metadata")
        count = cursor.fetchone()[0]
    if not token and count == 0:
        return "unconfigured"
    return "synced"



def get_gmail_sync_checkpoint(db_path: str, account_id: str = "primary") -> Optional[Dict[str, Any]]:
    """
    Retrieve stored Gmail synchronization checkpoint for account.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT account_id, last_history_id, last_synced_at, last_message_id FROM gmail_sync_checkpoints WHERE account_id = ?", (account_id,))
        row = cursor.fetchone()
        if row:
            return {
                "account_id": row[0],
                "last_history_id": row[1],
                "last_synced_at": row[2],
                "last_message_id": row[3],
            }
    return None


def update_gmail_sync_checkpoint(db_path: str, account_id: str = "primary", history_id: Optional[str] = None, last_message_id: Optional[str] = None) -> None:
    """
    Persist updated incremental synchronization checkpoint for account.
    """
    init_gmail_tables(db_path)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO gmail_sync_checkpoints (account_id, last_history_id, last_synced_at, last_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                last_history_id = COALESCE(excluded.last_history_id, gmail_sync_checkpoints.last_history_id),
                last_synced_at = excluded.last_synced_at,
                last_message_id = COALESCE(excluded.last_message_id, gmail_sync_checkpoints.last_message_id)
            """,
            (account_id, str(history_id) if history_id is not None else None, now_iso, last_message_id),
        )
        conn.commit()


def sync_gmail_intelligence(db_path: str, access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch recent email headers via Gmail API using incremental history checkpoints,
    categorize emails, persist metadata, and return synchronization summary statistics.
    """
    init_gmail_tables(db_path)
    token = access_token or get_google_access_token()
    if not token:
        return {"synced_messages": 0, "status": "unconfigured"}

    checkpoint = get_gmail_sync_checkpoint(db_path, account_id="primary")
    start_history_id = checkpoint.get("last_history_id") if checkpoint else None

    result = fetch_gmail_incremental_messages(access_token=token, start_history_id=start_history_id, max_results=20)
    messages = result.get("messages", [])
    new_history_id = result.get("history_id") or (checkpoint.get("last_history_id") if checkpoint else None)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    synced_count = 0
    latest_msg_id = checkpoint.get("last_message_id") if checkpoint else None
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        existing_ids = set()
        cursor.execute("SELECT message_id FROM gmail_metadata")
        for row in cursor.fetchall():
            if row[0]:
                existing_ids.add(row[0])

        for msg in messages:
            msg_id = msg.get("id", str(uuid.uuid4()))
            latest_msg_id = msg_id

            if msg_id in existing_ids:
                synced_count += 1
                continue

            headers_dict = {}

            if isinstance(msg.get("headers"), list):
                for h in msg["headers"]:
                    if isinstance(h, dict) and "name" in h and "value" in h:
                        headers_dict[h["name"].lower()] = str(h["value"])
            else:
                payload = msg.get("payload", {})
                headers = payload.get("headers", []) if isinstance(payload, dict) else []

                if isinstance(headers, list):
                    for h in headers:
                        if isinstance(h, dict) and "name" in h and "value" in h:
                            headers_dict[h["name"].lower()] = str(h["value"])

            subject = msg.get("subject") or headers_dict.get("subject", "(No Subject)")
            sender = msg.get("sender") or headers_dict.get("from", "unknown")
            sender_domain = extract_domain(sender)

            has_unsub = 1 if (
                msg.get("list_unsubscribe")
                or "list-unsubscribe" in headers_dict
            ) else 0

            category = categorize_email_message(headers_dict)
            phishing_analysis = analyze_email_phishing(headers_dict)
            risk_score = phishing_analysis.get("risk_score", "low")
            phishing_confidence = phishing_analysis.get("phishing_confidence")
            phishing_source = phishing_analysis.get("phishing_source")

            cursor.execute(
                """
                INSERT OR REPLACE INTO gmail_metadata
                (message_id, subject, sender, sender_domain, category, risk_score, phishing_confidence, phishing_source, has_unsubscribe, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, subject, sender, sender_domain, category, risk_score, phishing_confidence, phishing_source, has_unsub, now_iso),
            )
            synced_count += 1

        conn.commit()

    update_gmail_sync_checkpoint(db_path, account_id="primary", history_id=new_history_id, last_message_id=latest_msg_id)
    return {"synced_messages": synced_count, "status": "synced"}


def get_inbox_intelligence_summary(db_path: str) -> Dict[str, Any]:
    """
    Retrieve inbox intelligence breakdown: counts by category, top high-frequency promotional senders,
    cleanup recommendations, and active pending proposals.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM gmail_metadata")
        total_emails = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM gmail_metadata WHERE category = 'important'")
        important_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM gmail_metadata WHERE category = 'phishing_alert' OR risk_score IN ('moderate', 'high', 'critical')")
        phishing_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM gmail_metadata WHERE category = 'newsletter_promotional'")
        promotional_count = cursor.fetchone()[0]

        # Top promotional senders frequency
        cursor.execute(
            """
            SELECT sender_domain, COUNT(*) as cnt
            FROM gmail_metadata
            WHERE category = 'newsletter_promotional' AND sender_domain != ''
            GROUP BY sender_domain
            ORDER BY cnt DESC
            LIMIT 5
            """
        )
        top_senders = [{"domain": row[0], "count": row[1]} for row in cursor.fetchall()]

        # Generate cleanup recommendations for senders with newsletter/promotional emails
        cleanup_recommendations = []
        for s in top_senders:
            cleanup_recommendations.append({
                "sender_domain": s["domain"],
                "email_count": s["count"],
                "recommendation": f"Unsubscribe from {s['domain']} (sent {s['count']} promotional emails)",
                "action_type": "unsubscribe_recommendation",
            })

        # Pending proposals
        cursor.execute(
            "SELECT proposal_token, sender_domain, action_type, status, created_at FROM gmail_cleanup_proposals WHERE status = 'pending'"
        )
        pending_proposals = [
            {
                "proposal_token": row[0],
                "sender_domain": row[1],
                "action_type": row[2],
                "status": row[3],
                "created_at": row[4],
            }
            for row in cursor.fetchall()
        ]

    return {
        "total_emails_scanned": total_emails,
        "important_count": important_count,
        "phishing_alert_count": phishing_count,
        "promotional_count": promotional_count,
        "top_promotional_senders": top_senders,
        "cleanup_recommendations": cleanup_recommendations,
        "pending_proposals": pending_proposals,
    }


CLEANUP_PROPOSAL_TTL_SECONDS = 900  # 15 minutes, same window as calendar proposals


def _cleanup_proposal_expired(created_at: str, now: datetime.datetime) -> bool:
    """True when a proposal created at `created_at` (UTC ISO) is past its 15-minute window."""
    try:
        created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError):
        return True  # unparseable timestamp: fail closed
    return (now - created).total_seconds() > CLEANUP_PROPOSAL_TTL_SECONDS


def propose_gmail_cleanup_action(db_path: str, sender_domain: str, action_type: str = "unsubscribe_recommendation") -> Dict[str, Any]:
    """
    Generate a new proposal token for email cleanup/unsubscribe action.
    This proposal requires explicit user confirmation via confirm_gmail_cleanup_action before execution,
    and expires 15 minutes after creation.
    """
    init_gmail_tables(db_path)
    proposal_token = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO gmail_cleanup_proposals (proposal_token, sender_domain, action_type, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (proposal_token, sender_domain, action_type, now_iso),
        )
        conn.commit()

    return {
        "proposal_token": proposal_token,
        "sender_domain": sender_domain,
        "action_type": action_type,
        "status": "pending",
        "expires_at": (now + datetime.timedelta(seconds=CLEANUP_PROPOSAL_TTL_SECONDS)).isoformat(),
        "message": f"Proposal created to {action_type} for domain '{sender_domain}'. Requires confirmation via /gmail/confirm_cleanup.",
    }


def confirm_gmail_cleanup_action(db_path: str, proposal_token: str, confirmed: bool) -> Dict[str, Any]:
    """
    Confirm or reject a pending, unexpired cleanup proposal.
    The claim is atomic: a single UPDATE guarded by status = 'pending' and the 15-minute window
    decides the outcome, so of several concurrent attempts exactly one succeeds.
    Only the proposal status changes; no Gmail action is executed here.
    """
    init_gmail_tables(db_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    cutoff_iso = (now - datetime.timedelta(seconds=CLEANUP_PROPOSAL_TTL_SECONDS)).isoformat()

    def expired_result():
        return {"success": False, "error": "PROPOSAL_EXPIRED", "status": "expired",
                "message": "Proposal expired. Please request a new proposal."}

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT sender_domain, action_type, status, created_at FROM gmail_cleanup_proposals WHERE proposal_token = ?", (proposal_token,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "PROPOSAL_NOT_FOUND", "message": "Invalid proposal token"}

        sender_domain, action_type, current_status, created_at = row
        if current_status != "pending":
            return {"success": False, "error": "PROPOSAL_ALREADY_PROCESSED", "message": f"Proposal is already {current_status}"}

        if _cleanup_proposal_expired(created_at, now):
            cursor.execute(
                "UPDATE gmail_cleanup_proposals SET status = 'expired' WHERE proposal_token = ? AND status = 'pending'",
                (proposal_token,),
            )
            conn.commit()
            return expired_result()

        # Atomic claim: the row count of this guarded UPDATE is the only thing that decides success.
        new_status = "confirmed" if confirmed else "rejected"
        cursor.execute(
            """
            UPDATE gmail_cleanup_proposals
            SET status = ?, executed_at = ?
            WHERE proposal_token = ? AND status = 'pending' AND created_at > ?
            """,
            (new_status, now_iso, proposal_token, cutoff_iso),
        )
        claimed = cursor.rowcount == 1
        conn.commit()

        if not claimed:
            cursor.execute("SELECT status FROM gmail_cleanup_proposals WHERE proposal_token = ?", (proposal_token,))
            latest = cursor.fetchone()
            if latest and latest[0] == "pending":
                return expired_result()  # lost to the 15-minute boundary
            return {"success": False, "error": "PROPOSAL_ALREADY_PROCESSED",
                    "message": f"Proposal is already {latest[0] if latest else 'processed'}"}

    return {
        "success": True,
        "proposal_token": proposal_token,
        "sender_domain": sender_domain,
        "action_type": action_type,
        "status": new_status,
        "message": f"Cleanup proposal for '{sender_domain}' successfully {new_status}.",
    }


def get_suspicious_emails(db_path: str) -> List[Dict[str, Any]]:
    """
    Retrieve all recorded phishing alerts / high-risk suspicious emails from SQLite database.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT message_id, subject, sender, sender_domain, risk_score, received_at
            FROM gmail_metadata
            WHERE category = 'phishing_alert' OR risk_score IN ('high', 'critical')
            ORDER BY received_at DESC
            """
        )
        return [
            {
                "message_id": row[0],
                "subject": row[1],
                "sender": row[2],
                "sender_domain": row[3],
                "risk_score": row[4],
                "received_at": row[5],
            }
            for row in cursor.fetchall()
        ]


def get_promotional_emails(db_path: str) -> List[Dict[str, Any]]:
    """
    Retrieve recorded newsletter / promotional emails from SQLite database.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT message_id, subject, sender, sender_domain, has_unsubscribe, received_at
            FROM gmail_metadata
            WHERE category = 'newsletter_promotional'
            ORDER BY received_at DESC
            """
        )
        return [
            {
                "message_id": row[0],
                "subject": row[1],
                "sender": row[2],
                "sender_domain": row[3],
                "has_unsubscribe": bool(row[4]),
                "received_at": row[5],
            }
            for row in cursor.fetchall()
        ]


def get_top_promotional_senders(db_path: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Retrieve top promotional senders grouped by domain, ordered by message count descending.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT sender_domain, COUNT(*) as cnt
            FROM gmail_metadata
            WHERE category = 'newsletter_promotional' AND sender_domain != ''
            GROUP BY sender_domain
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [{"sender_domain": row[0], "domain": row[0], "count": row[1]} for row in cursor.fetchall()]


def get_important_emails(db_path: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Retrieve important or high-priority emails from SQLite database.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT message_id, subject, sender, sender_domain, category, risk_score, received_at
            FROM gmail_metadata
            WHERE category = 'important' OR risk_score IN ('high', 'critical')
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        if not rows:
            cursor.execute(
                """
                SELECT message_id, subject, sender, sender_domain, category, risk_score, received_at
                FROM gmail_metadata
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
        return [
            {
                "message_id": row[0],
                "subject": row[1],
                "sender": row[2],
                "sender_domain": row[3],
                "category": row[4],
                "risk_score": row[5],
                "received_at": row[6],
            }
            for row in rows
        ]


def get_recent_messages(db_path: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Retrieve recent messages from SQLite database.
    """
    init_gmail_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT message_id, subject, sender, sender_domain, category, risk_score, received_at
            FROM gmail_metadata
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "message_id": row[0],
                "subject": row[1],
                "sender": row[2],
                "sender_domain": row[3],
                "category": row[4],
                "risk_score": row[5],
                "received_at": row[6],
            }
            for row in cursor.fetchall()
        ]



