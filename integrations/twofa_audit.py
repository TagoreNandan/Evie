"""
2FA Account Security Auditor Module for Evie Assistant.

Implements SEC-04:
Inspects 2FA status for GitHub accounts using the authenticated user endpoint.
Explicitly documents Google 2FA API limitations (deferred for non-admin personal accounts).
"""

from typing import Any, Dict
import httpx
from keyring_utils import get_credential, SERVICE_NAME

GITHUB_USER_URL = "https://api.github.com/user"


class TwoFactorAuditor:
    """
    Audits 2FA configuration on connected accounts.
    """

    def audit_github_2fa(self, token_key: str = "github_token", timeout: float = 10.0) -> Dict[str, Any]:
        """
        Audit GitHub account 2FA status using the GET /user endpoint.
        Requires GitHub token with 'read:user' or 'user' scope to access the private
        'two_factor_authentication' field.
        """
        token = get_credential(SERVICE_NAME, token_key)
        if not token:
            raise RuntimeError(f"GitHub token key '{token_key}' not found in OS keyring")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Evie-Security-Assistant",
        }

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(GITHUB_USER_URL, headers=headers)
        except Exception as e:
            raise RuntimeError(f"Network error querying GitHub API: {e}") from e

        if response.status_code == 401:
            raise RuntimeError("GitHub API token unauthorized (HTTP 401)")
        elif response.status_code != 200:
            raise RuntimeError(f"GitHub API unexpected status code (HTTP {response.status_code})")

        data = response.json()
        two_fa_enabled = data.get("two_factor_authentication")

        if two_fa_enabled is None:
            return {
                "provider": "github",
                "username": data.get("login"),
                "2fa_enabled": None,
                "status": "scope_insufficient",
                "notes": "Token lacks 'read:user' or 'user' OAuth scope required to read 2FA status",
            }

        return {
            "provider": "github",
            "username": data.get("login"),
            "2fa_enabled": bool(two_fa_enabled),
            "status": "ok",
        }

    def audit_google_2fa(self) -> Dict[str, Any]:
        """
        Google personal 2FA status audit.
        Explicit SEC-04 limitation: Google OAuth userinfo API does NOT expose 2FA status
        for personal non-admin accounts, and no standard non-admin API endpoint exists.
        Marked explicitly as deferred/blocked without inventing unsupported approximations.
        """
        return {
            "provider": "google",
            "2fa_enabled": None,
            "status": "deferred",
            "reason": "Google personal 2FA status API not available for non-admin accounts",
        }
