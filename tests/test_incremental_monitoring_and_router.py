"""
Unit and Integration Test Suite for Incremental Monitoring, Event Webhooks, and NLU Router (Phase 8 Refactor).
"""

import os
import sqlite3
import pytest
from fastapi.testclient import TestClient

from main import app, DB_PATH
from services.conversational_router import parse_user_intent, execute_grounded_query
from services.github_intelligence import (
    init_github_tables,
    sync_github_intelligence,
    process_github_webhook_event,
)
from services.gmail_intelligence import (
    init_gmail_tables,
    sync_gmail_intelligence,
    get_gmail_sync_checkpoint,
    update_gmail_sync_checkpoint,
)
from security_score import compute_security_score, init_score_history_table

client = TestClient(app)
TEST_DB = "test_incremental_router_evie.db"


@pytest.fixture(autouse=True)
def setup_test_db():
    from main import init_db
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db(TEST_DB)
    init_github_tables(TEST_DB)
    init_gmail_tables(TEST_DB)

    yield

    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_nlu_intent_routing_variations(monkeypatch):
    """
    GOAL 1: Test that diverse natural language query variations resolve to appropriate grounded intelligence handlers.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('TagoreNandan/TagoreNandan', 'TagoreNandan', 'TagoreNandan', 0, 0, 'url', ?)",
            (now_iso,),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('TagoreNandan/TagoreNandan', 'commit', 'sha_tn1', 'dev', 'feat: update ML pipeline', 'url', ?, 0)",
            (now_iso,),
        )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('m1', 'Weekly Digest', 'news@site.com', 'site.com', 'newsletter_promotional', 'low', 1, ?)
            """,
            (now_iso,),
        )
        conn.commit()

    # Query 1: "Show me the commits in TagoreNandan/TagoreNandan"
    res1 = client.post("/chat", json={"message": "Show me the commits in TagoreNandan/TagoreNandan"})
    assert res1.status_code == 200
    assert "TagoreNandan/TagoreNandan" in res1.json()["reply"]
    assert "feat: update ML pipeline" in res1.json()["reply"]

    # Query 2: "What did I change in TagoreNandan/TagoreNandan recently?"
    res2 = client.post("/chat", json={"message": "What did I change in TagoreNandan/TagoreNandan recently?"})
    assert res2.status_code == 200
    assert "TagoreNandan/TagoreNandan" in res2.json()["reply"]

    # Query 3: "What happened in my inbox recently?"
    res3 = client.post("/chat", json={"message": "What happened in my inbox recently?"})
    assert res3.status_code == 200
    assert "Gmail Inbox Intelligence Summary" in res3.json()["reply"]

    # Query 4: "Anything important in my email?"
    res4 = client.post("/chat", json={"message": "Anything important in my email?"})
    assert res4.status_code == 200
    assert "Important Email Messages" in res4.json()["reply"] or "Gmail" in res4.json()["reply"]

    # Query 5: "What's new in Gmail?"
    res5 = client.post("/chat", json={"message": "What's new in Gmail?"})
    assert res5.status_code == 200
    assert "Gmail Inbox Intelligence Summary" in res5.json()["reply"]


def test_conversational_fallback_no_drs(monkeypatch):
    """
    GOAL 2: Test that conversational queries (greetings, identity, general status) do not invoke DRS or produce "Insufficient search evidence found".
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)

    for msg in ["yo", "who are you", "what can you do", "help", "hello evie"]:
        res = client.post("/chat", json={"message": msg})
        assert res.status_code == 200
        reply = res.json()["reply"]
        assert "Insufficient search evidence" not in reply
        assert "Evie" in reply or "GitHub" in reply or "Gmail" in reply


def test_github_incremental_skip_and_webhook(monkeypatch):
    """
    GOAL 3 & GOAL 5: Test that unchanged repositories skip commit processing, and webhook events trigger targeted processing.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    pushed_time = "2026-09-22T10:00:00Z"

    # Pre-populate cached repo with pushed_at
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at) VALUES ('org/repo-cached', 'repo-cached', 'org', 0, 0, 'url', ?, ?)",
            (pushed_time, pushed_time),
        )
        conn.commit()

    # Mock discover_user_repositories_detailed returning the exact same pushed_at
    mock_repos = [
        {"full_name": "org/repo-cached", "name": "repo-cached", "owner": "org", "is_private": False, "is_org": False, "html_url": "url", "pushed_at": pushed_time}
    ]
    monkeypatch.setattr("services.github_intelligence.discover_user_repositories_detailed", lambda token_key="github_token": mock_repos)

    scan_called = False
    def mock_scan(*args, **kwargs):
        nonlocal scan_called
        scan_called = True
        return {}

    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity", mock_scan)

    # Execute sync
    res = sync_github_intelligence(TEST_DB)
    assert res["repositories_synced"] == 1
    assert not scan_called, "Unchanged repository must skip activity scanning API calls"

    # Test Webhook Event processing
    event_payload = {
        "event_type": "push",
        "repository": {"full_name": "org/repo-cached"},
        "commits": [
            {"id": "sha_wh_123", "message": "fix: webhook commit", "author": {"name": "alice"}, "url": "https://github.com/org/repo-cached/commit/sha_wh_123"}
        ]
    }
    wh_res = process_github_webhook_event(event_payload, TEST_DB)
    assert wh_res["status"] == "processed"
    assert wh_res["processed_count"] == 1

    # Verify commit row was inserted directly
    with sqlite3.connect(TEST_DB) as conn:
        cur = conn.execute("SELECT summary FROM github_activity_log WHERE identifier = 'sha_wh_123'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "fix: webhook commit"


def test_gmail_checkpoint_incremental(monkeypatch):
    """
    GOAL 4 & GOAL 5: Test Gmail sync checkpoint read/write and incremental metadata persistence.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)

    # Initial checkpoint state
    cp = get_gmail_sync_checkpoint(TEST_DB, account_id="primary")
    assert cp is None

    # Update checkpoint
    update_gmail_sync_checkpoint(TEST_DB, account_id="primary", history_id="12345", last_message_id="msg_999")
    cp_updated = get_gmail_sync_checkpoint(TEST_DB, account_id="primary")
    assert cp_updated is not None
    assert cp_updated["last_history_id"] == "12345"
    assert cp_updated["last_message_id"] == "msg_999"


def test_security_score_ui_contract_stability(monkeypatch):
    """
    GOAL 7: Test GET /score endpoint payload contract and numeric score evaluation consistency.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)

    res = client.get("/score")
    assert res.status_code == 200
    data = res.json()
    assert "score" in data
    assert "current_score" in data
    assert "grade" in data
    assert "active_findings_count" in data
    assert isinstance(data["score"], (int, float))
    assert data["score"] == data["current_score"]


