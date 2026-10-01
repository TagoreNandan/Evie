"""
Unit tests for Shared Briefing Aggregator Engine (services/briefing.py) & FastAPI Endpoints (main.py).
Tests BRIEF-01, BRIEF-02, BRIEF-03, BRIEF-04, partial failure resilience, credential safety, and GitHub event deduplication.
"""

import hmac
import hashlib
import sqlite3
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

import services.briefing as briefing
from main import app, DB_PATH


@pytest.fixture
def test_db(tmp_path):
    """
    Create a temporary SQLite database pre-populated with Phase 1 & 2 tables.
    """
    db_file = str(tmp_path / "test_evie.db")
    briefing.init_github_events_table(db_file)

    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_token TEXT UNIQUE,
                action_type TEXT NOT NULL,
                event_summary TEXT,
                start_time TEXT,
                end_time TEXT,
                description TEXT,
                event_id TEXT,
                confirmed_by_user INTEGER DEFAULT 0,
                status TEXT DEFAULT 'proposed',
                executed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS secret_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line_number INTEGER,
                pattern_type TEXT,
                redacted_preview TEXT,
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
                acknowledged INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exposure_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT,
                exposure_type TEXT,
                status TEXT DEFAULT 'open'
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
        conn.commit()

    return db_file


def test_generate_briefing_full_aggregation(test_db):
    """
    Verify complete briefing aggregation when all sources succeed.
    """
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO github_events (repo_name, event_type, actor, summary) VALUES (?, ?, ?, ?)",
            ("Evie", "push", "somespecies", "3 commits pushed"),
        )
        conn.execute(
            "INSERT INTO secret_findings (repo_name, commit_sha, file_path, redacted_preview) VALUES (?, ?, ?, ?)",
            ("Evie", "abc1234", "config.py", "AKIA...34"),
        )
        conn.execute(
            "INSERT INTO breach_checks (email_checked, breach_name, acknowledged) VALUES (?, ?, 0)",
            ("test@example.com", "TestBreach"),
        )
        conn.execute(
            "INSERT INTO twofa_audit (provider, twofa_enabled) VALUES ('github', 0)"
        )
        conn.commit()

    mock_calendar_events = [
        {"summary": "Team Standup", "start": "2026-09-19T10:00:00Z", "end": "2026-09-19T10:30:00Z"}
    ]

    with patch("keyring_utils.get_credential", return_value="mock_oauth_token"), patch(
        "services.briefing.fetch_today_calendar_events", return_value=mock_calendar_events
    ):
        result = briefing.generate_briefing(test_db, trigger="wake")

    assert result["trigger"] == "wake"
    assert result["partial_failures"] == []

    sections = result["sections"]
    assert sections["github_activity"]["count"] == 1
    assert sections["github_activity"]["events"][0]["summary"] == "3 commits pushed"

    assert sections["calendar_today"]["status"] == "ok"
    assert sections["calendar_today"]["event_count"] == 1
    assert sections["calendar_today"]["events"][0]["summary"] == "Team Standup"

    security = sections["security_findings"]
    assert security["open_secrets"] == 1
    assert security["unacknowledged_breaches"] == 1
    assert security["twofa_warnings"] == ["github"]


def test_generate_briefing_partial_calendar_failure(test_db):
    """
    Verify resilience when Calendar API raises an exception.
    Briefing must complete with safe error code for calendar, returning GitHub & Security sections cleanly.
    """
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO github_events (repo_name, event_type, actor, summary) VALUES (?, ?, ?, ?)",
            ("Evie", "push", "somespecies", "1 commit pushed"),
        )
        conn.commit()

    with patch("keyring_utils.get_credential", return_value="mock_oauth_token"), patch(
        "services.briefing.fetch_today_calendar_events", side_effect=Exception("HTTP 500 Connection Refused")
    ):
        result = briefing.generate_briefing(test_db, trigger="wake")

    assert len(result["partial_failures"]) == 1
    assert result["partial_failures"][0] == {"source": "google_calendar", "error_code": "CALENDAR_FETCH_FAILED"}

    calendar_sec = result["sections"]["calendar_today"]
    assert calendar_sec["status"] == "unavailable"
    assert calendar_sec["error_code"] == "CALENDAR_FETCH_FAILED"
    assert "HTTP 500 Connection Refused" not in str(result)

    assert result["sections"]["github_activity"]["count"] == 1


