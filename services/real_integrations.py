"""
Real Integration Adapters & Google OAuth Token Refresh Service for Evie Assistant.

Implements SEC-06 & Section 3 of 02_TRD.md:
- Production OAuth refresh token handling for Google Drive, Calendar, and Gmail APIs.
- Unified orchestration reusing existing integration modules:
  * integrations.github_watch
  * integrations.breach_watch
  * integrations.exposure_watch (Drive & Calendar ACLs)
  * integrations.calendar_actions
  * integrations.phishing_scan
  * integrations.twofa_audit
- Resilient error handling with zero secret leakage and rate limit (HTTP 429) handling.
- Telemetry synchronization across SQLite database findings tables.
"""

import json
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Set

import keyring_utils
import redaction
import security_score

from integrations.github_watch import scan_github_repository_commits
from integrations.breach_watch import BreachWatcher
from integrations.exposure_watch import DriveExposureAuditor, CalendarExposureAuditor
from integrations.phishing_scan import scan_phishing_patterns
from integrations.twofa_audit import TwoFactorAuditor


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def refresh_google_oauth_token() -> Optional[str]:
    """
    Refresh Google OAuth access token using client credentials and refresh token from OS Keyring.
    Configured for Production publishing status (indefinite refresh token validity).

    Returns:
        Fresh access token string on success, or None if credentials are missing or refresh fails.
    """
    client_id = keyring_utils.get_credential("evie_assistant", "google_oauth_client_id")
    client_secret = keyring_utils.get_credential("evie_assistant", "google_oauth_client_secret")
    refresh_token = keyring_utils.get_credential("evie_assistant", "google_oauth_refresh_token")

    if not client_id or not client_secret or not refresh_token:
        return None

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            return res_json.get("access_token")
    except Exception as e:
        # Guarantee zero secret leakage in exception logs
        redaction.redact_secret(str(e))
        return None


get_google_access_token = refresh_google_oauth_token



def fetch_github_telemetry(
    repo_name: str = "user/evie", token_key: str = "github_pat", max_commits: int = 10
) -> Dict[str, Any]:
    """
    Fetch GitHub commit secret findings by delegating to integrations.github_watch.
    """
    try:
        findings = scan_github_repository_commits(repo_name, token_key=token_key, max_commits=max_commits)
        return {"status": "success", "findings": findings, "commits_count": len(findings)}
    except Exception as e:
        redact_msg = redaction.redact_secret(str(e))
        return {"status": "unconfigured" if "not found" in str(e) else "error", "findings": [], "error": redact_msg}


def fetch_hibp_breaches(email: str = "user@example.com", key_name: str = "hibp_api_key") -> List[Dict[str, Any]]:
    """
    Fetch breach history by delegating to integrations.breach_watch.BreachWatcher.
    """
    try:
        watcher = BreachWatcher()
        return watcher.check_email_breaches(email, key_name=key_name)
    except Exception as e:
        if "HTTP 429" in str(e) or "rate limit" in str(e).lower():
            return [{"rate_limit_exceeded": True, "status": "rate_limited", "retry_after": "5"}]
        return []


def fetch_google_drive_exposures(
    drive_service: Any = None, trusted_domains: Optional[Set[str]] = None
) -> List[Dict[str, Any]]:
    """
    Audit Google Drive file exposures by delegating to integrations.exposure_watch.DriveExposureAuditor.
    Inspects metadata/permissions only; never downloads file content.
    """
    if drive_service is None:
        return []
    try:
        auditor = DriveExposureAuditor()
        return auditor.audit_drive_permissions(drive_service, trusted_domains=trusted_domains)
    except Exception as e:
        redaction.redact_secret(str(e))
        return []


def fetch_google_calendar_exposures(
    calendar_service: Any = None, trusted_domains: Optional[Set[str]] = None
) -> List[Dict[str, Any]]:
    """
    Audit Google Calendar ACL exposures by delegating to integrations.exposure_watch.CalendarExposureAuditor.
    """
    if calendar_service is None:
        return []
    try:
        auditor = CalendarExposureAuditor()
        return auditor.audit_calendar_acls(calendar_service, trusted_domains=trusted_domains)
    except Exception as e:
        redaction.redact_secret(str(e))
        return []


