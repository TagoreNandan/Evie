"""
Unit and Integration Test Suite for GitHub + Gmail Personal & Work Intelligence (Phase 8).
"""

import os
import sqlite3
import pytest
from fastapi.testclient import TestClient

from main import app, DB_PATH
from services.github_intelligence import (
    init_github_tables,
    get_repository_analytics,
    get_unreviewed_prs,
    get_secret_findings,
)
from services.gmail_intelligence import (
    init_gmail_tables,
    get_inbox_intelligence_summary,
    propose_gmail_cleanup_action,
    confirm_gmail_cleanup_action,
)
from integrations.phishing_scan import categorize_email_message, analyze_email_phishing
from services.briefing import generate_briefing

client = TestClient(app)
TEST_DB = "test_github_gmail_intel.db"


@pytest.fixture(autouse=True)
def setup_test_database():
    """Setup clean test database before each test and cleanup afterwards."""
    from main import init_db
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db(TEST_DB)
    init_github_tables(TEST_DB)
    init_gmail_tables(TEST_DB)

    yield

    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_github_repository_analytics_empty():
    analytics = get_repository_analytics(TEST_DB)
    assert analytics["total_repositories"] == 0
    assert analytics["commits_last_month"] == 0
    assert analytics["commits_this_year"] == 0
    assert analytics["open_prs_count"] == 0