def test_generate_briefing_missing_credential(test_db):
    """
    Verify handling when Google Calendar OAuth credential is missing in keyring.
    """
    with patch("keyring_utils.get_credential", return_value=None):
        result = briefing.generate_briefing(test_db, trigger="wake")

    assert len(result["partial_failures"]) == 1
    assert result["partial_failures"][0] == {"source": "google_calendar", "error_code": "CREDENTIAL_MISSING"}
    assert result["sections"]["calendar_today"]["status"] == "unavailable"
    assert result["sections"]["calendar_today"]["error_code"] == "CREDENTIAL_MISSING"


def test_generate_briefing_github_deduplication(test_db):
    """
    Verify deterministic deduplication: events included in a briefing are marked included_in_briefing = 1
    and not re-announced in subsequent briefing reports.
    """
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO github_events (repo_name, event_type, actor, summary) VALUES (?, ?, ?, ?)",
            ("Evie", "push", "somespecies", "Initial push"),
        )
        conn.execute(
            "INSERT INTO github_events (repo_name, event_type, actor, summary) VALUES (?, ?, ?, ?)",
            ("Evie", "pull_request", "somespecies", "PR #1 opened"),
        )
        conn.commit()

    with patch("keyring_utils.get_credential", return_value=None):
        first_report = briefing.generate_briefing(test_db, trigger="wake")
        second_report = briefing.generate_briefing(test_db, trigger="wake")

    assert first_report["sections"]["github_activity"]["count"] == 2
    assert second_report["sections"]["github_activity"]["count"] == 0

    with sqlite3.connect(test_db) as conn:
        cur = conn.execute("SELECT count(*) FROM github_events WHERE included_in_briefing = 1")
        assert cur.fetchone()[0] == 2


# --- FastAPI Endpoint Tests ---

client = TestClient(app)


def test_webhook_github_valid_signature_and_idempotency(test_db, monkeypatch):
    """
    Test POST /webhooks/github:
    1. Valid HMAC signature processing.
    2. Idempotency using X-GitHub-Delivery header.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)
    secret = "secret_key_123"

    def mock_get_cred(service, key):
        if service == "evie_assistant" and key == "github_webhook_secret":
            return secret
        return None

    payload_bytes = b'{"repository":{"name":"Evie"},"sender":{"login":"somespecies"},"commits":[{"id":"1"}]}'
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": "delivery-uuid-001",
        "X-GitHub-Event": "push",
        "Content-Type": "application/json",
    }

    with patch("keyring_utils.get_credential", side_effect=mock_get_cred), patch(
        "services.briefing.fetch_today_calendar_events", return_value=[]
    ):
        # 1. First delivery -> processed
        res1 = client.post("/webhooks/github", content=payload_bytes, headers=headers)
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["status"] == "processed"
        assert data1["delivery_id"] == "delivery-uuid-001"

        # 2. Replayed delivery with same X-GitHub-Delivery -> already_processed
        res2 = client.post("/webhooks/github", content=payload_bytes, headers=headers)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["status"] == "already_processed"
        assert data2["delivery_id"] == "delivery-uuid-001"


def test_webhook_github_invalid_signature(test_db, monkeypatch):
    """
    Test POST /webhooks/github:
    Reject missing or invalid HMAC signature with HTTP 401.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)

    def mock_get_cred(service, key):
        if service == "evie_assistant" and key == "github_webhook_secret":
            return "secret_key_123"
        return None

    headers = {
        "X-Hub-Signature-256": "sha256=invalid_signature_hex",
        "X-GitHub-Delivery": "delivery-uuid-002",
        "X-GitHub-Event": "push",
        "Content-Type": "application/json",
    }

    with patch("keyring_utils.get_credential", side_effect=mock_get_cred):
        res = client.post("/webhooks/github", json={"repository": {"name": "Evie"}}, headers=headers)
        assert res.status_code == 401
        assert "UNAUTHORIZED_WEBHOOK" in res.json()["detail"]


def test_briefing_wake_and_on_demand_endpoints(test_db, monkeypatch):
    """
    Test GET /briefing/wake and GET /briefing/on-demand routes.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)

    with patch("keyring_utils.get_credential", return_value=None):
        res_wake = client.get("/briefing/wake")
        assert res_wake.status_code == 200
        assert res_wake.json()["trigger"] == "wake"

        res_demand = client.get("/briefing/on-demand")
        assert res_demand.status_code == 200
        assert res_demand.json()["trigger"] == "on_demand"

        res_full = client.get("/report/full")
        assert res_full.status_code == 200
        assert res_full.json()["trigger"] == "on_demand"
