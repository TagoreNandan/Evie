"""
Dashboard State Snapshot Service for Evie Assistant.

Implements Phase C of Revision 2 gap checklist:
- GET /dashboard/state structured response aggregator.
- Derives overall_status ("Good", "Needs Attention", "Critical") from the canonical score
  stored in security_score_current (read-only; never recalculated here).
- Exposes structured snapshots for GitHub, Gmail, Security Findings, and Recent Activity.
- Applies clear user-facing language indicating phishing detection is probabilistic/heuristic and not definitive proof.
"""

import sqlite3
from typing import Any, Dict, List
import security_score
from services.github_intelligence import get_repository_analytics, init_github_tables
from services.gmail_intelligence import (
    get_inbox_intelligence_summary,
    get_suspicious_emails,
    get_promotional_emails,
    init_gmail_tables,
)

PHISHING_HEURISTIC_DISCLAIMER = (
    "Phishing risk assessment is based on automated heuristic analysis of email headers and metadata. "
    "Risk indicators are advisory flags and do not constitute definitive proof of malicious intent."
)

PHISHING_ITEM_DISCLAIMER = (
    "Heuristic risk assessment — indicates potential risk pattern, not definitive proof."
)


def derive_overall_status(score: int) -> str:
    """
    Derive overall system status as 'Good', 'Needs Attention', or 'Critical'
    strictly from mapping the existing security score.
    Score >= 80 -> Good
    50 <= Score < 80 -> Needs Attention
    Score < 50 -> Critical
    Does NOT alter security score calculation or storage.
    """
    if score >= 80:
        return "Good"
    elif score >= 50:
        return "Needs Attention"
    else:
        return "Critical"


def get_dashboard_state(db_path: str = "evie.db") -> Dict[str, Any]:
    """
    Retrieve full structured snapshot for the dashboard UI state.
    """
    init_github_tables(db_path)
    init_gmail_tables(db_path)

    # 1. Canonical Security Score & Finding counts (read from security_score_current)
    score_data = security_score.get_current_security_score(db_path)

    score_val = score_data["score"]
    breakdown = score_data["breakdown"]
    counts = score_data["counts"]

    open_secrets = counts.get("open_secrets_count", 0)
    unack_breaches = counts.get("unacknowledged_breaches_count", 0)
    open_exposures = counts.get("open_exposures_count", 0)
    missing_twofa = counts.get("missing_twofa_count", 0)
    high_dep_alerts = counts.get("high_dep_alerts_count", 0)

    # 2. Derive overall status strictly from existing score
    overall_status = derive_overall_status(score=score_val)

    # 3. GitHub analytics summary
    try:
        repo_analytics = get_repository_analytics(db_path)
    except Exception:
        repo_analytics = {}

    github_summary = {
        "total_repositories": repo_analytics.get("total_repositories", 0),
        "open_prs_count": repo_analytics.get("open_prs_count", 0),
        "unreviewed_prs_count": repo_analytics.get("unreviewed_prs_count", 0),
        "secret_findings_count": repo_analytics.get("secret_findings_count", 0),
        "commits_total": repo_analytics.get("commits_total", 0),
        "repositories": repo_analytics.get("repositories", []),
    }

    # 4. Gmail & Phishing / Promotional Summaries
    try:
        gmail_intel = get_inbox_intelligence_summary(db_path)
    except Exception:
        gmail_intel = {}

    try:
        suspicious_list = get_suspicious_emails(db_path)
    except Exception:
        suspicious_list = []

    try:
        promotional_list = get_promotional_emails(db_path)
    except Exception:
        promotional_list = []

    phishing_alerts = []
    for email in suspicious_list:
        phishing_alerts.append({
            "message_id": email.get("message_id"),
            "subject": email.get("subject"),
            "sender": email.get("sender"),
            "sender_domain": email.get("sender_domain"),
            "risk_score": email.get("risk_score"),
            "received_at": email.get("received_at"),
            "assessment_nature": "heuristic_risk_indicator",
            "disclaimer": PHISHING_ITEM_DISCLAIMER,
        })

    gmail_summary = {
        "total_emails_scanned": gmail_intel.get("total_emails_scanned", 0),
        "important_count": gmail_intel.get("important_count", 0),
        "phishing_alert_count": gmail_intel.get("phishing_alert_count", 0),
        "promotional_count": gmail_intel.get("promotional_count", 0),
        "cleanup_recommendations": gmail_intel.get("cleanup_recommendations", []),
        "top_promotional_senders": gmail_intel.get("top_promotional_senders", []),
    }

    promotional_summary = {
        "promotional_count": len(promotional_list),
        "top_senders": gmail_intel.get("top_promotional_senders", []),
        "emails": promotional_list[:10],
    }

    # 5. Security findings summary
    security_findings_summary = {
        "open_secrets_count": open_secrets,
        "unacknowledged_breaches_count": unack_breaches,
        "open_exposures_count": open_exposures,
        "missing_twofa_count": missing_twofa,
        "high_dep_alerts_count": high_dep_alerts,
        "total_open_findings": open_secrets + unack_breaches + open_exposures + missing_twofa + high_dep_alerts,
    }

    # 6. Recent activity (from github_activity_log)
    recent_activity = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed
                FROM github_activity_log
                ORDER BY timestamp DESC
                LIMIT 10
                """
            )
            for row in cur.fetchall():
                recent_activity.append({
                    "repo_name": row["repo_name"],
                    "activity_type": row["activity_type"],
                    "identifier": row["identifier"],
                    "author": row["author"],
                    "summary": row["summary"],
                    "html_url": row["html_url"],
                    "timestamp": row["timestamp"],
                    "is_unreviewed": bool(row["is_unreviewed"]),
                })
    except Exception:
        recent_activity = []

    return {
        "overall_status": overall_status,
        "security_score": {
            "score": score_val,
            "grade": score_data["grade"],
            "trend": score_data["trend"],
            "breakdown": breakdown,
            "computed_at": score_data["computed_at"],
        },
        "github_summary": github_summary,
        "gmail_summary": gmail_summary,
        "security_findings_summary": security_findings_summary,
        "phishing_alerts": phishing_alerts,
        "phishing_disclaimer": PHISHING_HEURISTIC_DISCLAIMER,
        "promotional_summary": promotional_summary,
        "recent_activity": recent_activity,
    }