def test_extract_repository_entity_regression():
    """
    Regression test: Ensure 'recently', 'today', 'yesterday' are never extracted as repository names.
    """
    from services.conversational_router import extract_repository_entity

    # Case 1: "Did anything change in my TagoreNandan repository recently?"
    repo1 = extract_repository_entity("Did anything change in my TagoreNandan repository recently?")
    assert repo1 == "TagoreNandan", f"Expected 'TagoreNandan', got '{repo1}'"

    # Case 2: "What changed in repo TagoreNandan today?"
    repo2 = extract_repository_entity("What changed in repo TagoreNandan today?")
    assert repo2 == "TagoreNandan", f"Expected 'TagoreNandan', got '{repo2}'"

    # Case 3: "Any commits in my main repo recently?"
    repo3 = extract_repository_entity("Any commits in my main repo recently?")
    assert repo3 is None, f"Expected None for 'main repo recently', got '{repo3}'"


def test_all_live_failure_query_variations(monkeypatch):
    """
    Test all natural language query variations listed in live failure prompt:
    GitHub, Gmail, Cross-source, and Security posture queries.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced) VALUES ('TagoreNandan/ai-image', 'ai-image', 'TagoreNandan', 0, 0, 'url', ?)",
            (now_iso,),
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp, is_unreviewed) VALUES ('TagoreNandan/ai-image', 'commit', 'sha_img1', 'dev', 'feat: train model', 'url', ?, 0)",
            (now_iso,),
        )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, has_unsubscribe, received_at)
            VALUES ('m_imp1', 'Urgent Security Update Needed', 'security@google.com', 'google.com', 'important', 'low', 0, ?)
            """,
            (now_iso,),
        )
        conn.commit()

    # 1. GitHub Queries
    g_queries = [
        "What did I change recently?",
        "Anything new in my main repo?",
        "Did I do anything yesterday?",
        "What have I been working on?",
        "Did anything change in my TagoreNandan repository recently?",
    ]
    for q in g_queries:
        res = client.post("/chat", json={"message": q})
        assert res.status_code == 200
        reply = res.json()["reply"]
        assert "Insufficient search evidence" not in reply
        assert "Commit" in reply or "GitHub" in reply or "Activity" in reply or "TagoreNandan" in reply or "repository" in reply

    # 2. Gmail Queries
    m_queries = [
        "What's new in my inbox?",
        "Did anyone send me anything important?",
        "Anything I should know from email?",
        "Did I get anything suspicious?",
    ]
    for q in m_queries:
        res = client.post("/chat", json={"message": q})
        assert res.status_code == 200
        reply = res.json()["reply"]
        assert "Insufficient search evidence" not in reply
        assert "Gmail" in reply or "Email" in reply or "Inbox" in reply or "Important" in reply or "suspicious" in reply

    # 3. Cross-source Queries
    c_queries = [
        "What changed today?",
        "What happened while I was away?",
        "Anything important since yesterday?",
        "Give me a quick update.",
        "Give me a quick rundown of what changed today.",
    ]
    for q in c_queries:
        res = client.post("/chat", json={"message": q})
        assert res.status_code == 200
        reply = res.json()["reply"]
        assert "Insufficient search evidence" not in reply
        assert "GitHub" in reply or "Gmail" in reply or "Evie Status Report" in reply or "Report" in reply

    # 4. Security Posture Queries
    s_queries = [
        "What's my security situation?",
        "How secure am I?",
        "What should I worry about?",
        "Give me my security status.",
    ]
    for q in s_queries:
        res = client.post("/chat", json={"message": q})
        assert res.status_code == 200
        reply = res.json()["reply"]
        assert "Insufficient search evidence" not in reply
        assert "Security Health Score" in reply or "Active Findings" in reply or "Grade" in reply

