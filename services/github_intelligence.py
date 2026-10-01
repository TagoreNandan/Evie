"""
GitHub Intelligence Service for Evie Assistant.

Implements SEC-07:
Coordinates repository discovery, commit time-series tracking, PR review status monitoring,
and secret finding persistence in SQLite.
"""

import datetime
import sqlite3
import security_score
from typing import Any, Dict, List, Optional

from integrations.github_watch import (
    SECRET_PATTERNS,
    discover_user_repositories_detailed,
    scan_github_repository_activity,
    scan_github_repository_commits,
)
from redaction import redact_secret


def _mask_secrets(text: str) -> str:
    """Redact credential-pattern matches before commit/PR text is stored in the activity log."""
    text = text or ""
    for pattern in SECRET_PATTERNS.values():
        text = pattern.sub(lambda m: redact_secret(m.group(0)), text)
    return text


def check_github_sync_status(db_path: str) -> str:
    """
    Distinguishes GitHub synchronization state:
    - 'unconfigured': if no GitHub token credential exists AND no repositories/activity have been synchronized.
    - 'synced': if GitHub credentials exist or repositories/activity have been synchronized in database.
    """
    from keyring_utils import get_credential, SERVICE_NAME
    token = get_credential(SERVICE_NAME, "github_token") or get_credential(SERVICE_NAME, "github_pat")

    init_github_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM github_repo_cache")
        repo_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM github_activity_log")
        act_count = cursor.fetchone()[0]
        try:
            cursor.execute("SELECT COUNT(*) FROM github_events")
            evt_count = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            evt_count = 0

    if not token and repo_count == 0 and act_count == 0 and evt_count == 0:
        return "unconfigured"
    return "synced"


