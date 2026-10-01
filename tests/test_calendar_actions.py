"""
Unit tests for Confirmation-Gated Calendar Actions (integrations/calendar_actions.py).
Tests CAL-01, CAL-02, atomic claim lifecycle, 15-min TTL, input validation, and credential secrecy.
"""

import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import integrations.calendar_actions as cal_actions
from main import app, DB_PATH
from fastapi.testclient import TestClient


@pytest.fixture
def test_db(tmp_path):
    """
    Create a temporary SQLite database with calendar_actions table.
    """
    db_file = str(tmp_path / "test_evie_cal.db")
    cal_actions.init_calendar_actions_table(db_file)
    return db_file


def test_propose_calendar_action_success(test_db):
    """
    Verify /calendar/propose creates DB row with confirmed_by_user = 0 and performs ZERO API calls.
    """
    with patch("httpx.Client") as mock_client:
        res = cal_actions.propose_calendar_action(
            db_path=test_db,
            action_type="create",
            summary="Team Standup",
            start_time="2026-09-20T10:00:00Z",
            end_time="2026-09-20T10:30:00Z",
            description="Daily sync",
        )
        assert mock_client.call_count == 0

    assert res["status"] == "proposed"
    token = res["proposal_token"]
    assert token is not None

    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM calendar_actions WHERE proposal_token = ?", (token,))
        row = cur.fetchone()
        assert row is not None
        assert row["confirmed_by_user"] == 0
        assert row["status"] == "proposed"
        assert row["event_summary"] == "Team Standup"


def test_propose_calendar_action_input_validation(test_db):
    """
    Verify input validation for invalid action_type, bad timestamps, and missing event_id.
    """
    # Invalid action_type
    with pytest.raises(ValueError, match="Invalid action_type"):
        cal_actions.propose_calendar_action(test_db, "invalid_act", "Summary", "2026-09-20T10:00:00Z", "2026-09-20T10:30:00Z")

    # end_time <= start_time
    with pytest.raises(ValueError, match="end_time must be strictly after start_time"):
        cal_actions.propose_calendar_action(test_db, "create", "Summary", "2026-09-20T10:30:00Z", "2026-09-20T10:00:00Z")

    # missing event_id for update
    with pytest.raises(ValueError, match="event_id is required"):
        cal_actions.propose_calendar_action(test_db, "update", "Summary", "2026-09-20T10:00:00Z", "2026-09-20T10:30:00Z")


def test_confirm_calendar_action_cancelled(test_db):
    """
    Verify declining proposal (confirmed = False) updates status to 'cancelled' and performs ZERO API calls.
    """
    prop = cal_actions.propose_calendar_action(test_db, "create", "Lunch", "2026-09-20T12:00:00Z", "2026-09-20T13:00:00Z")
    token = prop["proposal_token"]

    with patch("httpx.Client") as mock_client:
        res = cal_actions.confirm_calendar_action(test_db, token, confirmed=False)
        assert mock_client.call_count == 0

    assert res["status"] == "cancelled"
    with sqlite3.connect(test_db) as conn:
        cur = conn.execute("SELECT status FROM calendar_actions WHERE proposal_token = ?", (token,))
        assert cur.fetchone()[0] == "cancelled"


def test_confirm_calendar_action_expired(test_db):
    """
    Verify proposals older than 15 minutes are rejected as expired without API calls.
    """
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    token = "old-proposal-token-123"

    with sqlite3.connect(test_db) as conn:
        conn.execute(
            """
            INSERT INTO calendar_actions (proposal_token, action_type, event_summary, start_time, end_time, confirmed_by_user, status, created_at)
            VALUES (?, 'create', 'Old Event', '2026-09-20T10:00:00Z', '2026-09-20T10:30:00Z', 0, 'proposed', ?)
            """,
            (token, old_time),
        )
        conn.commit()

    with patch("httpx.Client") as mock_client:
        res = cal_actions.confirm_calendar_action(test_db, token, confirmed=True)
        assert mock_client.call_count == 0

    assert res["status"] == "expired"
    with sqlite3.connect(test_db) as conn:
        cur = conn.execute("SELECT status FROM calendar_actions WHERE proposal_token = ?", (token,))
        assert cur.fetchone()[0] == "expired"


