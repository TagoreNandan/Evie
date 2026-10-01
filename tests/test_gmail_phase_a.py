"""
Unit tests for Phase A Gmail Intelligence enhancements:
- Promotional vs phishing classification separation
- Laya noul first-pass filter and low-confidence escalation
- Gmail historyId incremental checkpoints and fallback handling
"""

import os
import sqlite3
import pytest
from unittest.mock import MagicMock, patch

import integrations.phishing_scan as pscan
from services.gmail_intelligence import (
    init_gmail_tables,
    sync_gmail_intelligence,
    get_gmail_sync_checkpoint,
    update_gmail_sync_checkpoint,
    get_inbox_intelligence_summary,
)

TEST_DB = "test_gmail_phase_a.db"


@pytest.fixture(autouse=True)
def setup_test_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_gmail_tables(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_promotional_email_remains_promotional_and_not_automatically_phishing():
    """
    Verify promotional headers return 'newsletter_promotional' independently of phishing flags.
    """
    promo_headers = {
        "From": "Deals <marketing@shop.com>",
        "Subject": "Weekly Newsletter & Discount Offers",
        "List-Unsubscribe": "<mailto:unsub@shop.com>",
        "Return-Path": "bounce@marketing.com",
    }
    category = pscan.categorize_email_message(promo_headers)
    assert category == "newsletter_promotional"

    # Even with urgency keywords in subject, promotional metadata determines promotional category
    urgent_promo = {
        "From": "Deals <marketing@shop.com>",
        "Subject": "URGENT: Discount Sale Ends Today!",
        "List-Unsubscribe": "<mailto:unsub@shop.com>",
    }
    assert pscan.categorize_email_message(urgent_promo) == "newsletter_promotional"


def test_phishing_classification_remains_independent():
    """
    Verify phishing analysis evaluates risk_score, confidence, and source independently of categorization.
    """
    phishing_headers = {
        "From": "Security Team <security@paypa1-verify.com>",
        "Return-Path": "attacker@phishing-site.net",
        "Subject": "URGENT: Verify Password Immediately",
        "Authentication-Results": "spf=fail dkim=fail",
    }
    analysis = pscan.analyze_email_phishing(phishing_headers)
    assert analysis["risk_score"] in ("high", "critical")
    assert analysis["phishing_confidence"] > 0.5
    assert len(analysis["flags"]) >= 2
    assert "domain_mismatch" in str(analysis["flags"]) or "dkim_failure" in str(analysis["flags"])


def test_laya_available_path():
    """
    Verify Laya noul first-pass path when noul module is available and confident.
    """
    mock_noul = MagicMock()
    mock_noul.predict.return_value = {"is_phishing": True, "confidence": 0.95}

    with patch.object(pscan, "NOUL_AVAILABLE", True), patch.object(pscan, "_noul_module", mock_noul):
        headers = {
            "From": "support@bank.com",
            "Subject": "Account Alert",
        }
        res = pscan.analyze_email_phishing(headers, confidence_threshold=0.7)
        assert res["phishing_source"] == "laya"
        assert res["phishing_confidence"] == 0.95
        assert res["risk_score"] in ("high", "critical")


def test_laya_unavailable_fallback_path():
    """
    Verify clean fallback to heuristic header analysis when Laya noul is unavailable.
    """
    with patch.object(pscan, "NOUL_AVAILABLE", False), patch.object(pscan, "_noul_module", None):
        headers = {
            "From": "clean@company.com",
            "Subject": "Meeting Notes",
        }
        res = pscan.analyze_email_phishing(headers)
        assert res["phishing_source"] == "heuristic"
        assert res["risk_score"] == "low"


def test_low_confidence_escalation():
    """
    Verify low-confidence Laya result (< threshold) escalates to rule-based/LLM path.
    """
    mock_noul = MagicMock()
    mock_noul.predict.return_value = {"is_phishing": True, "confidence": 0.35}  # Low confidence < 0.7

    with patch.object(pscan, "NOUL_AVAILABLE", True), patch.object(pscan, "_noul_module", mock_noul):
        headers = {
            "From": "support@bank.com",
            "Subject": "URGENT: Wire Transfer Required",
        }
        res = pscan.analyze_email_phishing(headers, confidence_threshold=0.7)
        assert res["phishing_source"] == "llm_escalation"
        assert res["risk_score"] in ("moderate", "high", "critical")


def test_checkpoint_creation_and_reuse():
    """
    Verify gmail_sync_checkpoints CRUD creation and retrieval.
    """
    update_gmail_sync_checkpoint(TEST_DB, account_id="primary", history_id="998877", last_message_id="msg_100")
    checkpoint = get_gmail_sync_checkpoint(TEST_DB, account_id="primary")
    assert checkpoint is not None
    assert checkpoint["last_history_id"] == "998877"
    assert checkpoint["last_message_id"] == "msg_100"


def test_only_new_history_changes_processed():
    """
    Verify fetch_gmail_incremental_messages queries history.list when start_history_id is present
    and returns only new history messages.
    """
    history_resp = MagicMock()
    history_resp.status_code = 200
    history_resp.json.return_value = {
        "historyId": "1005",
        "history": [
            {
                "messagesAdded": [{"message": {"id": "msg-hist-1"}}],
            }
        ],
    }

    msg_resp = MagicMock()
    msg_resp.status_code = 200
    msg_resp.json.return_value = {
        "id": "msg-hist-1",
        "payload": {
            "headers": [
                {"name": "From", "value": "news@daily.com"},
                {"name": "Subject", "value": "Daily News"},
                {"name": "List-Unsubscribe", "value": "<mailto:unsub@daily.com>"},
            ]
        },
    }

    mock_client = MagicMock()
    mock_client.get.side_effect = [history_resp, msg_resp]
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        res = pscan.fetch_gmail_incremental_messages("mock_token", start_history_id="1000", max_results=10)

    assert res["history_id"] == "1005"
    assert res["fallback_triggered"] is False
    assert len(res["messages"]) == 1
    assert res["messages"][0]["id"] == "msg-hist-1"


def test_safe_behavior_when_history_checkpoint_expired_or_unavailable():
    """
    Verify clean fallback to messages.list when history API returns 404 (expired historyId).
    """
    history_resp = MagicMock()
    history_resp.status_code = 404

    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.json.return_value = {
        "historyId": "2000",
        "messages": [{"id": "msg-fallback-1"}],
    }

    msg_resp = MagicMock()
    msg_resp.status_code = 200
    msg_resp.json.return_value = {
        "id": "msg-fallback-1",
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@work.com"},
                {"name": "Subject", "value": "Project Sync"},
            ]
        },
    }

    mock_client = MagicMock()
    mock_client.get.side_effect = [history_resp, list_resp, msg_resp]
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        res = pscan.fetch_gmail_incremental_messages("mock_token", start_history_id="expired_999", max_results=10)

    assert res["fallback_triggered"] is True
    assert res["history_id"] == "2000"
    assert len(res["messages"]) == 1
    assert res["messages"][0]["id"] == "msg-fallback-1"
