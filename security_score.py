"""
Security Health Score Aggregator & Trending Engine for Evie Assistant.

Implements SCORE-01 & Section 8 of 06_Acceptance_Criteria.md:
- Computes deterministic 0–100 Security Health Score.
- Aggregates active findings across secrets, breaches, exposures, 2FA, and dependencies.
- Logs historical trends to SQLite security_score_history table.

Canonical score (04_Backend_Schema.md Section 3, TRD Section 4):
- recompute_security_score() is the ONLY function that calculates the score and the
  ONLY writer of the single-row security_score_current table.
- Every consumer (dashboard, chat, briefing, GET /score) reads the stored row via
  get_current_security_score(); none recalculates independently.
"""

import json
import sqlite3
from datetime import datetime, timezone
import redaction


CURRENT_SCORE_EXTRA_COLUMNS = {
    "dependencies_deduction": "INTEGER DEFAULT 0",
    "active_findings_count": "INTEGER DEFAULT 0",
    "counts": "TEXT",
    "trend": "TEXT",
}


def init_score_history_table(db_path: str = "evie.db") -> None:
    """
    Ensure security_score_current and security_score_history tables exist per 04_Backend_Schema.md.
    security_score_current carries the documented columns plus the dependency deduction,
    finding counts, and trend so consumers never need to recount findings themselves.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_score_current (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                score INTEGER NOT NULL,
                grade TEXT NOT NULL,
                secrets_deduction INTEGER DEFAULT 0,
                breaches_deduction INTEGER DEFAULT 0,
                exposure_deduction INTEGER DEFAULT 0,
                twofa_deduction INTEGER DEFAULT 0,
                dependencies_deduction INTEGER DEFAULT 0,
                active_findings_count INTEGER DEFAULT 0,
                counts TEXT,
                trend TEXT,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        existing = {r[1] for r in conn.execute("PRAGMA table_info(security_score_current)")}
        for col, ddl in CURRENT_SCORE_EXTRA_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE security_score_current ADD COLUMN {col} {ddl}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_score_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score INTEGER NOT NULL,
                breakdown TEXT,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


def recompute_security_score(db_path: str = "evie.db", log_history: bool = True) -> dict:
    """
    The single authoritative score calculation. Recomputes the 0-100 Security Health Score
    from current finding state, persists it to security_score_current (single row, id = 1),
    appends a security_score_history row, and returns the canonical result.
    Call this after any finding is created or resolved.
    Baseline: 100 points.
    Deductions:
      - Open secrets: -15 pts per open finding (max -45)
      - Unacknowledged breaches: -10 pts per breach (max -30)
      - Open public exposures: -10 pts per exposure (max -30)
      - Missing 2FA: -15 pts per unenabled provider (max -30)
      - Critical/High Dependabot alerts: -10 pts per alert (max -30)
    Floor: 0
    """
    init_score_history_table(db_path)

    open_secrets_count = 0
    unacknowledged_breaches_count = 0
    open_exposures_count = 0
    missing_twofa_count = 0
    high_dep_alerts_count = 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 1. Open secrets count
        try:
            cur = conn.execute("SELECT COUNT(*) FROM secret_findings WHERE status = 'open'")
            open_secrets_count = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # 2. Unacknowledged breaches count
        try:
            cur = conn.execute("SELECT COUNT(*) FROM breach_checks WHERE acknowledged = 0")
            unacknowledged_breaches_count = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # 3. Open exposures count
        try:
            cur = conn.execute("SELECT COUNT(*) FROM exposure_findings WHERE status = 'open'")
            open_exposures_count = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # 4. Missing 2FA count
        try:
            cur = conn.execute("SELECT provider, twofa_enabled FROM twofa_audit ORDER BY checked_at DESC")
            providers_seen = set()
            for r in cur.fetchall():
                p = r["provider"]
                if p not in providers_seen:
                    providers_seen.add(p)
                    if r["twofa_enabled"] == 0:
                        missing_twofa_count += 1
        except sqlite3.OperationalError:
            pass

        # 5. Dependabot critical/high alerts count
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM dependency_alerts WHERE status = 'open' AND LOWER(severity) IN ('critical', 'high')"
            )
            high_dep_alerts_count = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

    # Calculate bounded penalty deductions
    secrets_deduction = max(-45, -15 * open_secrets_count)
    breaches_deduction = max(-30, -10 * unacknowledged_breaches_count)
    exposures_deduction = max(-30, -10 * open_exposures_count)
    twofa_deduction = max(-30, -15 * missing_twofa_count)
    dependencies_deduction = max(-30, -10 * high_dep_alerts_count)

    breakdown = {
        "secrets": secrets_deduction,
        "breaches": breaches_deduction,
        "exposures": exposures_deduction,
        "twofa": twofa_deduction,
        "dependencies": dependencies_deduction,
    }

    total_deductions = sum(breakdown.values())
    raw_score = 100 + total_deductions
    final_score = max(0, min(100, raw_score))

    # Trend calculation
    prev_score = None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT score FROM security_score_history ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            prev_score = row["score"]

    if prev_score is None:
        trend = "stable"
    elif final_score > prev_score:
        trend = "improving"
    elif final_score < prev_score:
        trend = "declining"
    else:
        trend = "stable"

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    active_findings_count = (
        open_secrets_count
        + unacknowledged_breaches_count
        + open_exposures_count
        + missing_twofa_count
        + high_dep_alerts_count
    )

    counts = {
        "open_secrets_count": open_secrets_count,
        "unacknowledged_breaches_count": unacknowledged_breaches_count,
        "open_exposures_count": open_exposures_count,
        "missing_twofa_count": missing_twofa_count,
        "high_dep_alerts_count": high_dep_alerts_count,
    }

    grade = (
        "A" if final_score >= 85
        else ("B" if final_score >= 75
        else ("C" if final_score >= 60
        else "D"))
    )

    with sqlite3.connect(db_path) as conn:
        if log_history:
            conn.execute(
                """
                INSERT INTO security_score_history (score, breakdown, computed_at)
                VALUES (?, ?, ?)
                """,
                (final_score, json.dumps(breakdown), now_iso),
            )
        conn.execute(
            """
            INSERT INTO security_score_current (
                id, score, grade, secrets_deduction, breaches_deduction, exposure_deduction,
                twofa_deduction, dependencies_deduction, active_findings_count, counts, trend, computed_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                score = excluded.score,
                grade = excluded.grade,
                secrets_deduction = excluded.secrets_deduction,
                breaches_deduction = excluded.breaches_deduction,
                exposure_deduction = excluded.exposure_deduction,
                twofa_deduction = excluded.twofa_deduction,
                dependencies_deduction = excluded.dependencies_deduction,
                active_findings_count = excluded.active_findings_count,
                counts = excluded.counts,
                trend = excluded.trend,
                computed_at = excluded.computed_at
            """,
            (
                final_score,
                grade,
                secrets_deduction,
                breaches_deduction,
                exposures_deduction,
                twofa_deduction,
                dependencies_deduction,
                active_findings_count,
                json.dumps(counts),
                trend,
                now_iso,
            ),
        )
        conn.commit()

    return get_current_security_score(db_path)


def _row_to_score(row: sqlite3.Row) -> dict:
    """
    Shape a security_score_current row into the public score payload (GET /score contract).
    """
    score = row["score"]
    open_secrets_count = json.loads(row["counts"] or "{}").get("open_secrets_count", 0)
    return {
        "score": score,
        "current_score": score,
        "grade": row["grade"],
        "active_findings_count": row["active_findings_count"],
        "candidate_secrets_count": open_secrets_count,
        "trend": row["trend"] or "stable",
        "breakdown": {
            "secrets": row["secrets_deduction"],
            "breaches": row["breaches_deduction"],
            "exposures": row["exposure_deduction"],
            "twofa": row["twofa_deduction"],
            "dependencies": row["dependencies_deduction"],
        },
        "counts": json.loads(row["counts"] or "{}"),
        "note": "Secret findings reflect high-entropy candidate secrets detected in git commits.",
        "computed_at": row["computed_at"],
    }


def get_current_security_score(db_path: str = "evie.db") -> dict:
    """
    Read the canonical score from security_score_current. This is the only way consumers
    (dashboard, chat, briefing, GET /score) obtain the score. If the row has never been
    written (fresh database), it is bootstrapped once via recompute_security_score().
    """
    init_score_history_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM security_score_current WHERE id = 1").fetchone()
    if row is None:
        return recompute_security_score(db_path)
    return _row_to_score(row)


def compute_security_score(db_path: str = "evie.db", log_history: bool = True) -> dict:
    """
    Backward-compatible alias for recompute_security_score(). Not a separate calculation path.
    """
    return recompute_security_score(db_path, log_history=log_history)
