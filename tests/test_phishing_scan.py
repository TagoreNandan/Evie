"""
Unit tests for Read-Only Gmail Phishing Pattern Analyzer (integrations/phishing_scan.py).
Tests SCORE-04, header inspection, financial urgency keywords, domain mismatches, auth failures, and risk scoring.
"""

import pytest
import integrations.phishing_scan as pscan


def test_analyze_email_header_clean():
    """
    Verify clean email header produces risk_score = 'low' and zero flags.
    """
    headers = {
        "From": "alice@company.com",
        "Return-Path": "alice@company.com",
        "Subject": "Project Status Update for Friday",
        "Authentication-Results": "dkim=pass spf=pass",
    }
    res = pscan.analyze_email_header(headers)
    assert res["risk_score"] == "low"
    assert len(res["flags"]) == 0


def test_analyze_email_header_domain_mismatch():
    """
    Verify domain mismatch between From and Return-Path is flagged.
    """
    headers = {
        "From": "Security Team <security@paypal.com>",
        "Return-Path": "attacker@phishing-site.net",
        "Subject": "Routine Notice",
        "Authentication-Results": "dkim=pass",
    }
    res = pscan.analyze_email_header(headers)
    assert res["risk_score"] == "moderate"
    assert any("domain_mismatch" in f for f in res["flags"])


def test_analyze_email_header_urgency_keyword():
    """
    Verify financial urgency keywords in subject are flagged.
    """
    headers = {
        "From": "support@example.com",
        "Return-Path": "support@example.com",
        "Subject": "URGENT: Account Suspended Immediately",
    }
    res = pscan.analyze_email_header(headers)
    assert res["risk_score"] == "moderate"
    assert any("urgency_keyword" in f for f in res["flags"])


def test_analyze_email_header_auth_failures():
    """
    Verify DKIM and SPF authentication failures are flagged.
    """
    headers = {
        "From": "info@bank.com",
        "Return-Path": "info@bank.com",
        "Subject": "Account Statement",
        "Authentication-Results": "dkim=fail spf=fail",
    }
    res = pscan.analyze_email_header(headers)
    assert res["risk_score"] == "high"
    assert "dkim_failure" in res["flags"]
    assert "spf_failure" in res["flags"]


def test_scan_phishing_patterns_batch():
    """
    Verify scan_phishing_patterns filters batch messages returning only flagged risks >= 'moderate'.
    """
    messages = [
        {
            "id": "msg-001",
            "headers": {
                "From": "clean@example.com",
                "Return-Path": "clean@example.com",
                "Subject": "Weekly Newsletter",
            },
        },
        {
            "id": "msg-002",
            "headers": {
                "From": "billing@paypal.com",
                "Return-Path": "fake@phishing-domain.com",
                "Subject": "URGENT: Wire Transfer Verification Required",
            },
        },
    ]

    flagged = pscan.scan_phishing_patterns(messages)
    assert len(flagged) == 1
    assert flagged[0]["message_id"] == "msg-002"
    assert flagged[0]["risk_score"] in ("high", "critical")


def test_fetch_gmail_messages_missing_token():
    with pytest.raises(ValueError, match="CREDENTIAL_MISSING"):
        pscan.fetch_gmail_messages("")


def test_fetch_gmail_messages_success_and_no_body_requested():
    """
    Verify successful Gmail message listing, metadata header retrieval,
    no body requested (format=metadata only), and phishing analysis reuse.
    """
    from unittest.mock import MagicMock, patch

    list_response = MagicMock()
    list_response.status_code = 200
    list_response.json.return_value = {
        "messages": [{"id": "msg-101"}, {"id": "msg-102"}]
    }

    msg1_response = MagicMock()
    msg1_response.status_code = 200
    msg1_response.json.return_value = {
        "id": "msg-101",
        "payload": {
            "headers": [
                {"name": "From", "value": "security@paypal.com"},
                {"name": "Return-Path", "value": "hacker@malicious.com"},
                {"name": "Subject", "value": "URGENT: Verify Password Immediately"},
                {"name": "Authentication-Results", "value": "spf=fail dkim=fail"},
            ]
        },
    }

    msg2_response = MagicMock()
    msg2_response.status_code = 200
    msg2_response.json.return_value = {
        "id": "msg-102",
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@company.com"},
                {"name": "Return-Path", "value": "alice@company.com"},
                {"name": "Subject", "value": "Team Meeting Notes"},
                {"name": "Authentication-Results", "value": "spf=pass dkim=pass"},
            ]
        },
    }

    mock_client = MagicMock()
    mock_client.get.side_effect = [list_response, msg1_response, msg2_response]
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        messages = pscan.fetch_gmail_messages("mock_token_123", max_results=10)

    assert len(messages) == 2
    assert messages[0]["id"] == "msg-101"
    assert messages[1]["id"] == "msg-102"

    # Verify format=metadata was requested and body was NOT requested
    call_args_list = mock_client.get.call_args_list
    assert len(call_args_list) == 3
    for call in call_args_list[1:]:
        params = call.kwargs.get("params", [])
        param_dict = dict(params) if isinstance(params, list) else params
        assert param_dict.get("format") == "metadata"
        assert "body" not in param_dict
        assert "snippet" not in param_dict

    # Re-use phishing analysis logic
    flagged = pscan.scan_phishing_patterns(messages)
    assert len(flagged) == 1
    assert flagged[0]["message_id"] == "msg-101"
    assert flagged[0]["risk_score"] in ("high", "critical")


def test_fetch_gmail_messages_empty_inbox():
    from unittest.mock import MagicMock, patch

    list_response = MagicMock()
    list_response.status_code = 200
    list_response.json.return_value = {"messages": []}

    mock_client = MagicMock()
    mock_client.get.return_value = list_response
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        messages = pscan.fetch_gmail_messages("mock_token", max_results=10)

    assert messages == []


def test_fetch_gmail_messages_http_401():
    from unittest.mock import MagicMock, patch

    resp = MagicMock()
    resp.status_code = 401

    mock_client = MagicMock()
    mock_client.get.return_value = resp
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="HTTP 401"):
            pscan.fetch_gmail_messages("invalid_token")


def test_fetch_gmail_messages_http_403():
    from unittest.mock import MagicMock, patch

    resp = MagicMock()
    resp.status_code = 403

    mock_client = MagicMock()
    mock_client.get.return_value = resp
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="HTTP 403"):
            pscan.fetch_gmail_messages("forbidden_token")


def test_fetch_gmail_messages_http_429():
    from unittest.mock import MagicMock, patch

    resp = MagicMock()
    resp.status_code = 429

    mock_client = MagicMock()
    mock_client.get.return_value = resp
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="HTTP 429"):
            pscan.fetch_gmail_messages("rate_limited_token")


def test_fetch_gmail_messages_malformed_response():
    from unittest.mock import MagicMock, patch

    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("Invalid JSON string")

    mock_client = MagicMock()
    mock_client.get.return_value = resp
    mock_client.__enter__.return_value = mock_client

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="Malformed JSON response"):
            pscan.fetch_gmail_messages("mock_token")

