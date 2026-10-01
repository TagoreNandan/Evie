"""
Unit tests for TwoFactorAuditor and twofa_audit module.
"""

from unittest.mock import patch, MagicMock
import pytest

from integrations.twofa_audit import TwoFactorAuditor


def test_audit_github_2fa_enabled():
    mock_github_response = {
        "login": "octocat",
        "two_factor_authentication": True,
    }

    with patch("integrations.twofa_audit.get_credential", return_value="ghp_fake_token_12345"):
        with patch("httpx.Client.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_github_response
            mock_get.return_value = mock_response

            auditor = TwoFactorAuditor()
            res = auditor.audit_github_2fa()

            assert res["provider"] == "github"
            assert res["username"] == "octocat"
            assert res["2fa_enabled"] is True
            assert res["status"] == "ok"


def test_audit_github_2fa_disabled():
    mock_github_response = {
        "login": "octocat",
        "two_factor_authentication": False,
    }

    with patch("integrations.twofa_audit.get_credential", return_value="ghp_fake_token_12345"):
        with patch("httpx.Client.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_github_response
            mock_get.return_value = mock_response

            auditor = TwoFactorAuditor()
            res = auditor.audit_github_2fa()

            assert res["provider"] == "github"
            assert res["2fa_enabled"] is False
            assert res["status"] == "ok"


def test_audit_github_2fa_insufficient_scope():
    # When token lacks read:user scope, two_factor_authentication field is omitted from response
    mock_github_response = {
        "login": "octocat",
    }

    with patch("integrations.twofa_audit.get_credential", return_value="ghp_fake_token_12345"):
        with patch("httpx.Client.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_github_response
            mock_get.return_value = mock_response

            auditor = TwoFactorAuditor()
            res = auditor.audit_github_2fa()

            assert res["provider"] == "github"
            assert res["2fa_enabled"] is None
            assert res["status"] == "scope_insufficient"


def test_audit_google_2fa_deferred():
    auditor = TwoFactorAuditor()
    res = auditor.audit_google_2fa()

    assert res["provider"] == "google"
    assert res["2fa_enabled"] is None
    assert res["status"] == "deferred"
    assert "Google personal 2FA status API not available" in res["reason"]
