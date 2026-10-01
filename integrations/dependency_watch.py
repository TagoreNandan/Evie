"""
GitHub Dependabot Vulnerability Alert Monitor for Evie Assistant.

Implements SCORE-03 & Section 5 of 02_TRD.md:
Queries GitHub Dependabot alerts API for monitored repositories using read-only scope (security_events).
Stores open vulnerability alerts in SQLite dependency_alerts table.
"""

import sqlite3
from datetime import datetime, timezone
import httpx

import keyring_utils
import redaction
import security_score


def init_dependency_alerts_table(db_path: str = "evie.db") -> None:
    """
    Ensure dependency_alerts table exists in SQLite database per 04_Backend_Schema.md.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dependency_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                package_name TEXT,
                severity TEXT,
                advisory_url TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.commit()


def fetch_dependabot_alerts(repo_name: str, github_pat: str | None = None, db_path: str = "evie.db") -> list[dict]:
    """
    Fetch open Dependabot alerts for a repository and persist findings to SQLite (SCORE-03).
    """
    init_dependency_alerts_table(db_path)

    token = github_pat or keyring_utils.get_credential("evie_assistant", "github_pat")
    if not token:
        raise ValueError("CREDENTIAL_MISSING: GitHub PAT not found in OS keyring")

    url = f"https://api.github.com/repos/{repo_name}/dependabot/alerts"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Evie-Assistant-Security",
    }

    alerts = []
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(url, headers=headers)
            res.raise_for_status()
            raw_alerts = res.json()
    except Exception as e:
        raise RuntimeError("API_ERROR: Failed to fetch Dependabot alerts from GitHub") from e

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(db_path) as conn:
        for item in raw_alerts:
            state = item.get("state", "open")
            if state != "open":
                continue

            vuln = item.get("security_vulnerability", {})
            pkg_name = vuln.get("package", {}).get("name", "unknown")
            severity = vuln.get("severity", "moderate")
            advisory_url = item.get("html_url", "")

            # Deduplicate by repo_name, package_name, severity
            cur = conn.execute(
                "SELECT id FROM dependency_alerts WHERE repo_name = ? AND package_name = ? AND status = 'open'",
                (repo_name, pkg_name),
            )
            if not cur.fetchone():
                conn.execute(
                    """
                    INSERT INTO dependency_alerts (repo_name, package_name, severity, advisory_url, detected_at, status)
                    VALUES (?, ?, ?, ?, ?, 'open')
                    """,
                    (repo_name, pkg_name, severity, advisory_url, now_iso),
                )
            alerts.append(
                {
                    "repo_name": repo_name,
                    "package_name": pkg_name,
                    "severity": severity,
                    "advisory_url": advisory_url,
                    "status": "open",
                }
            )
        conn.commit()

    security_score.recompute_security_score(db_path)

    return alerts