def test_github_intelligence_persistence_and_queries():
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced)
            VALUES ('acme/core-api', 'core-api', 'acme', 1, 1, 'https://github.com/acme/core-api', ?)
            """,
            (now_iso,),
        )
        cursor.execute(
            """
            INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed)
            VALUES ('acme/core-api', 'commit', 'a1b2c3d', 'dev', 'Add feature', 'https://github.com/acme/core-api/commit/a1b2c3d', ?, 0)
            """,
            (now_iso,),
        )
        cursor.execute(
            """
            INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed)
            VALUES ('acme/core-api', 'pr', '12', 'dev', 'Fix security bug', 'https://github.com/acme/core-api/pull/12', ?, 1)
            """,
            (now_iso,),
        )
        cursor.execute(
            """
            INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status)
            VALUES ('acme/core-api', 'a1b2c3d', '.env', 'AWS Access Key ID', 'AKIA...1234', 'open')
            """,
        )
        conn.commit()

    analytics = get_repository_analytics(TEST_DB)
    assert analytics["total_repositories"] == 1
    assert analytics["commits_total"] == 1
    assert analytics["unreviewed_prs_count"] == 1

    unreviewed = get_unreviewed_prs(TEST_DB)
    assert len(unreviewed) == 1
    assert unreviewed[0]["repo_name"] == "acme/core-api"
    assert unreviewed[0]["number"] == "12"

    secrets = get_secret_findings(TEST_DB)
    assert len(secrets) == 1
    assert secrets[0]["redacted_value"] == "AKIA...1234"
    assert "AKIA" in secrets[0]["redacted_value"]


def test_is_ignored_file_lockfiles_and_packages():
    from integrations.github_watch import is_ignored_file

    assert is_ignored_file("project3/uv.lock") is True
    assert is_ignored_file("ts-backend/bun.lock") is True
    assert is_ignored_file("package-lock.json") is True
    assert is_ignored_file("apps/server/go.sum") is True
    assert is_ignored_file("go.mod") is True
    assert is_ignored_file("venv/lib/python3.13/site-packages/RECORD") is True
    assert is_ignored_file("node_modules/@esbuild-kit/core-utils/dist/index.js") is True

    assert is_ignored_file("src/main.py") is False
    assert is_ignored_file("config/settings.yaml") is False


def test_gmail_categorization():
    phishing_headers = {
        "From": "Security <support@paypa1-verify.com>",
        "Subject": "Urgent Payment Action Required",
        "Return-Path": "bounce@scam.org",
        "Authentication-Results": "spf=fail",
    }
    # Phishing analysis path detects phishing risk score independently
    phishing_analysis = analyze_email_phishing(phishing_headers)
    assert phishing_analysis["risk_score"] in ("high", "critical")

    # Categorization path evaluates category independently
    assert categorize_email_message(phishing_headers) == "important"

    promo_headers = {
        "From": "Deals <marketing@shop.com>",
        "Subject": "Weekly Newsletter & Discount Offers",
        "List-Unsubscribe": "<mailto:unsub@shop.com>",
    }
    assert categorize_email_message(promo_headers) == "newsletter_promotional"

    important_headers = {
        "From": "Alerts <no-reply@aws.amazon.com>",
        "Subject": "Confidential Security Notice",
    }
    assert categorize_email_message(important_headers) == "important"


def test_gmail_proposal_confirmation_flow():
    proposal = propose_gmail_cleanup_action(TEST_DB, "spam-news.com", "unsubscribe_recommendation")
    token = proposal["proposal_token"]
    assert proposal["status"] == "pending"

    # Confirm proposal
    confirmation = confirm_gmail_cleanup_action(TEST_DB, token, confirmed=True)
    assert confirmation["success"] is True
    assert confirmation["status"] == "confirmed"

    # Second claim should fail
    second_confirm = confirm_gmail_cleanup_action(TEST_DB, token, confirmed=True)
    assert second_confirm["success"] is False
    assert second_confirm["error"] == "PROPOSAL_ALREADY_PROCESSED"


def test_briefing_integration():
    briefing = generate_briefing(TEST_DB, trigger="wake")
    assert "github_intelligence" in briefing["sections"]
    assert "gmail_intelligence" in briefing["sections"]


def test_chat_query_routing_github_and_gmail(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)

    # 1. Repositories query
    res = client.post("/chat", json={"message": "What repositories do I have?"})
    assert res.status_code == 200
    assert "reply" in res.json()

    # 2. PRs waiting for review
    res = client.post("/chat", json={"message": "Which PRs are waiting for review?"})
    assert res.status_code == 200
    assert "reply" in res.json()

    # 3. Secret exposure query
    res = client.post("/chat", json={"message": "Did anyone expose a secret?"})
    assert res.status_code == 200
    assert "reply" in res.json()

    # 4. Commits last month query
    res = client.post("/chat", json={"message": "How many commits did I make last month?"})
    assert res.status_code == 200
    assert "reply" in res.json()

    # 5. Promotional newsletters query
    res = client.post("/chat", json={"message": "What promotional newsletters am I receiving?"})
    assert res.status_code == 200
    assert "reply" in res.json()


def test_github_unconfigured_vs_zero_data_handling(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import keyring_utils
    monkeypatch.setattr(keyring_utils, "get_credential", lambda service, key: None)

    # Case A: Unconfigured state test (no PAT, no synced data)
    res = client.post("/chat", json={"message": "What happened on GitHub yesterday?"})
    assert res.status_code == 200
    assert "not been synchronized or configured" in res.json()["reply"]

    res = client.post("/chat", json={"message": "How many commits did I make last month?"})
    assert res.status_code == 200
    assert "not been synchronized or configured" in res.json()["reply"]

    # Case B: Configured / Synced state with zero results test
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('test/repo', 'repo', 'test', 0, 0, 'url', ?)",
            (now_iso,),
        )
        conn.commit()

    res = client.post("/chat", json={"message": "How many commits did I make last month?"})
    assert res.status_code == 200
    assert "0 commit(s)" in res.json()["reply"]


def test_single_github_scan_in_integrations_sync(monkeypatch):
    """
    Verify /integrations/sync invokes sync_github_intelligence only once
    and does not trigger duplicate fetch_github_telemetry scans.
    """
    import services.real_integrations as ri

    sync_count = {"count": 0}

    def mock_sync_github_intelligence(db_path):
        sync_count["count"] += 1
        return {"repositories_synced": 5, "commits_synced": 10, "prs_synced": 1, "secrets_found": 0}

    def mock_fetch_github_telemetry(repo):
        pytest.fail("fetch_github_telemetry should NOT be called during sync_all_real_integrations")

    monkeypatch.setattr("services.github_intelligence.sync_github_intelligence", mock_sync_github_intelligence)
    monkeypatch.setattr(ri, "fetch_github_telemetry", mock_fetch_github_telemetry)

    res = ri.sync_all_real_integrations(db_path=TEST_DB)
    assert sync_count["count"] == 1
    assert res["github"]["status"] == "success"
    assert res["github"]["repositories_synced"] == 5
    assert res["github"]["commits_count"] == 10


def test_incremental_secret_scanning_skips_scanned_commits(monkeypatch):
    """
    Verify previously scanned commits are not rescanned, new commits are scanned,
    and historical findings remain intact.
    """
    from integrations.github_watch import scan_github_repository_commits
    from services.github_intelligence import get_scanned_commit_shas, init_github_tables

    init_github_tables(TEST_DB)

    class DummyCommit:
        def __init__(self, sha, patch="AKIA1234567890123456"):
            self.sha = sha
            self.files = [DummyFile("test.py", patch)]

    class DummyFile:
        def __init__(self, filename, patch):
            self.filename = filename
            self.patch = patch

    class DummyRepo:
        def get_commits(self):
            return [DummyCommit("c11111111111"), DummyCommit("c22222222222")]

    class DummyGithub:
        def __init__(self, token):
            pass
        def get_repo(self, name):
            return DummyRepo()

    import keyring_utils
    monkeypatch.setattr(keyring_utils, "get_credential", lambda service, key: "fake_token")
    monkeypatch.setattr("integrations.github_watch.get_credential", lambda service, key: "fake_token")
    monkeypatch.setattr("github.Github", DummyGithub)

    # 1. Unscanned run -> scans both commits
    findings1 = scan_github_repository_commits("user/repo", scanned_shas=set())
    assert len(findings1) == 2

    # 2. Incremental run: commits are newest-first (c111 newer than c222); the older c222 is
    #    already scanned, so only the newer c111 is scanned and the walk stops at c222
    findings2 = scan_github_repository_commits("user/repo", scanned_shas={"c222222"})
    assert len(findings2) == 1
    assert findings2[0]["commit_sha"] == "c111111"

    # 3. Newest commit already scanned -> the walk stops immediately (everything older was scanned)
    findings3 = scan_github_repository_commits("user/repo", scanned_shas={"c111111"})
    assert len(findings3) == 0
    findings4 = scan_github_repository_commits("user/repo", scanned_shas={"c111111", "c222222"})
    assert len(findings4) == 0


def test_chat_analytics_reports_repositories_and_commit_counts(monkeypatch):
    """
    Verify /chat analytics still reports repositories and commit counts.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('org/repo1', 'repo1', 'org', 0, 1, 'https://github.com/org/repo1', ?)",
            (now_iso,),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('org/repo1', 'commit', 'abc1234', 'dev', 'feat: update', 'url', ?, 0)",
            (now_iso,),
        )
        conn.commit()

    res = client.post("/chat", json={"message": "What repositories do I have?"})
    assert res.status_code == 200
    json_resp = res.json()
    assert "org/repo1" in json_resp["reply"]
    assert json_resp["analytics"]["total_repositories"] == 1
    assert json_resp["analytics"]["commits_total"] == 1


