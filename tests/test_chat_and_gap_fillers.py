"""
Unit and Integration Tests for Documented Gaps:
- GitHub repository discovery & activity scanning (integrations/github_watch.py)
- Telemetry findings persistence into SQLite database tables (services/real_integrations.py)
- Conversational /chat endpoint intent routing & tone application (main.py)
"""

import os
import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import keyring_utils
import services.real_integrations as real_integrations
import integrations.github_watch as gw
from main import app, init_db

TEST_DB = "test_chat_gaps_evie.db"


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


class _FakeTwoFactorAuditor:
    def audit_github_2fa(self, token_key="github_token", timeout=10.0):
        return {"provider": "github", "2fa_enabled": True, "status": "success"}

    def audit_google_2fa(self):
        return {"provider": "google", "2fa_enabled": None, "status": "deferred"}


def test_discover_user_repositories(monkeypatch):
    import sys
    from types import ModuleType

    monkeypatch.setattr(gw, "get_credential", lambda service, key: "mock_pat")

    class MockRepo:
        def __init__(self, full_name):
            self.full_name = full_name

    class MockUser:
        def get_repos(self):
            return [MockRepo("user/repo1"), MockRepo("org/repo2")]

    class MockGithub:
        def __init__(self, token):
            pass
        def get_user(self):
            return MockUser()

    mock_mod = ModuleType("github")
    mock_mod.Github = MockGithub
    monkeypatch.setitem(sys.modules, "github", mock_mod)

    repos = gw.discover_user_repositories("github_pat")
    assert len(repos) == 2
    assert "user/repo1" in repos
    assert "org/repo2" in repos


def test_scan_github_repository_activity(monkeypatch):
    import sys
    from types import ModuleType

    monkeypatch.setattr(gw, "get_credential", lambda service, key: "mock_pat")

    class MockCommitAuthor:
        name = "alice"

    class MockCommitObj:
        author = MockCommitAuthor()
        message = "Fix security vulnerability"

    class MockCommit:
        sha = "abc123456"
        commit = MockCommitObj()
        html_url = "https://github.com/user/repo/commit/abc123456"

    class MockUserObj:
        login = "bob"

    class MockPR:
        number = 42
        title = "Add OAuth verification"
        user = MockUserObj()
        html_url = "https://github.com/user/repo/pull/42"
        def get_reviews(self):
            return []

    class MockRepo:
        def get_commits(self):
            return [MockCommit()]
        def get_pulls(self, state="open"):
            return [MockPR()]

    class MockGithub:
        def __init__(self, token):
            pass
        def get_repo(self, repo_name):
            return MockRepo()

    mock_mod = ModuleType("github")
    mock_mod.Github = MockGithub
    monkeypatch.setitem(sys.modules, "github", mock_mod)

    activity = gw.scan_github_repository_activity("user/repo")
    assert len(activity["commits"]) == 1
    assert activity["commits"][0]["sha"] == "abc1234"
    assert len(activity["unreviewed_prs"]) == 1
    assert activity["unreviewed_prs"][0]["number"] == 42


def test_sync_all_real_integrations_persists_sqlite_tables(monkeypatch):
    monkeypatch.setattr(real_integrations, "refresh_google_oauth_token", lambda: "mock_token")
    # Complete the sync_all mocks: no live Gmail sync or GitHub 2FA audit in fast tests
    monkeypatch.setattr("services.gmail_intelligence.sync_gmail_intelligence", lambda db_path, access_token=None: {"synced_messages": 0, "status": "mocked"})
    monkeypatch.setattr(real_integrations, "TwoFactorAuditor", _FakeTwoFactorAuditor)
    def mock_sync_gh(db_path="evie.db", repos=None):
        from services.github_intelligence import init_github_tables
        init_github_tables(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO secret_findings
                (repo_name, commit_sha, file_path, line_number, pattern_type, redacted_preview, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("user/test_repo", "abc1234", "config.py", 10, "AWS Access Key ID", "AK...12", "open"),
            )
        return {
            "status": "success",
            "repositories_discovered": 1,
            "repositories_scanned": 1,
            "repositories_synced": 1,
            "commits_synced": 1,
            "secret_findings_count": 1,
        }

    monkeypatch.setattr(
        "services.github_intelligence.sync_github_intelligence",
        mock_sync_gh,
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_hibp_breaches",
        lambda email: [{"name": "MockBreach", "breach_date": "2024-01-01", "data_classes": ["Passwords"]}],
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_google_drive_exposures",
        lambda drive_service, trusted_domains=None: [{"resource_id": "file1", "resource_name": "PublicDoc", "exposure_type": "public"}],
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_google_calendar_exposures",
        lambda calendar_service, trusted_domains=None: [{"resource_id": "cal1", "resource_name": "SharedCal", "exposure_type": "public"}],
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_gmail_phishing_alerts",
        lambda messages=None: [{"message_id": "msg999", "subject": "URGENT Payment", "risk_score": "high"}],
    )

    res = real_integrations.sync_all_real_integrations(
        db_path=TEST_DB,
        github_repo="user/test_repo",
        drive_service=MagicMock(),
        calendar_service=MagicMock(),
        gmail_messages=[{"id": "msg999"}],
    )

    assert res["status"] == "success"

    with sqlite3.connect(TEST_DB) as conn:
        # Check secret findings persisted
        cur = conn.execute("SELECT COUNT(*) FROM secret_findings WHERE repo_name = 'user/test_repo'")
        assert cur.fetchone()[0] == 1

        # Check exposure findings (Gmail phishing + Drive + Calendar)
        cur = conn.execute("SELECT COUNT(*) FROM exposure_findings")
        assert cur.fetchone()[0] == 3

        # Check breach checks persisted
        cur = conn.execute("SELECT COUNT(*) FROM breach_checks")
        assert cur.fetchone()[0] == 1


def test_post_chat_endpoint_routing(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    client = TestClient(app)

    # 1. Greeting / Wake trigger
    resp = client.post("/chat", json={"message": "Good morning Evie!"})
    assert resp.status_code == 200
    data = resp.json()
    assert "Daily Briefing" in data["reply"]
    assert "briefing" in data

    # 2. Report query
    resp = client.post("/chat", json={"message": "What is the daily report?"})
    assert resp.status_code == 200
    data = resp.json()
    assert "Status Report" in data["reply"]

    # 3. Security Score query
    resp = client.post("/chat", json={"message": "What is my security health score?"})
    assert resp.status_code == 200
    data = resp.json()
    assert "Security Health Score" in data["reply"]

    # 4. Calendar Proposal intent
    resp = client.post("/chat", json={"message": "Propose event Security Review tomorrow at 10am"})
    assert resp.status_code == 200
    data = resp.json()
    assert "proposal" in data

    # 5. General DRS research query: DRS is paused and not a registered tool, so it is rejected
    resp = client.post("/chat", json={"message": "Should I learn Rust for backend services?"})
    assert resp.status_code == 200
    data = resp.json()
    assert "reply" in data
    assert "drs" not in data
    assert data["tool"]["status"] == "rejected"
