"""
Self-checks for the fast-test safety boundary in tests/conftest.py (Protected Area #4).
"""

import os
import socket
import urllib.request

import httpx
import keyring
import pytest

import keyring_utils

REAL_CREDENTIAL_KEYS = (
    "github_token", "github_pat", "google_oauth_client_id", "google_oauth_client_secret",
    "google_oauth_refresh_token", "google_calendar_oauth", "hibp_api_key", "anthropic_api_key",
)


def test_real_keyring_is_unreachable():
    for key in REAL_CREDENTIAL_KEYS:
        assert keyring.get_password("evie_assistant", key) is None
        assert keyring_utils.get_credential("evie_assistant", key) is None
    assert type(keyring.get_keyring()).__module__ == "keyring.backends.fail"
    with pytest.raises(RuntimeError):
        keyring_utils.set_credential("evie_assistant", "github_pat", "should-never-be-written")
    with pytest.raises(RuntimeError):
        keyring_utils.delete_credential("evie_assistant", "github_pat")


@pytest.mark.parametrize("attempt", [
    lambda: socket.create_connection(("api.github.com", 443), timeout=1),
    lambda: socket.getaddrinfo("oauth2.googleapis.com", 443),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("140.82.112.3", 443)),
    lambda: httpx.get("https://gmail.googleapis.com/", timeout=1),
    lambda: urllib.request.urlopen("https://oauth2.googleapis.com/token", timeout=1),
])
def test_outbound_network_is_blocked_and_recorded(fast_test_boundary, attempt):
    with pytest.raises(Exception):
        attempt()
    assert fast_test_boundary, "blocked attempt was not recorded"
    fast_test_boundary.clear()  # expected, deliberate attempt: don't fail teardown


def test_loopback_is_still_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())
    finally:
        client.close()
        server.close()


def test_speaker_paths_point_at_temporary_locations(tmp_path):
    ref = os.environ["EVIE_SPEAKER_REFERENCE_PATH"]
    model = os.environ["EVIE_SPEAKER_MODEL_DIR"]
    assert ref.startswith(str(tmp_path)) and model.startswith(str(tmp_path))
    assert ".evie" not in ref and ".evie" not in model