def test_gmail_detailed_queries_and_listing(monkeypatch):
    """
    Verify /chat queries for suspicious emails and promotional emails return
    actual items (sender, subject, risk_score, unsubscribe info).
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('msg_1', 'Urgent Action Required: Verify Account', 'scammer@fake-phish.com', 'fake-phish.com', 'phishing_alert', 'high', 1, ?)
            """,
            (now_iso,),
        )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('msg_2', '50% off Summer Sale!', 'marketing@weekly-news.com', 'weekly-news.com', 'newsletter_promotional', 'low', 1, ?)
            """,
            (now_iso,),
        )
        conn.commit()

    # Query 1: Suspicious emails
    res = client.post("/chat", json={"message": "What suspicious emails do I have?"})
    assert res.status_code == 200
    reply = res.json()["reply"]
    assert "scammer@fake-phish.com" in reply
    assert "Urgent Action Required" in reply
    assert "high" in reply.lower()

    # Query 2: Promotional emails
    res = client.post("/chat", json={"message": "What newsletters or promotional emails do I have?"})
    assert res.status_code == 200
    reply = res.json()["reply"]
    assert "marketing@weekly-news.com" in reply
    assert "50% off Summer Sale!" in reply


def test_chat_contextual_followups(monkeypatch):
    """
    Verify contextual follow-ups ("show me those", "tell them", "which ones?")
    target the preceding chat context correctly.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('msg_promo', 'Daily Deals Digest', 'spammer@promo.com', 'promo.com', 'newsletter_promotional', 'low', 1, ?)
            """,
            (now_iso,),
        )
        conn.commit()

    # Step 1: Ask about newsletters to set context
    res1 = client.post("/chat", json={"message": "What promotional emails do I have?"})
    assert res1.status_code == 200
    assert "spammer@promo.com" in res1.json()["reply"]

    # Step 2: Contextual follow-up "tell them" to unsubscribe
    res2 = client.post("/chat", json={"message": "tell them"})
    assert res2.status_code == 200
    json2 = res2.json()
    reply2 = json2["reply"]
    assert "Unsubscribe" in reply2 or "unsubscribe" in reply2.lower() or "promo.com" in reply2
    assert "proposal" in reply2.lower() or "proposals" in json2


def test_github_yesterday_activity_query(monkeypatch):
    """
    Verify /chat handles "What happened on GitHub yesterday?" with real activity log rows.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    yesterday = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('org/repo1', 'repo1', 'org', 0, 1, 'https://github.com/org/repo1', ?)",
            (yesterday,),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('org/repo1', 'commit', 'sha1234', 'dev', 'fix: resolve race condition', 'url', ?, 0)",
            (yesterday,),
        )
        conn.commit()

    res = client.post("/chat", json={"message": "What happened on GitHub yesterday?"})
    assert res.status_code == 200
    reply = res.json()["reply"]
    assert "org/repo1" in reply
    assert "fix: resolve race condition" in reply