def init_github_tables(db_path: str) -> None:

    """
    Initialize SQLite schema for GitHub repository metadata, activity history, and secret findings.
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS github_repo_cache (
                full_name TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                is_private INTEGER NOT NULL,
                is_org INTEGER NOT NULL,
                html_url TEXT NOT NULL,
                last_synced TEXT NOT NULL,
                last_pushed_at TEXT
            )
        """)
        try:
            cursor.execute("ALTER TABLE github_repo_cache ADD COLUMN last_pushed_at TEXT")
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS github_activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                activity_type TEXT NOT NULL, -- 'commit', 'pr'
                identifier TEXT NOT NULL,    -- SHA or PR number
                author TEXT NOT NULL,
                summary TEXT NOT NULL,
                html_url TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                is_unreviewed INTEGER DEFAULT 0,
                UNIQUE(repo_name, activity_type, identifier)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS secret_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line_number INTEGER DEFAULT 0,
                pattern_type TEXT,
                redacted_preview TEXT,
                status TEXT DEFAULT 'open',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                UNIQUE(repo_name, commit_sha, file_path, line_number, pattern_type)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS github_scanned_commits (
                repo_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(repo_name, commit_sha)
            )
        """)
        conn.commit()


def get_scanned_commit_shas(db_path: str, repo_name: str) -> set:
    """
    Retrieve the set of commit SHAs already secret-scanned for a repository.
    Sourced only from github_scanned_commits ("this exact SHA was actually scanned");
    a SHA merely appearing in a finding (e.g. from a webhook payload) is not proof its
    content was scanned.
    """
    init_github_tables(db_path)
    shas = set()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT commit_sha FROM github_scanned_commits WHERE repo_name = ?", (repo_name,))
        for row in cursor.fetchall():
            if row[0]:
                shas.add(row[0])
                if len(row[0]) >= 7:
                    shas.add(row[0][:7])
    return shas


def record_secret_finding(cursor, repo_name: str, commit_sha: str, file_path: str, line_number, pattern_type: str, redacted_preview: str) -> bool:
    """
    Insert one (already redacted) secret finding unless it is a duplicate, or unless the user
    already marked the same repository/file/line/pattern as a false positive (AC 2.4).
    Returns True if a new row was written.
    """
    line = line_number or 0
    cursor.execute(
        """
        SELECT 1 FROM secret_findings
        WHERE repo_name = ? AND file_path = ? AND COALESCE(line_number, 0) = ? AND pattern_type = ?
          AND (status = 'false_positive' OR commit_sha = ?)
        LIMIT 1
        """,
        (repo_name, file_path, line, pattern_type, commit_sha),
    )
    if cursor.fetchone():
        return False
    cursor.execute(
        """
        INSERT OR IGNORE INTO secret_findings
        (repo_name, commit_sha, file_path, line_number, pattern_type, redacted_preview, status)
        VALUES (?, ?, ?, ?, ?, ?, 'open')
        """,
        (repo_name, commit_sha, file_path, line, pattern_type, redacted_preview),
    )
    return cursor.rowcount > 0


def sync_github_intelligence(db_path: str, token_key: str = "github_token") -> Dict[str, Any]:
    """
    Discover all accessible personal/private/org repos, scan activity & secrets incrementally,
    and persist results to SQLite.
    """
    init_github_tables(db_path)
    repos = discover_user_repositories_detailed(token_key=token_key)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    synced_repos = 0
    synced_commits = 0
    synced_prs = 0
    synced_secrets = 0
    repos_changed = 0
    repos_skipped = 0
    repos_incomplete = 0

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Load existing cached pushed_at timestamps for incremental checks
        cached_pushed = {}
        cursor.execute("SELECT full_name, last_pushed_at FROM github_repo_cache")
        for row in cursor.fetchall():
            if row[0]:
                cached_pushed[row[0]] = row[1]

        # Mark stale lockfile and dependency package findings as resolved
        cursor.execute(
            """
            UPDATE secret_findings
            SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
            WHERE status = 'open' AND (
                file_path LIKE '%.lock' OR
                file_path LIKE '%-lock.json' OR
                file_path LIKE '%-lock.yaml' OR
                file_path LIKE '%go.sum' OR
                file_path LIKE '%go.mod' OR
                file_path LIKE '%node_modules/%' OR
                file_path LIKE '%site-packages/%' OR
                file_path LIKE '%venv/%' OR
                file_path LIKE '%.venv/%' OR
                file_path LIKE '%.dist-info/%'
            )
            """
        )
        conn.commit()

        for r in repos:
            repo_name = r["full_name"]
            remote_pushed = r.get("pushed_at")

            # Store / update repo metadata
            cursor.execute(
                """
                INSERT INTO github_repo_cache 
                (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    name=excluded.name,
                    owner=excluded.owner,
                    is_private=excluded.is_private,
                    is_org=excluded.is_org,
                    html_url=excluded.html_url,
                    last_synced=excluded.last_synced
                """,
                (
                    repo_name,
                    r["name"],
                    r["owner"],
                    1 if r["is_private"] else 0,
                    1 if r["is_org"] else 0,
                    r["html_url"],
                    now_iso,
                    None,  # pushed_at checkpoint is advanced only after a complete secret scan
                ),
            )
            synced_repos += 1

            # Check if repository was pushed since last sync
            prev_pushed = cached_pushed.get(repo_name)
            if remote_pushed and prev_pushed and remote_pushed == prev_pushed:
                # No new commits/pushes since last scan -> skip scanning API calls!
                repos_skipped += 1
                conn.commit()
                continue
            repos_changed += 1

            # Scan activity
            activity = scan_github_repository_activity(r["full_name"], token_key=token_key)

            for c in activity.get("commits", []):
                ts = c.get("timestamp") or now_iso
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO github_activity_log
                    (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed)
                    VALUES (?, 'commit', ?, ?, ?, ?, ?, 0)
                    """,
                    (r["full_name"], c["sha"], c.get("author", "unknown"), _mask_secrets(c.get("summary", "")), c.get("html_url", ""), ts),
                )
                synced_commits += 1

            for pr in activity.get("pull_requests", []):
                is_unrev = 1 if pr.get("is_unreviewed") else 0
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO github_activity_log
                    (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed)
                    VALUES (?, 'pr', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["full_name"],
                        str(pr["number"]),
                        pr.get("author", "unknown"),
                        _mask_secrets(pr.get("title", "")),
                        pr.get("html_url", ""),
                        now_iso,
                        is_unrev,
                    ),
                )
                synced_prs += 1

            # Delta secret scan: newest-first until an already-scanned SHA (hard cap per sync)
            scanned_shas = get_scanned_commit_shas(db_path, r["full_name"])
            try:
                scan = scan_github_repository_commits(r["full_name"], token_key=token_key, scanned_shas=scanned_shas, return_scanned=True)
            except Exception:
                scan = None

            if scan is not None:
                for f in scan.findings:
                    if record_secret_finding(
                        cursor,
                        f.get("repo_name", r["full_name"]),
                        f.get("commit_sha", ""),
                        f.get("filename") or f.get("file_path", ""),
                        f.get("line_number", 0),
                        f.get("secret_type") or f.get("pattern_type", "High Entropy Candidate Secret"),
                        f.get("redacted_value") or f.get("redacted_preview", "[REDACTED]"),
                    ):
                        synced_secrets += 1

                # github_scanned_commits means "this exact SHA was actually secret-scanned"
                for sha in scan.scanned_shas:
                    cursor.execute(
                        "INSERT OR IGNORE INTO github_scanned_commits (repo_name, commit_sha, scanned_at) VALUES (?, ?, ?)",
                        (r["full_name"], sha, now_iso),
                    )

            if scan is not None and scan.complete:
                if remote_pushed:
                    cursor.execute(
                        "UPDATE github_repo_cache SET last_pushed_at = ? WHERE full_name = ?",
                        (remote_pushed, repo_name),
                    )
            else:
                # Keep the old checkpoint so the unscanned delta is retried on the next sync
                repos_incomplete += 1

            conn.commit()

    security_score.recompute_security_score(db_path)

    return {
        "repositories_synced": synced_repos,
        "commits_synced": synced_commits,
        "prs_synced": synced_prs,
        "secrets_found": synced_secrets,
        "repositories_changed": repos_changed,
        "repositories_skipped": repos_skipped,
        "repositories_incomplete": repos_incomplete,
    }


