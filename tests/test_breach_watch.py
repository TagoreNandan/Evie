"""
Unit tests for BreachWatcher and breach_watch module.
"""

from unittest.mock import patch, MagicMock
import pytest

from integrations.breach_watch import BreachWatcher, redact_email


def test_redact_email():
    assert redact_email("user.name@example.com") == "u***e@example.com"
    assert redact_email("a@b.com") == "**@b.com"
    assert redact_email(None) == "****"


def test_check_email_breaches_success():
    mock_breaches_json = [
        {
            "Name": "Adobe",
            "Domain": "adobe.com",
            "BreachDate": "2013-10-04",
            "DataClasses": ["Email addresses", "Passwords"],
            "IsVerified": True,
        }
    ]

    with patch("integrations.breach_watch.get_credential", return_value="synthetic_hibp_key"):
        with patch("httpx.Client.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_breaches_json
            mock_get.return_value = mock_response

            watcher = BreachWatcher()
            findings = watcher.check_email_breaches("test.user@example.com")

            assert len(findings) == 1
            assert findings[0]["name"] == "Adobe"
            assert findings[0]["domain"] == "adobe.com"
            assert findings[0]["breach_date"] == "2013-10-04"
            assert "Email addresses" in findings[0]["data_classes"]

            # Verify HTTPS URL and headers
            mock_get.assert_called_once()
            url_called = mock_get.call_args[0][0]
            headers_called = mock_get.call_args[1]["headers"]
            assert url_called.startswith("https://haveibeenpwned.com/api/v3/breachedaccount/")
            assert headers_called["hibp-api-key"] == "synthetic_hibp_key"
            assert headers_called["User-Agent"] == "Evie-Security-Assistant"


def test_check_email_breaches_404_no_breach():
    with patch("integrations.breach_watch.get_credential", return_value="synthetic_hibp_key"):
        with patch("httpx.Client.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_get.return_value = mock_response

            watcher = BreachWatcher()
            findings = watcher.check_email_breaches("safe.user@example.com")
            assert findings == []


def test_check_email_breaches_http_errors():
    with patch("integrations.breach_watch.get_credential", return_value="synthetic_hibp_key"):
        with patch("httpx.Client.get") as mock_get:
            # Test 401 Unauthorized
            mock_response_401 = MagicMock()
            mock_response_401.status_code = 401
            mock_get.return_value = mock_response_401

            watcher = BreachWatcher()
            with pytest.raises(RuntimeError, match="HTTP 401"):
                watcher.check_email_breaches("test@example.com")

            # Test 429 Rate Limit
            mock_response_429 = MagicMock()
            mock_response_429.status_code = 429
            mock_get.return_value = mock_response_429

            with pytest.raises(RuntimeError, match="HTTP 429"):
                watcher.check_email_breaches("test@example.com")


def test_check_email_breaches_missing_keyring_credential():
    with patch("integrations.breach_watch.get_credential", return_value=None):
        watcher = BreachWatcher()
        with pytest.raises(RuntimeError, match="hibp_api_key.*not found in OS keyring"):
            watcher.check_email_breaches("test@example.com")