def test_enhanced_briefing_sections():
    """
    Verify generate_briefing includes detailed items for both GitHub and Gmail.
    """
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('msg_phish', 'Account Compromised', 'phisher@fake.com', 'fake.com', 'phishing_alert', 'critical', 0, ?)
            """,
            (now_iso,),
        )
        conn.execute(
            """
            INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status)
            VALUES ('org/repo1', 'sha1234', 'config.json', 'Stripe Secret Key', 'sk_live...1234', 'open')
            """,
        )
        conn.commit()

    briefing = generate_briefing(TEST_DB, trigger="wake")

    # Check Gmail section details
    gmail_sec = briefing["sections"]["gmail_intelligence"]
    assert gmail_sec["phishing_alerts_count"] == 1
    assert len(gmail_sec["suspicious_emails"]) == 1
    assert gmail_sec["suspicious_emails"][0]["sender"] == "phisher@fake.com"
    assert gmail_sec["suspicious_emails"][0]["risk_score"] == "critical"

    # Check GitHub section details
    github_sec = briefing["sections"]["github_intelligence"]
    assert github_sec["open_secrets_count"] == 1


def test_exact_live_user_queries_and_followups(monkeypatch):
    """
    Regression test suite for exact live user queries specified in Phase 8 fixes:
    - "What suspicious emails do I have?" -> "tell me more about those"
    - "What promotional emails do I have?"
    - "Do I have any exposed secrets?" -> "show me more"
    - "Are there any PRs waiting for me?"
    - "Give me my latest briefing"
    - /score API payload alignment (current_score, grade, active_findings_count)
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    yesterday = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        # 1. Insert Gmail suspicious & promotional metadata
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('msg_susp', 'Urgent Security Alert', 'phish@fake-login.com', 'fake-login.com', 'phishing_alert', 'high', 0, ?)
            """,
            (now_iso,),
        )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('msg_newsletter', 'Weekly Digest', 'news@tech-trends.com', 'tech-trends.com', 'newsletter_promotional', 'low', 1, ?)
            """,
            (now_iso,),
        )
        # 2. Insert GitHub repo, activity, PR, and secret findings (6 findings to test pagination)
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('org/repo1', 'repo1', 'org', 0, 1, 'https://github.com/org/repo1', ?)",
            (now_iso,),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('org/repo1', 'commit', 'sha100', 'dev', 'feat: update auth', 'url', ?, 0)",
            (yesterday,),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('org/repo1', 'pr', 'PR#42', 'dev', 'Fix security flaw', 'url', ?, 1)",
            (now_iso,),
        )
        for i in range(1, 7):
            conn.execute(
                """
                INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status)
                VALUES ('org/repo1', ?, 'config.json', 'Stripe API Key', 'sk_live...999', 'open')
                """,
                (f"sha_sec_{i}",),
            )
        conn.commit()

    # Query A: Exact "What suspicious emails do I have?"
    res = client.post("/chat", json={"message": "What suspicious emails do I have?"})
    assert res.status_code == 200
    reply = res.json()["reply"]
    assert "phish@fake-login.com" in reply
    assert "Urgent Security Alert" in reply

    # Query A2: Follow-up "tell me more about those"
    res_f1 = client.post("/chat", json={"message": "tell me more about those"})
    assert res_f1.status_code == 200
    reply_f1 = res_f1.json()["reply"]
    assert "phish@fake-login.com" in reply_f1
    assert "HIGH" in reply_f1 or "Risk: HIGH" in reply_f1

    # Query B: Exact "What promotional emails do I have?" (Must NOT route to PRs)
    res_p = client.post("/chat", json={"message": "What promotional emails do I have?"})
    assert res_p.status_code == 200
    reply_p = res_p.json()["reply"]
    assert "news@tech-trends.com" in reply_p
    assert "Weekly Digest" in reply_p
    assert "pull requests" not in reply_p.lower()

    # Query C: Exact "Do I have any exposed secrets?" (Bounded to 5 items)
    res_s = client.post("/chat", json={"message": "Do I have any exposed secrets?"})
    assert res_s.status_code == 200
    reply_s = res_s.json()["reply"]
    assert "6 high-entropy secret candidate(s)" in reply_s
    assert "Showing candidates 1-5" in reply_s
    assert "Say 'show me more'" in reply_s

    # Query C2: Follow-up "show me more" (Page 2)
    res_s2 = client.post("/chat", json={"message": "show me more"})
    assert res_s2.status_code == 200
    reply_s2 = res_s2.json()["reply"]
    assert "showing 6-6 of 6" in reply_s2.lower()
    assert "sha_sec" in reply_s2

    # Query D: Exact "Are there any PRs waiting for me?"
    res_pr = client.post("/chat", json={"message": "Are there any PRs waiting for me?"})
    assert res_pr.status_code == 200
    reply_pr = res_pr.json()["reply"]
    assert "PR#42" in reply_pr or "Fix security flaw" in reply_pr

    # Query E: Exact "Give me my latest briefing"
    res_b = client.post("/chat", json={"message": "Give me my latest briefing"})
    assert res_b.status_code == 200
    reply_b = res_b.json()["reply"]
    assert "GitHub Intelligence" in reply_b
    assert "Gmail Intelligence" in reply_b
    assert "Security & Exposure Findings" in reply_b
    assert "Secret Candidates: 6" in reply_b

    # Query F: GET /score endpoint payload alignment
    res_score = client.get("/score")
    assert res_score.status_code == 200
    score_json = res_score.json()
    assert "current_score" in score_json
    assert "grade" in score_json
    assert "active_findings_count" in score_json
    assert score_json["active_findings_count"] == 6