def get_repository_analytics(db_path: str) -> Dict[str, Any]:
    """
    Retrieve repository analytics including total repos, commit breakdown by time window,
    active PRs, unreviewed PRs, and secret findings.
    """
    init_github_tables(db_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    one_month_ago = (now - datetime.timedelta(days=30)).isoformat()
    one_year_ago = (now - datetime.timedelta(days=365)).isoformat()
    yesterday = (now - datetime.timedelta(days=1)).isoformat()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM github_repo_cache")
        total_repos = cursor.fetchone()[0]

        cursor.execute("SELECT full_name, name, owner, is_private, is_org, html_url FROM github_repo_cache")
        repos = [
            {
                "full_name": row[0],
                "name": row[1],
                "owner": row[2],
                "is_private": bool(row[3]),
                "is_org": bool(row[4]),
                "html_url": row[5],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE activity_type = 'commit' AND timestamp >= ?", (yesterday,))
        commits_yesterday = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE activity_type = 'commit' AND timestamp >= ?", (one_month_ago,))
        commits_last_month = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE activity_type = 'commit' AND timestamp >= ?", (one_year_ago,))
        commits_this_year = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE activity_type = 'commit'")
        commits_total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE activity_type = 'pr'")
        open_prs_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE activity_type = 'pr' AND is_unreviewed = 1")
        unreviewed_prs_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM secret_findings WHERE status = 'open'")
        secret_findings_count = cursor.fetchone()[0]

    return {
        "total_repositories": total_repos,
        "repositories": repos,
        "commits_yesterday": commits_yesterday,
        "commits_last_month": commits_last_month,
        "commits_this_year": commits_this_year,
        "commits_total": commits_total,
        "open_prs_count": open_prs_count,
        "unreviewed_prs_count": unreviewed_prs_count,
        "secret_findings_count": secret_findings_count,
    }


def get_unreviewed_prs(db_path: str) -> List[Dict[str, Any]]:
    """
    Get all open unreviewed PRs from SQLite database.
    """
    init_github_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT repo_name, identifier, author, summary, html_url
            FROM github_activity_log
            WHERE activity_type = 'pr' AND is_unreviewed = 1
            """
        )
        return [
            {
                "repo_name": row[0],
                "number": row[1],
                "author": row[2],
                "title": row[3],
                "html_url": row[4],
            }
            for row in cursor.fetchall()
        ]


def get_secret_findings(db_path: str) -> List[Dict[str, Any]]:
    """
    Retrieve all recorded open secret findings from SQLite database.
    """
    init_github_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT repo_name, commit_sha, file_path, line_number, pattern_type, redacted_preview, status, detected_at
            FROM secret_findings
            WHERE status = 'open'
            """
        )
        return [
            {
                "repo_name": row[0],
                "commit_sha": row[1],
                "file_path": row[2],
                "filename": row[2],
                "line_number": row[3],
                "pattern_type": row[4],
                "secret_type": row[4],
                "redacted_preview": row[5],
                "redacted_value": row[5],
                "status": row[6],
                "github_url": f"https://github.com/{row[0]}/commit/{row[1]}",
                "timestamp": str(row[7]) if row[7] else "",
            }
            for row in cursor.fetchall()
        ]


def get_recent_activity(db_path: str, days: int = 1, strict_date: bool = False) -> List[Dict[str, Any]]:
    """
    Retrieve recent GitHub commit and PR activity log items from SQLite database.
    If strict_date is True, does not fall back to older historical items when 0 items match.
    """
    init_github_tables(db_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=days)).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed
            FROM github_activity_log
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (cutoff,),
        )
        rows = cursor.fetchall()
        if not rows and not strict_date:
            cursor.execute(
                """
                SELECT repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed
                FROM github_activity_log
                ORDER BY timestamp DESC
                LIMIT 10
                """
            )
            rows = cursor.fetchall()

        return [
            {
                "repo_name": row[0],
                "activity_type": row[1],
                "identifier": row[2],
                "author": row[3],
                "summary": row[4],
                "html_url": row[5],
                "timestamp": row[6],
                "is_unreviewed": bool(row[7]),
            }
            for row in rows
        ]


