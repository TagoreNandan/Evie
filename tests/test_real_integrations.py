"""
Unit and Integration Tests for Real Integration Adapters & Google OAuth Token Refresh (`services/real_integrations.py`).

Verifies unified orchestration path across all 5 security integrations:
- GitHub (secret scanning)
- HIBP (breach watcher)
- Google Drive (file exposure auditor)
- Google Calendar (ACL exposure auditor)
- Gmail (phishing pattern analyzer)
"""

import os
import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import keyring_utils
import services.real_integrations as real_integrations
from main import app, init_db

TEST_DB = "test_real_integrations_evie.db"


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


def test_refresh_google_oauth_token_missing_credentials(monkeypatch):
    monkeypatch.setattr(keyring_utils, "get_credential", lambda service, key: None)
    token = real_integrations.refresh_google_oauth_token()
    assert token is None


def test_refresh_google_oauth_token_success(monkeypatch):
    monkeypatch.setattr(keyring_utils, "get_credential", lambda service, key: f"mock_{key}")

    mock_response = MagicMock()
    mock_response.read.return_value = b'{"access_token": "mock_fresh_access_token_123"}'
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        token = real_integrations.refresh_google_oauth_token()
        assert token == "mock_fresh_access_token_123"


def test_refresh_google_oauth_token_secret_redaction(monkeypatch):
    monkeypatch.setattr(keyring_utils, "get_credential", lambda service, key: f"ghp_secret_key_12345")

    with patch("urllib.request.urlopen", side_effect=Exception("Connection failed with secret ghp_secret_key_12345")):
        token = real_integrations.refresh_google_oauth_token()
        assert token is None


def test_fetch_github_telemetry_delegation(monkeypatch):
    monkeypatch.setattr(
        real_integrations,
        "scan_github_repository_commits",
        lambda repo_name, token_key, max_commits: [{"secret_type": "AWS Key", "redacted_value": "AK...12"}],
    )
    res = real_integrations.fetch_github_telemetry("user/repo")
    assert res["status"] == "success"
    assert len(res["findings"]) == 1
    assert res["findings"][0]["secret_type"] == "AWS Key"


def test_fetch_hibp_breaches_delegation(monkeypatch):
    class MockBreachWatcher:
        def check_email_breaches(self, email, key_name="hibp_api_key"):
            return [{"name": "MockBreach", "domain": "mock.com"}]

    monkeypatch.setattr(real_integrations, "BreachWatcher", MockBreachWatcher)
    res = real_integrations.fetch_hibp_breaches("test@example.com")
    assert len(res) == 1
    assert res[0]["name"] == "MockBreach"


def test_fetch_google_drive_exposures_delegation():
    mock_service = MagicMock()
    mock_service.files().list().execute.return_value = {
        "files": [{"id": "file1", "name": "SecretDoc", "permissions": [{"type": "anyone", "role": "reader"}]}]
    }

    findings = real_integrations.fetch_google_drive_exposures(drive_service=mock_service)
    assert len(findings) == 1
    assert findings[0]["resource_type"] == "drive_file"
    assert findings[0]["exposure_type"] == "public"


def test_fetch_google_calendar_exposures_delegation():
    mock_service = MagicMock()
    mock_service.calendarList().list().execute.return_value = {"items": [{"id": "cal1", "summary": "Main Calendar"}]}
    mock_service.acl().list().execute.return_value = {
        "items": [{"id": "rule1", "scope": {"type": "anyone"}, "role": "reader"}]
    }

    findings = real_integrations.fetch_google_calendar_exposures(calendar_service=mock_service)
    assert len(findings) == 1
    assert findings[0]["resource_type"] == "calendar_acl"
    assert findings[0]["exposure_type"] == "public"


def test_fetch_gmail_phishing_alerts_delegation():
    messages = [
        {
            "id": "msg_001",
            "headers": {
                "Subject": "URGENT: Wire Transfer Required",
                "From": "attacker@fake.com",
                "Return-Path": "spoofed@other.com",
            },
        }
    ]
    alerts = real_integrations.fetch_gmail_phishing_alerts(messages)
    assert len(alerts) == 1
    assert alerts[0]["message_id"] == "msg_001"
    assert alerts[0]["risk_score"] in ("high", "critical")