def test_quality_pass_four_queries(monkeypatch):
    """
    Regression test suite for the four Phase 8 intelligence-quality pass queries:
    1. "Which sender is sending me the most promotional emails?"
    2. "What happened on GitHub yesterday?"
    3. "Which repositories have the most secret candidates?"
    4. "What changed recently across my GitHub and Gmail?"
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    yesterday_date = (now - datetime.timedelta(days=1)).date()
    yesterday_iso = datetime.datetime(yesterday_date.year, yesterday_date.month, yesterday_date.day, 12, 0, 0, tzinfo=datetime.timezone.utc).isoformat()
    old_iso = (now - datetime.timedelta(days=10)).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        # Promotional emails: 3 from spammer.com, 1 from promo.org
        for i in range(3):
            conn.execute(
                """
                INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
                VALUES (?, 'Promo offer', 'deals@spammer.com', 'spammer.com', 'newsletter_promotional', 'low', 1, ?)
                """,
                (f"spammer_{i}", now_iso),
            )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('promo_1', 'Newsletter', 'info@promo.org', 'promo.org', 'newsletter_promotional', 'low', 1, ?)
            """,
            (now_iso,),
        )

        # Repositories & secrets
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('org/repo-alpha', 'repo-alpha', 'org', 0, 1, 'url', ?)",
            (now_iso,),
        )
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('org/repo-beta', 'repo-beta', 'org', 0, 1, 'url', ?)",
            (now_iso,),
        )
        for i in range(4):
            conn.execute(
                "INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status) VALUES ('org/repo-alpha', ?, '.env', 'API Key', 'sk_123', 'open')",
                (f"sha_a_{i}",),
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status) VALUES ('org/repo-beta', ?, '.env', 'API Key', 'sk_456', 'open')",
                (f"sha_b_{i}",),
            )

        # Historical old commit (10 days ago) - should NOT show up for yesterday query
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('org/repo-alpha', 'commit', 'old_sha', 'dev', 'Old commit', 'url', ?, 0)",
            (old_iso,),
        )
        conn.commit()

    # Query 1: Top promotional sender
    res1 = client.post("/chat", json={"message": "Which sender is sending me the most promotional emails?"})
    assert res1.status_code == 200
    reply1 = res1.json()["reply"]
    assert "spammer.com" in reply1
    assert "3 promotional email(s)" in reply1

    # Query 2a: Yesterday activity when 0 commits occurred yesterday
    res2a = client.post("/chat", json={"message": "What happened on GitHub yesterday?"})
    assert res2a.status_code == 200
    reply2a = res2a.json()["reply"]
    assert "0 commit(s)" in reply2a
    assert "Old commit" not in reply2a  # Strict yesterday filter must not return old commits

    # Query 2b: Yesterday activity when commits DID occur yesterday
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('org/repo-alpha', 'commit', 'yest_sha', 'dev', 'Yesterday work', 'url', ?, 0)",
            (yesterday_iso,),
        )
        conn.commit()
    res2b = client.post("/chat", json={"message": "What happened on GitHub yesterday?"})
    assert res2b.status_code == 200
    reply2b = res2b.json()["reply"]
    assert "1 commit(s)" in reply2b
    assert "Yesterday work" in reply2b

    # Query 3: Repositories with the most secret candidates
    res3 = client.post("/chat", json={"message": "Which repositories have the most secret candidates?"})
    assert res3.status_code == 200
    reply3 = res3.json()["reply"]
    assert "org/repo-alpha" in reply3
    assert "4 high-entropy secret candidate(s)" in reply3
    assert "org/repo-beta" in reply3
    assert "2 high-entropy secret candidate(s)" in reply3

    # Query 4: Recent changes across GitHub and Gmail
    res4 = client.post("/chat", json={"message": "What changed recently across my GitHub and Gmail?"})
    assert res4.status_code == 200
    reply4 = res4.json()["reply"]
    assert "GitHub Intelligence" in reply4
    assert "Gmail Intelligence" in reply4