def fetch_gmail_phishing_alerts(messages: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Scan Gmail email headers for phishing patterns by delegating to integrations.phishing_scan.
    Strictly READ-ONLY operation.
    If no pre-supplied messages are provided, fetches live message headers via Gmail API using refreshed OAuth token.
    """
    if messages is not None:
        return scan_phishing_patterns(messages)

    try:
        from integrations.phishing_scan import fetch_gmail_messages
        token = refresh_google_oauth_token()
        if not token:
            return []
        live_messages = fetch_gmail_messages(token)
        return scan_phishing_patterns(live_messages)
    except Exception as e:
        redaction.redact_secret(str(e))
        return []


def sync_all_real_integrations(
    db_path: str = "evie.db",
    email: str = "user@example.com",
    github_repo: str = "user/evie",
    drive_service: Any = None,
    calendar_service: Any = None,
    gmail_messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Orchestrate telemetry sync across all 5 security integrations:
    GitHub, HIBP, Google Drive, Google Calendar, and Gmail, plus 2FA audit & Google OAuth token refresh.
    Persists findings into SQLite database tables.
    """
    from main import init_db
    init_db(db_path)
    google_token = refresh_google_oauth_token()
    
    # 0. Sync GitHub & Gmail Intelligence caches
    gh_intel_res = {}
    try:
        from services.github_intelligence import sync_github_intelligence, get_secret_findings
        gh_intel_res = sync_github_intelligence(db_path)
    except Exception:
        pass

    try:
        from services.gmail_intelligence import sync_gmail_intelligence
        sync_gmail_intelligence(db_path, access_token=google_token)
    except Exception:
        pass

    # Use canonical sync_github_intelligence result for GitHub telemetry
    from services.github_intelligence import get_secret_findings
    open_secrets = get_secret_findings(db_path)
    github_status = "success" if gh_intel_res.get("repositories_synced", 0) > 0 else "unconfigured"
    first_github_res = {
        "status": github_status,
        "findings": open_secrets,
        "commits_count": gh_intel_res.get("commits_synced", 0),
        "repositories_synced": gh_intel_res.get("repositories_synced", 0),
    }

    hibp_res = fetch_hibp_breaches(email)
    drive_res = fetch_google_drive_exposures(drive_service)
    calendar_res = fetch_google_calendar_exposures(calendar_service)
    gmail_res = fetch_gmail_phishing_alerts(gmail_messages)

    # 2FA Audit
    twofa_auditor = TwoFactorAuditor()
    twofa_github = None
    try:
        twofa_github = twofa_auditor.audit_github_2fa()
    except Exception:
        pass
    twofa_google = twofa_auditor.audit_google_2fa()

    # Determine per-integration status
    gmail_status = "success" if google_token else "unconfigured"
    gmail_res_data = {
        "status": gmail_status,
        "phishing_alerts_found": len(gmail_res),
    }

    hibp_key = keyring_utils.get_credential("evie_assistant", "hibp_api_key")
    hibp_status = "success" if hibp_key else "unconfigured"
    if hibp_res and isinstance(hibp_res[0], dict) and hibp_res[0].get("status") == "rate_limited":
        hibp_status = "rate_limited"

    drive_status = "success" if drive_service is not None else "unconfigured"
    calendar_status = "success" if calendar_service is not None else "unconfigured"

    # 2. Persist findings into SQLite
    with sqlite3.connect(db_path) as conn:
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
            CREATE TABLE IF NOT EXISTS twofa_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                twofa_enabled INTEGER,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Gmail Phishing
        for msg in gmail_res:
            msg_id = msg.get("message_id", "unknown")
            subject = msg.get("subject", "(No Subject)")
            risk = msg.get("risk_score", "moderate")
            cur = conn.execute("SELECT COUNT(*) FROM exposure_findings WHERE source = 'gmail' AND item_id = ?", (msg_id,))
            if cur.fetchone()[0] == 0:
                conn.execute(
                    """
                    INSERT INTO exposure_findings (source, item_id, item_name, exposure_type, status)
                    VALUES ('gmail', ?, ?, ?, 'open')
                    """,
                    (msg_id, subject, f"phishing_risk_{risk}"),
                )

        # Drive Exposures
        for item in drive_res:
            res_id = item.get("resource_id", "unknown")
            res_name = item.get("resource_name", "Drive File")
            exp_type = item.get("exposure_type", "public")
            cur = conn.execute("SELECT COUNT(*) FROM exposure_findings WHERE source = 'drive' AND item_id = ?", (res_id,))
            if cur.fetchone()[0] == 0:
                conn.execute(
                    """
                    INSERT INTO exposure_findings (source, item_id, item_name, exposure_type, status)
                    VALUES ('drive', ?, ?, ?, 'open')
                    """,
                    (res_id, res_name, exp_type),
                )

        # Calendar Exposures
        for item in calendar_res:
            res_id = item.get("resource_id", "unknown")
            res_name = item.get("resource_name", "Calendar ACL")
            exp_type = item.get("exposure_type", "public")
            cur = conn.execute("SELECT COUNT(*) FROM exposure_findings WHERE source = 'calendar' AND item_id = ?", (res_id,))
            if cur.fetchone()[0] == 0:
                conn.execute(
                    """
                    INSERT INTO exposure_findings (source, item_id, item_name, exposure_type, status)
                    VALUES ('calendar', ?, ?, ?, 'open')
                    """,
                    (res_id, res_name, exp_type),
                )

        # HIBP Breaches
        for b in hibp_res:
            if isinstance(b, dict) and b.get("name"):
                b_name = b["name"]
                b_date = b.get("breach_date", "")
                data_cls = json.dumps(b.get("data_classes", []))
                cur = conn.execute("SELECT COUNT(*) FROM breach_checks WHERE email_checked = ? AND breach_name = ?", (email, b_name))
                if cur.fetchone()[0] == 0:
                    conn.execute(
                        """
                        INSERT INTO breach_checks (email_checked, breach_name, breach_date, data_classes, acknowledged)
                        VALUES (?, ?, ?, ?, 0)
                        """,
                        (email, b_name, b_date, data_cls),
                    )

        # 2FA Audit
        conn.execute(
            """
            INSERT INTO twofa_audit (provider, twofa_enabled)
            VALUES ('google', ?), ('github', ?);
            """,
            (1 if twofa_google.get("2fa_enabled") else 0, 1 if (twofa_github and twofa_github.get("2fa_enabled")) else 0),
        )
        conn.commit()

    security_score.recompute_security_score(db_path)

    return {
        "status": "success",
        "github": first_github_res,
        "gmail": gmail_res_data,
        "hibp": {"status": hibp_status, "breaches_found": len(hibp_res)},
        "drive": {"status": drive_status, "exposures_found": len(drive_res)},
        "calendar": {"status": calendar_status, "exposures_found": len(calendar_res)},
        "hibp_breaches_found": len(hibp_res),
        "drive_exposures_found": len(drive_res),
        "calendar_exposures_found": len(calendar_res),
        "gmail_phishing_alerts_found": len(gmail_res),
        "google_authenticated": bool(google_token),
    }