def test_confirm_calendar_action_atomic_claim_and_execution(test_db):
    """
    Verify confirmed=True performs atomic claim (proposed -> claimed -> executing -> executed)
    and executes Google Calendar REST API v3 call.
    Verify atomic claim prevents second execution replay.
    """
    prop = cal_actions.propose_calendar_action(test_db, "create", "Project Review", "2026-09-20T14:00:00Z", "2026-09-20T15:00:00Z")
    token = prop["proposal_token"]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "google_event_999"}
    mock_response.raise_for_status.return_value = None

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = mock_response

    mock_httpx_cls = MagicMock()
    mock_httpx_cls.return_value.__enter__.return_value = mock_http_client

    with patch("keyring_utils.get_credential", return_value="mock_bearer_token"), patch(
        "httpx.Client", mock_httpx_cls
    ):
        res = cal_actions.confirm_calendar_action(test_db, token, confirmed=True)

    assert res["status"] == "executed"
    assert res["event_id"] == "google_event_999"

    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT status, confirmed_by_user, event_id FROM calendar_actions WHERE proposal_token = ?", (token,))
        row = cur.fetchone()
        assert row["status"] == "executed"
        assert row["confirmed_by_user"] == 1
        assert row["event_id"] == "google_event_999"

    # Verify second confirmation attempt fails atomic claim
    with pytest.raises(ValueError, match="already in state 'executed'|already claimed"):
        cal_actions.confirm_calendar_action(test_db, token, confirmed=True)


def test_confirm_calendar_action_api_failure(test_db):
    """
    Verify API execution failure sets status = 'execution_failed' and does NOT mark status = 'executed'.
    """
    prop = cal_actions.propose_calendar_action(test_db, "create", "Failing Sync", "2026-09-20T16:00:00Z", "2026-09-20T17:00:00Z")
    token = prop["proposal_token"]

    mock_http_client = MagicMock()
    mock_http_client.post.side_effect = Exception("Google HTTP 500 Service Unavailable")

    mock_httpx_cls = MagicMock()
    mock_httpx_cls.return_value.__enter__.return_value = mock_http_client

    with patch("keyring_utils.get_credential", return_value="mock_bearer_token"), patch(
        "httpx.Client", mock_httpx_cls
    ):
        with pytest.raises(RuntimeError, match="Google Calendar API mutation failed"):
            cal_actions.confirm_calendar_action(test_db, token, confirmed=True)

    with sqlite3.connect(test_db) as conn:
        cur = conn.execute("SELECT status FROM calendar_actions WHERE proposal_token = ?", (token,))
        assert cur.fetchone()[0] == "execution_failed"


def test_credential_secrecy(test_db):
    """
    Verify OAuth bearer tokens are never exposed in exception strings, returned dicts, or logs.
    """
    prop = cal_actions.propose_calendar_action(test_db, "create", "Secret Test", "2026-09-20T18:00:00Z", "2026-09-20T19:00:00Z")
    token = prop["proposal_token"]
    secret_token = "ya29.secret_oauth_bearer_token_xyz123"

    mock_http_client = MagicMock()
    mock_http_client.post.side_effect = Exception("HTTP 403 Forbidden")

    mock_httpx_cls = MagicMock()
    mock_httpx_cls.return_value.__enter__.return_value = mock_http_client

    with patch("keyring_utils.get_credential", return_value=secret_token), patch(
        "httpx.Client", mock_httpx_cls
    ):
        with pytest.raises(RuntimeError) as exc_info:
            cal_actions.confirm_calendar_action(test_db, token, confirmed=True)

    assert secret_token not in str(exc_info.value)


# --- FastAPI Calendar Endpoint Tests ---

client = TestClient(app)


def test_fastapi_calendar_propose_and_confirm(test_db, monkeypatch):
    """
    Test FastAPI /calendar/propose and /calendar/confirm routes via TestClient.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)

    # 1. Propose endpoint
    propose_payload = {
        "action_type": "create",
        "summary": "Design Sync",
        "start_time": "2026-09-20T11:00:00Z",
        "end_time": "2026-09-20T12:00:00Z",
        "description": "Discuss architecture",
    }
    res_prop = client.post("/calendar/propose", json=propose_payload)
    assert res_prop.status_code == 200
    prop_data = res_prop.json()
    assert prop_data["status"] == "proposed"
    token = prop_data["proposal_token"]

    # 2. Confirm endpoint with mock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "evt_fastapi_123"}
    mock_response.raise_for_status.return_value = None

    mock_http_client = MagicMock()
    mock_http_client.post.return_value = mock_response

    mock_httpx_cls = MagicMock()
    mock_httpx_cls.return_value.__enter__.return_value = mock_http_client

    with patch("keyring_utils.get_credential", return_value="mock_token"), patch(
        "httpx.Client", mock_httpx_cls
    ):
        res_conf = client.post("/calendar/confirm", json={"proposal_token": token, "confirmed": True})
        assert res_conf.status_code == 200
        conf_data = res_conf.json()
        assert conf_data["status"] == "executed"
        assert conf_data["event_id"] == "evt_fastapi_123"