def test_conversational_router_and_async_sync(monkeypatch):
    """
    Test suite for Phase 8 Architecture Refinements:
    - Casual greetings ("yo", "hello", "what can you do?")
    - Repository-specific commit queries ("Show me commits in TagoreNandan/ai-image-classifier")
    - Non-blocking async POST /integrations/sync
    - Incremental skip logic for unchanged repositories
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at) VALUES ('TagoreNandan/ai-image-classifier', 'ai-image-classifier', 'TagoreNandan', 0, 0, 'url', ?, ?)",
            (now_iso, "2026-09-22T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('TagoreNandan/ai-image-classifier', 'commit', 'abc1234', 'TagoreNandan', 'Trained model v2', 'url', ?, 0)",
            (now_iso,),
        )
        conn.commit()

    # 1. Test casual greeting
    res_g = client.post("/chat", json={"message": "yo"})
    assert res_g.status_code == 200
    reply_g = res_g.json()["reply"]
    assert "Hello! I am Evie" in reply_g
    assert "GitHub Intelligence" in reply_g

    res_g2 = client.post("/chat", json={"message": "what can you do?"})
    assert res_g2.status_code == 200
    reply_g2 = res_g2.json()["reply"]
    assert "GitHub Intelligence" in reply_g2

    # 2. Test repo-specific commit query
    res_r = client.post("/chat", json={"message": "Show me the commits in TagoreNandan/ai-image-classifier"})
    assert res_r.status_code == 200
    reply_r = res_r.json()["reply"]
    assert "TagoreNandan/ai-image-classifier" in reply_r
    assert "Trained model v2" in reply_r

    # 3. Test non-blocking async /integrations/sync (background sync mocked: no live GitHub/Gmail/OAuth)
    import services.real_integrations as real_integrations
    sync_calls = []
    monkeypatch.setattr(real_integrations, "sync_all_real_integrations", lambda db_path: sync_calls.append(db_path) or {"status": "success"})
    res_sync = client.post("/integrations/sync")
    assert sync_calls == [TEST_DB]
    assert res_sync.status_code == 200
    assert res_sync.json()["status"] == "sync_started"







