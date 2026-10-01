"""
Explicit live-API tests (AGENTS.md coding conventions; TRD Section 8: "explicit live-API tests
(GitHub/Gmail/OAuth) run separately").

Excluded from the default fast suite. Run explicitly with:
    python -m pytest tests -m live -q

These use the real OS keyring and real network. They are read-only: an OAuth token refresh,
a GitHub repository listing, and one Gmail metadata fetch (format=metadata, no bodies).
Each test skips when its credentials are absent. Assertions use booleans and structure only,
so no token, secret, or message content is printed on success or failure.
"""

import pytest

import keyring_utils

pytestmark = pytest.mark.live

GOOGLE_OAUTH_KEYS = ("google_oauth_client_id", "google_oauth_client_secret", "google_oauth_refresh_token")


def _missing(keys):
    return [k for k in keys if not keyring_utils.get_credential(keyring_utils.SERVICE_NAME, k)]


def _require(keys):
    missing = _missing(keys)
    if missing:
        pytest.skip(f"live credentials not configured in keyring: {', '.join(missing)}")


def _refreshed_google_token():
    from services.real_integrations import refresh_google_oauth_token
    return refresh_google_oauth_token()


def test_live_google_oauth_refresh():
    _require(GOOGLE_OAUTH_KEYS)
    token = _refreshed_google_token()
    token_ok = isinstance(token, str) and len(token) > 20
    del token
    assert token_ok, "Google OAuth refresh did not return an access token"


def test_live_github_repository_discovery():
    if not _missing(("github_token",)) or not _missing(("github_pat",)):
        from integrations.github_watch import discover_user_repositories_detailed
        repos = discover_user_repositories_detailed()
    else:
        pytest.skip("live credentials not configured in keyring: github_token or github_pat")
    shape_ok = all(isinstance(r.get("full_name"), str) and "/" in r["full_name"] for r in repos)
    count = len(repos)
    del repos
    assert count > 0, "GitHub discovery returned no repositories (invalid/expired token, or no repositories)"
    assert shape_ok, "GitHub discovery returned malformed repository records"


def test_live_gmail_metadata_fetch():
    _require(GOOGLE_OAUTH_KEYS)
    from integrations.phishing_scan import fetch_gmail_messages
    token = _refreshed_google_token()
    if not token:
        pytest.fail("Google OAuth refresh failed, so Gmail could not be queried", pytrace=False)
    try:
        messages = fetch_gmail_messages(token, max_results=1)
    except Exception as e:
        pytest.fail(f"Gmail metadata fetch failed: {type(e).__name__}", pytrace=False)
    finally:
        del token
    shape_ok = isinstance(messages, list) and all(isinstance(m, dict) for m in messages)
    del messages
    assert shape_ok, "Gmail metadata fetch returned an unexpected shape"
