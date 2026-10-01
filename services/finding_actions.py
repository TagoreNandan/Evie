"""
Security Finding Queries & Actions for Evie Assistant.

Shared by the REST endpoints (GET /findings, POST /findings/{id}/resolve) and the
tool-calling registry so finding reads and resolution have exactly one implementation.
Resolution always triggers recompute_security_score() (04_Backend_Schema.md Section 6).
"""

import sqlite3
from typing import Any, Dict, List

import security_score

RESOLVABLE_FINDING_TYPES = ("secret", "exposure", "dependency", "breach")
RESOLUTION_STATUSES = ("resolved", "false_positive")


def list_open_findings(db_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    List open security findings across all categories (GET /findings contract).
    """
    findings: Dict[str, List[Dict[str, Any]]] = {"secrets": [], "breaches": [], "exposures": [], "twofa": [], "dependencies": []}
    queries = {
        "secrets": "SELECT * FROM secret_findings WHERE status = 'open'",
        "breaches": "SELECT * FROM breach_checks WHERE acknowledged = 0",
        "exposures": "SELECT * FROM exposure_findings WHERE status = 'open'",
        "twofa": "SELECT * FROM twofa_audit ORDER BY checked_at DESC",
        "dependencies": "SELECT * FROM dependency_alerts WHERE status = 'open'",
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for category, sql in queries.items():
            try:
                findings[category] = [dict(r) for r in conn.execute(sql).fetchall()]
            except sqlite3.OperationalError:
                pass
    return findings


def get_twofa_status(db_path: str) -> List[Dict[str, Any]]:
    """
    Latest 2FA audit result per provider (same rule the security score uses).
    """
    latest: Dict[str, Dict[str, Any]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT provider, twofa_enabled, checked_at FROM twofa_audit ORDER BY checked_at DESC, id DESC").fetchall()
        except sqlite3.OperationalError:
            rows = []
    for r in rows:
        if r["provider"] not in latest:
            latest[r["provider"]] = {
                "provider": r["provider"],
                "twofa_enabled": bool(r["twofa_enabled"]),
                "checked_at": r["checked_at"],
            }
    return list(latest.values())


def resolve_finding(db_path: str, finding_type: str, finding_id: int, resolution_status: str = "resolved") -> Dict[str, Any] | None:
    """
    Mark a finding resolved/false-positive, then immediately recompute the canonical score.
    Returns the canonical score payload, or None if no matching finding exists.
    Raises ValueError for an unsupported finding type or status.
    """
    if resolution_status not in RESOLUTION_STATUSES:
        raise ValueError("INVALID_RESOLUTION_STATUS")

    statements = {
        "secret": ("UPDATE secret_findings SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?", (resolution_status, finding_id)),
        "exposure": ("UPDATE exposure_findings SET status = ? WHERE id = ?", (resolution_status, finding_id)),
        "dependency": ("UPDATE dependency_alerts SET status = ? WHERE id = ?", (resolution_status, finding_id)),
        "breach": ("UPDATE breach_checks SET acknowledged = 1 WHERE id = ?", (finding_id,)),
    }
    if finding_type not in statements:
        raise ValueError("INVALID_FINDING_TYPE")

    sql, params = statements[finding_type]
    try:
        with sqlite3.connect(db_path) as conn:
            updated = conn.execute(sql, params).rowcount
            conn.commit()
    except sqlite3.OperationalError:
        updated = 0
    if not updated:
        return None

    return security_score.recompute_security_score(db_path)