def get_yesterday_activity(db_path: str) -> Dict[str, Any]:
    """
    Retrieve activity strictly from the previous calendar day (yesterday).
    Does NOT fall back to older historical records if zero activity occurred yesterday.
    """
    init_github_tables(db_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    yesterday_dt = now - datetime.timedelta(days=1)
    yesterday_date_str = yesterday_dt.strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed
            FROM github_activity_log
            WHERE timestamp LIKE ?
            ORDER BY timestamp DESC
            """,
            (f"{yesterday_date_str}%",),
        )
        rows = cursor.fetchall()
        
        if not rows:
            start_of_yesterday = datetime.datetime.combine(yesterday_dt.date(), datetime.time.min, tzinfo=datetime.timezone.utc).isoformat()
            end_of_yesterday = datetime.datetime.combine(yesterday_dt.date(), datetime.time.max, tzinfo=datetime.timezone.utc).isoformat()
            cursor.execute(
                """
                SELECT repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed
                FROM github_activity_log
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
                """,
                (start_of_yesterday, end_of_yesterday),
            )
            rows = cursor.fetchall()

        items = [
            {
                "repo_name": row[0],
                "activity_type": row[1],
                "identifier": row[2],
                "author": row[3],
                "summary": row[4],
                "html_url": row[5],
                "timestamp": row[6],
                "is_unreviewed": bool(row[7]),
            }
            for row in rows
        ]
        commit_count = sum(1 for i in items if i["activity_type"] == "commit")
        return {"items": items, "commit_count": commit_count}


def get_secret_findings_by_repository(db_path: str) -> List[Dict[str, Any]]:
    """
    Retrieve open secret findings count aggregated per repository.
    """
    init_github_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT repo_name, COUNT(*) as cnt
            FROM secret_findings
            WHERE status = 'open'
            GROUP BY repo_name
            ORDER BY cnt DESC
            """
        )
        return [{"repo_name": row[0], "candidate_count": row[1]} for row in cursor.fetchall()]