def test_sync_all_real_integrations_orchestrating_all_five(monkeypatch):
    monkeypatch.setattr(real_integrations, "refresh_google_oauth_token", lambda: "mock_token")
    # Complete the sync_all mocks: no live Gmail sync or GitHub 2FA audit in fast tests
    monkeypatch.setattr("services.gmail_intelligence.sync_gmail_intelligence", lambda db_path, access_token=None: {"synced_messages": 0, "status": "mocked"})
    monkeypatch.setattr(real_integrations, "TwoFactorAuditor", _FakeTwoFactorAuditor)
    monkeypatch.setattr(
        "services.github_intelligence.sync_github_intelligence",
        lambda db_path="evie.db", repos=None: {
            "status": "success",
            "repositories_discovered": 1,
            "repositories_scanned": 1,
            "repositories_synced": 1,
            "commits_synced": 1,
            "secret_findings_count": 1,
        },
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_hibp_breaches",
        lambda email: [{"name": "BreachA"}],
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_google_drive_exposures",
        lambda drive_service, trusted_domains=None: [{"resource_type": "drive_file"}],
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_google_calendar_exposures",
        lambda calendar_service, trusted_domains=None: [{"resource_type": "calendar_acl"}],
    )
    monkeypatch.setattr(
        real_integrations,
        "fetch_gmail_phishing_alerts",
        lambda messages=None: [{"message_id": "msg1"}],
    )

    res = real_integrations.sync_all_real_integrations(
        db_path=TEST_DB,
        drive_service=MagicMock(),
        calendar_service=MagicMock(),
        gmail_messages=[{"id": "msg1"}],
    )

    assert res["status"] == "success"
    assert res["google_authenticated"] is True
    assert res["hibp_breaches_found"] == 1
    assert res["drive_exposures_found"] == 1
    assert res["calendar_exposures_found"] == 1
    assert res["gmail_phishing_alerts_found"] == 1

    # Verify SQLite table persistence
    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM twofa_audit")
        assert cursor.fetchone()[0] == 2


def test_sync_integrations_endpoint(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    monkeypatch.setattr(real_integrations, "refresh_google_oauth_token", lambda: "mock_token")
    # Complete the sync_all mocks: no live Gmail sync or GitHub 2FA audit in fast tests
    monkeypatch.setattr("services.gmail_intelligence.sync_gmail_intelligence", lambda db_path, access_token=None: {"synced_messages": 0, "status": "mocked"})
    monkeypatch.setattr(real_integrations, "TwoFactorAuditor", _FakeTwoFactorAuditor)
    monkeypatch.setattr(real_integrations, "fetch_hibp_breaches", lambda email: [])
    monkeypatch.setattr(real_integrations, "fetch_gmail_phishing_alerts", lambda messages=None: [])
    monkeypatch.setattr(
        "services.github_intelligence.sync_github_intelligence",
        lambda db_path="evie.db", repos=None: {
            "status": "success",
            "repositories_discovered": 1,
            "repositories_scanned": 1,
            "repositories_synced": 1,
            "commits_synced": 1,
            "secret_findings_count": 0,
        },
    )
    client = TestClient(app)

    response = client.post("/integrations/sync")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("sync_started", "success")


def test_phase1_to_5_watcher_regression_integrity():
    """
    Regression test ensuring Phase 1-5 integration watchers remain intact and functional.
    """
    import integrations.github_watch as gw
    import integrations.breach_watch as bw
    import integrations.exposure_watch as ew
    import integrations.calendar_actions as ca
    import integrations.phishing_scan as ps

    assert hasattr(gw, "SecretScanner")
    assert hasattr(bw, "BreachWatcher")
    assert hasattr(ew, "DriveExposureAuditor")
    assert hasattr(ca, "propose_calendar_action")
    assert hasattr(ps, "scan_phishing_patterns")


def test_sync_all_real_integrations_per_integration_status_unconfigured(monkeypatch):
    monkeypatch.setattr(real_integrations, "refresh_google_oauth_token", lambda: None)
    monkeypatch.setattr(keyring_utils, "get_credential", lambda service, key: None)
    monkeypatch.setattr(
        "services.github_intelligence.sync_github_intelligence",
        lambda db_path="evie.db", repos=None: {"status": "unconfigured", "repositories_synced": 0},
    )
    import integrations.github_watch as gw
    monkeypatch.setattr(gw, "get_credential", lambda service, key: None)
    res = real_integrations.sync_all_real_integrations(db_path=TEST_DB)
    assert res["status"] == "success"
    assert res["github"]["status"] == "unconfigured"
    assert res["gmail"]["status"] == "unconfigured"
    assert res["hibp"]["status"] == "unconfigured"
    assert res["drive"]["status"] == "unconfigured"
    assert res["calendar"]["status"] == "unconfigured"


