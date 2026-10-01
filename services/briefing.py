"""
Shared Briefing Aggregator Engine for Evie Assistant.

Implements BRIEF-01 & Section 2 of 02_TRD.md:
Aggregates overnight GitHub activity, today's calendar events, active security findings,
and general highlights into a single consolidated report.

All briefing delivery triggers (webhook, wake, on-demand) call generate_briefing().
"""

import sqlite3
from datetime import datetime, timezone, time
import httpx
import keyring_utils
import redaction
import security_score


def init_github_events_table(db_path: str = "evie.db") -> None:
    """
    Ensure github_events table exists in SQLite database per 04_Backend_Schema.md.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT UNIQUE,
                repo_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT,
                summary TEXT,
                event_data TEXT,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notified_realtime INTEGER DEFAULT 0,
                included_in_briefing INTEGER DEFAULT 0
            );
            """
        )
        conn.commit()


def fetch_today_calendar_events(oauth_token: str | None = None) -> list[dict]:
    """
    Fetch today's events from Google Calendar REST API v3.
    Isolated helper boundary to allow easy unit testing without network calls.
    """
    if not oauth_token:
        raise ValueError("CREDENTIAL_MISSING: Google Calendar OAuth credential not found in OS keyring")

    now = datetime.now(timezone.utc)
    start_of_day = datetime.combine(now.date(), time.min, tzinfo=timezone.utc).isoformat()
    end_of_day = datetime.combine(now.date(), time.max, tzinfo=timezone.utc).isoformat()

    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    headers = {"Authorization": f"Bearer {oauth_token}"}
    params = {
        "timeMin": start_of_day,
        "timeMax": end_of_day,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

    events = []
    for item in data.get("items", []):
        events.append(
            {
                "summary": item.get("summary", "(No Summary)"),
                "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
                "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
            }
        )
    return events


def generate_briefing(db_path: str = "evie.db", trigger: str = "wake", repo_name: str | None = None) -> dict:
    """
    Generate a consolidated daily briefing report.
    Aggregates GitHub events, Google Calendar events, and active security findings.
    Resilient to partial external API failures (surfaces safe error status without crashing).
    """
    init_github_events_table(db_path)
    partial_failures = []

    # 1. GitHub Activity Section (Deterministic Deduplication)
    github_events = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT id, repo_name, event_type, actor, summary, received_at FROM github_events WHERE included_in_briefing = 0"
        params = []
        if repo_name:
            query += " AND repo_name = ?"
            params.append(repo_name)
        query += " ORDER BY id ASC"
        
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        
        selected_ids = []
        for row in rows:
            selected_ids.append(row["id"])
            github_events.append(
                {
                    "repo_name": row["repo_name"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "summary": row["summary"],
                    "received_at": row["received_at"],
                }
            )
        
        if selected_ids:
            placeholders = ",".join("?" * len(selected_ids))
            conn.execute(f"UPDATE github_events SET included_in_briefing = 1 WHERE id IN ({placeholders})", selected_ids)
            conn.commit()

    github_section = {
        "status": "ok",
        "count": len(github_events),
        "events": github_events,
    }

    # 2. Google Calendar Section (Safe Error Handling)
    calendar_today = {}
    try:
        oauth_token = keyring_utils.get_credential("evie_assistant", "google_calendar_oauth")
        cal_events = fetch_today_calendar_events(oauth_token)
        calendar_today = {
            "status": "ok",
            "event_count": len(cal_events),
            "events": cal_events,
        }
    except ValueError:
        calendar_today = {
            "status": "unavailable",
            "error_code": "CREDENTIAL_MISSING",
            "event_count": 0,
            "events": [],
        }
        partial_failures.append({"source": "google_calendar", "error_code": "CREDENTIAL_MISSING"})
    except Exception:
        calendar_today = {
            "status": "unavailable",
            "error_code": "CALENDAR_FETCH_FAILED",
            "event_count": 0,
            "events": [],
        }
        partial_failures.append({"source": "google_calendar", "error_code": "CALENDAR_FETCH_FAILED"})

    # 3. Security Findings Section (Phase 1 SQLite Schema Alignment)
    open_secrets = 0
    unacknowledged_breaches = 0
    public_exposures = 0
    twofa_warnings = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # Secret findings
        try:
            cur = conn.execute("SELECT COUNT(*) FROM secret_findings WHERE status = 'open'")
            open_secrets = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # Breach checks
        try:
            cur = conn.execute("SELECT COUNT(*) FROM breach_checks WHERE acknowledged = 0")
            unacknowledged_breaches = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # Public exposure findings
        try:
            cur = conn.execute("SELECT COUNT(*) FROM exposure_findings WHERE status = 'open'")
            public_exposures = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # 2FA audit warnings
        try:
            cur = conn.execute("SELECT provider, twofa_enabled FROM twofa_audit ORDER BY checked_at DESC")
            for r in cur.fetchall():
                if r["twofa_enabled"] == 0:
                    twofa_warnings.append(r["provider"])
        except sqlite3.OperationalError:
            pass

    # Canonical score — read from security_score_current, never recalculated here
    canonical_score = security_score.get_current_security_score(db_path)

    security_section = {
        "status": "ok",
        "score": canonical_score["score"],
        "grade": canonical_score["grade"],
        "trend": canonical_score["trend"],
        "open_secrets": open_secrets,
        "unacknowledged_breaches": unacknowledged_breaches,
        "public_exposures": public_exposures,
        "twofa_warnings": twofa_warnings,
    }

    # 4. GitHub Intelligence Section
    github_intel = {}
    try:
        from services.github_intelligence import get_repository_analytics, get_unreviewed_prs, get_secret_findings, get_recent_activity
        analytics = get_repository_analytics(db_path)
        unreviewed_prs = get_unreviewed_prs(db_path)
        recent_activity = get_recent_activity(db_path, days=1)
        open_secrets = len(get_secret_findings(db_path))
        github_intel = {
            "status": "ok",
            "total_repositories": analytics.get("total_repositories", 0),
            "unreviewed_prs_count": analytics.get("unreviewed_prs_count", 0),
            "unreviewed_prs": unreviewed_prs,
            "open_secrets_count": open_secrets,
            "commits_yesterday": analytics.get("commits_yesterday", 0),
            "commits_last_month": analytics.get("commits_last_month", 0),
            "commits_this_year": analytics.get("commits_this_year", 0),
            "recent_activity": recent_activity,
        }
    except Exception:
        github_intel = {"status": "unavailable", "unreviewed_prs_count": 0, "unreviewed_prs": [], "open_secrets_count": 0}

    # 5. Gmail Intelligence Section
    gmail_intel = {}
    try:
        from services.gmail_intelligence import get_inbox_intelligence_summary, get_suspicious_emails
        gmail_summary = get_inbox_intelligence_summary(db_path)
        suspicious_list = get_suspicious_emails(db_path)
        gmail_intel = {
            "status": "ok",
            "important_count": gmail_summary.get("important_count", 0),
            "phishing_alert_count": gmail_summary.get("phishing_alert_count", 0),
            "phishing_alerts_count": gmail_summary.get("phishing_alert_count", 0),
            "phishing_alerts": suspicious_list,
            "suspicious_emails": suspicious_list,
            "promotional_count": gmail_summary.get("promotional_count", 0),
            "top_promotional_senders": gmail_summary.get("top_promotional_senders", []),
            "cleanup_recommendations": gmail_summary.get("cleanup_recommendations", []),
        }
    except Exception:
        gmail_intel = {"status": "unavailable", "important_count": 0, "phishing_alert_count": 0, "phishing_alerts_count": 0, "promotional_count": 0}

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "timestamp": timestamp,
        "trigger": trigger,
        "summary": f"Evie Daily Briefing ({trigger})",
        "sections": {
            "github_activity": github_section,
            "calendar_today": calendar_today,
            "security_findings": security_section,
            "github_intelligence": github_intel,
            "gmail_intelligence": gmail_intel,
        },
        "partial_failures": partial_failures,
    }