def process_github_webhook_event(event_data: Dict[str, Any], db_path: str) -> Dict[str, Any]:
    """
    Processes real-time GitHub push/PR webhook event payload.
    Persists incremental activity log and secret findings without scanning full repository history.
    """
    init_github_tables(db_path)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    repo_info = event_data.get("repository", {})
    repo_name = repo_info.get("full_name") or repo_info.get("name") or event_data.get("repo_name", "unknown")
    event_type = event_data.get("event_type") or event_data.get("action") or "push"

    processed_count = 0
    secrets_found = 0

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        if event_type == "push":
            commits = event_data.get("commits", [])
            for c in commits:
                sha = c.get("id") or c.get("sha", "")
                if sha:
                    author_name = "unknown"
                    if isinstance(c.get("author"), dict):
                        author_name = c["author"].get("name") or c["author"].get("username") or "unknown"
                    elif c.get("author"):
                        author_name = str(c.get("author"))

                    summary_msg = _mask_secrets((c.get("message") or "Push commit").split("\n")[0])
                    commit_url = c.get("url") or f"https://github.com/{repo_name}/commit/{sha}"

                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO github_activity_log
                        (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed)
                        VALUES (?, 'commit', ?, ?, ?, ?, ?, 0)
                        """,
                        (repo_name, sha, author_name, summary_msg, commit_url, now_iso),
                    )
                    # Not recorded in github_scanned_commits: push payloads carry file names and
                    # messages, not full diffs, so the next sync must still scan this SHA's content.
                    processed_count += 1

                    # Secret scanning on webhook commit payload content / patch / diff / message
                    from integrations.github_watch import SecretScanner
                    scanner = SecretScanner()
                    diff_text = ""
                    if "patch" in c:
                        diff_text += str(c["patch"]) + "\n"
                    if "diff" in c:
                        diff_text += str(c["diff"]) + "\n"
                    if "added" in c and isinstance(c["added"], list):
                        diff_text += "\n".join(str(x) for x in c["added"]) + "\n"
                    if "modified" in c and isinstance(c["modified"], list):
                        diff_text += "\n".join(str(x) for x in c["modified"]) + "\n"
                    if "message" in c:
                        diff_text += str(c["message"]) + "\n"

                    if diff_text:
                        findings = scanner.scan_diff(diff_text)
                        for f in findings:
                            f_path = f.get("filename") or f.get("file_path", "webhook_commit")
                            l_num = f.get("line_number", 0)
                            p_type = f.get("secret_type") or f.get("pattern_type", "High Entropy Candidate Secret")
                            red_val = f.get("redacted_value") or f.get("redacted_preview", "[REDACTED]")

                            if record_secret_finding(cursor, repo_name, sha, f_path, l_num, p_type, red_val):
                                secrets_found += 1

        elif event_type == "pull_request":
            pr = event_data.get("pull_request", {})
            pr_num = str(pr.get("number") or event_data.get("number", ""))
            if pr_num:
                pr_user = pr.get("user", {})
                author_login = pr_user.get("login") if isinstance(pr_user, dict) else "unknown"
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO github_activity_log
                    (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed)
                    VALUES (?, 'pr', ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        repo_name,
                        pr_num,
                        author_login,
                        _mask_secrets(pr.get("title", "Pull Request")),
                        pr.get("html_url", ""),
                        now_iso,
                    ),
                )
                processed_count += 1
        conn.commit()

    if secrets_found:
        security_score.recompute_security_score(db_path)

    return {
        "status": "processed",
        "processed_count": processed_count,
        "secrets_found": secrets_found,
        "repo_name": repo_name,
    }


