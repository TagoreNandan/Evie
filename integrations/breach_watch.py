"""
Have I Been Pwned (HIBP) Email Breach Monitor for Evie Assistant.

Implements SEC-02:
Queries HIBP v3 API over HTTPS to monitor registered email addresses for breaches.
Retrieves API keys via OS Keyring. Enforces strict privacy and HTTP error boundaries.
"""

import urllib.parse
from typing import Any, Dict, List, Optional
import httpx

from keyring_utils import get_credential, SERVICE_NAME
from redaction import redact_secret

HIBP_BASE_URL = "https://haveibeenpwned.com/api/v3/breachedaccount"
USER_AGENT = "Evie-Security-Assistant"


def redact_email(email: str) -> str:
    """
    Redact the local part of an email address for safe logging/output.
    e.g. user.name@example.com -> u***e@example.com
    """
    if not email or "@" not in email:
        return "****"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        redacted_local = "**"
    else:
        redacted_local = f"{local[0]}***{local[-1]}"
    return f"{redacted_local}@{domain}"


class BreachWatcher:
    """
    Watcher service for querying HIBP email breach database.
    """

    def check_email_breaches(
        self, email: str, key_name: str = "hibp_api_key", timeout: float = 10.0
    ) -> List[Dict[str, Any]]:
        """
        Check if an email address appears in HIBP data breaches.
        Retrieves HIBP API key from OS keyring.
        Explicit Phase 1 decision: Direct email lookup over HTTPS is used; k-anonymity is deferred.
        """
        if not email or "@" not in email:
            raise ValueError("Valid email address is required")

        api_key = get_credential(SERVICE_NAME, key_name)
        if not api_key:
            raise RuntimeError(f"HIBP API key '{key_name}' not found in OS keyring")

        encoded_email = urllib.parse.quote(email)
        url = f"{HIBP_BASE_URL}/{encoded_email}?truncateResponse=false"
        headers = {
            "hibp-api-key": api_key,
            "User-Agent": USER_AGENT,
        }

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url, headers=headers)
        except Exception as e:
            raise RuntimeError(f"Network error querying HIBP API for {redact_email(email)}: {e}") from e

        # Handle HTTP status codes explicitly
        if response.status_code == 404:
            # 404 explicitly means "no breaches found"
            return []
        elif response.status_code == 401:
            raise RuntimeError("HIBP API key unauthorized (HTTP 401)")
        elif response.status_code == 403:
            raise RuntimeError("HIBP API access forbidden (HTTP 403)")
        elif response.status_code == 429:
            raise RuntimeError("HIBP API rate limit exceeded (HTTP 429)")
        elif response.status_code >= 500:
            raise RuntimeError(f"HIBP server error (HTTP {response.status_code})")
        elif response.status_code != 200:
            raise RuntimeError(f"HIBP API unexpected status code (HTTP {response.status_code})")

        # Parse JSON response
        try:
            raw_breaches = response.json()
        except Exception as e:
            raise RuntimeError("Failed to parse JSON response from HIBP API") from e

        # Minimization: Return essential breach metadata only
        findings: List[Dict[str, Any]] = []
        for b in raw_breaches:
            findings.append({
                "name": b.get("Name"),
                "domain": b.get("Domain"),
                "breach_date": b.get("BreachDate"),
                "data_classes": b.get("DataClasses", []),
                "is_verified": b.get("IsVerified", True),
            })

        return findings
