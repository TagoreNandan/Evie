"""
Unit tests for Phase C: Dashboard State API & User-Facing Security Language.
"""

import sqlite3
import pytest
from fastapi.testclient import TestClient
from main import app, init_db
from services.dashboard_state import get_dashboard_state, derive_overall_status

client = TestClient(app)
TEST_DB = "test_evie_phase_c.db"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    db_file = str(tmp_path / "test_evie_phase_c.db")
    init_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_repo_cache (
                full_name TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                is_private INTEGER NOT NULL,
                is_org INTEGER NOT NULL,
                html_url TEXT NOT NULL,
                last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_pushed_at TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                identifier TEXT NOT NULL,
                author TEXT,
                summary TEXT,
                html_url TEXT,
                timestamp TEXT NOT NULL,
                is_unreviewed INTEGER DEFAULT 0,
                UNIQUE(repo_name, activity_type, identifier)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gmail_metadata (
                message_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_domain TEXT NOT NULL,
                category TEXT NOT NULL,
                risk_score TEXT NOT NULL,
                phishing_confidence REAL,
                phishing_source TEXT,
                has_unsubscribe INTEGER DEFAULT 0,
                received_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    return db_file


def test_dashboard_state_empty_db_safe(setup_test_db, monkeypatch):
    """
    Verify GET /dashboard/state succeeds and returns valid defaults on an empty database.
    """
    monkeypatch.setattr("main.DB_PATH", setup_test_db)
    res = client.get("/dashboard/state")
    assert res.status_code == 200
    data = res.json()

    assert data["overall_status"] in ("Good", "Needs Attention", "Critical")
    assert "github_summary" in data
    assert "gmail_summary" in data
    assert "security_findings_summary" in data
    assert "phishing_alerts" in data
    assert "phishing_disclaimer" in data
    assert "promotional_summary" in data
    assert "recent_activity" in data


def test_dashboard_state_sections_and_status(setup_test_db, monkeypatch):
    """
    Verify GET /dashboard/state returns accurate github, gmail, and findings summaries.
    """
    monkeypatch.setattr("main.DB_PATH", setup_test_db)

    # Populate sample repos and commits
    with sqlite3.connect(setup_test_db) as conn:
        conn.execute(
            "INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url) VALUES ('acme/app', 'app', 'acme', 0, 0, 'https://github.com/acme/app')"
        )
        conn.execute(
            "INSERT INTO github_activity_log (repo_name, activity_type, identifier, author, summary, html_url, timestamp) VALUES ('acme/app', 'commit', 'sha123', 'alice', 'feat: init', 'url', '2026-09-27T10:00:00Z')"
        )
        # Add a promotional email and a suspicious phishing alert email
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, phishing_confidence, phishing_source, received_at)
            VALUES ('msg_promo_1', 'Weekly Newsletter', 'news@marketing.com', 'marketing.com', 'newsletter_promotional', 'low', 0.05, 'laya_noul', '2026-09-27T11:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, phishing_confidence, phishing_source, received_at)
            VALUES ('msg_phish_1', 'Urgent Account Action Needed', 'support@phish.net', 'phish.net', 'phishing_alert', 'high', 0.92, 'laya_noul', '2026-09-27T12:00:00Z')
            """
        )
        conn.commit()

    res = client.get("/dashboard/state")
    assert res.status_code == 200
    data = res.json()

    # Overall Status check
    assert data["overall_status"] in ("Good", "Needs Attention", "Critical")

    # GitHub Summary check
    github = data["github_summary"]
    assert github["total_repositories"] == 1
    assert github["commits_total"] == 1

    # Gmail Summary check
    gmail = data["gmail_summary"]
    assert gmail["total_emails_scanned"] == 2
    assert gmail["phishing_alert_count"] == 1
    assert gmail["promotional_count"] == 1

    # Recent Activity check
    assert len(data["recent_activity"]) == 1
    assert data["recent_activity"][0]["identifier"] == "sha123"


def test_phishing_language_and_disclaimers(setup_test_db, monkeypatch):
    """
    Verify phishing alert outputs contain clear heuristic/not-proof disclaimers
    and preserve distinction between promotional and phishing emails.
    """
    monkeypatch.setattr("main.DB_PATH", setup_test_db)

    with sqlite3.connect(setup_test_db) as conn:
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, phishing_confidence, phishing_source, received_at)
            VALUES ('msg_phish_2', 'Verify Banking Credentials', 'alert@bank-fake.com', 'bank-fake.com', 'phishing_alert', 'critical', 0.98, 'llm_classifier', '2026-09-27T13:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO gmail_metadata (message_id, subject, sender, sender_domain, category, risk_score, phishing_confidence, phishing_source, received_at)
            VALUES ('msg_promo_2', '50% Off Sale', 'deals@store.com', 'store.com', 'newsletter_promotional', 'low', 0.02, 'laya_noul', '2026-09-27T14:00:00Z')
            """
        )
        conn.commit()

    res = client.get("/dashboard/state")
    assert res.status_code == 200
    data = res.json()

    # Verify root phishing disclaimer
    assert "heuristic" in data["phishing_disclaimer"].lower()
    assert "not" in data["phishing_disclaimer"].lower() and "proof" in data["phishing_disclaimer"].lower()

    # Verify individual phishing alert disclaimer
    alerts = data["phishing_alerts"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["message_id"] == "msg_phish_2"
    assert "disclaimer" in alert
    assert "heuristic" in alert["disclaimer"].lower() or "not definitive proof" in alert["disclaimer"].lower()

    # Verify promotional summary remains distinct
    promo = data["promotional_summary"]
    assert promo["promotional_count"] == 1
    assert promo["emails"][0]["message_id"] == "msg_promo_2"


def test_derive_overall_status_logic():
    """
    Verify overall_status is derived strictly from existing score presentation mapping
    rather than independently from raw finding counts.
    """
    assert derive_overall_status(score=100) == "Good"
    assert derive_overall_status(score=85) == "Good"
    assert derive_overall_status(score=80) == "Good"
    assert derive_overall_status(score=79) == "Needs Attention"
    assert derive_overall_status(score=60) == "Needs Attention"
    assert derive_overall_status(score=50) == "Needs Attention"
    assert derive_overall_status(score=49) == "Critical"
    assert derive_overall_status(score=20) == "Critical"
    assert derive_overall_status(score=0) == "Critical"

