"""
Fast/live test boundary (Protected Area #4; AGENTS.md coding conventions, TRD Section 8,
Acceptance Criteria Section 11).

Every test NOT marked @pytest.mark.live runs inside a safety boundary:
- The real OS keyring is unreachable: keyring.get_password returns None (tests that need a
  credential patch keyring_utils.get_credential / keyring.get_password themselves), writes
  and deletes are refused, and the active backend is swapped for keyring's "fail" backend.
- Outbound network is blocked: socket connects and DNS lookups to anything other than
  loopback/Unix sockets raise. Every blocked attempt is also recorded and fails the test at
  teardown, because production code often catches exceptions (e.g. OAuth refresh returns None),
  which would otherwise hide an attempted call.
- Speaker verification points at temporary paths, so the real ~/.evie voice print and model
  are never read.
- The LLM planner never reaches the real API: llm_client refuses live calls under pytest, and the keyring and network are blocked.

Live tests (python -m pytest tests -m live -q) run without this boundary.
"""

import socket

import pytest

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


class OutboundNetworkBlocked(RuntimeError):
    """Raised when a fast (non-live) test tries to reach the network."""


class RealKeyringBlocked(RuntimeError):
    """Raised when a fast (non-live) test tries to modify the real OS keyring."""


def _host_of(address):
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None  # AF_UNIX path or unknown -> local


def _is_local(host):
    return host is None or host in LOCAL_HOSTS or host.startswith("127.")


@pytest.fixture(autouse=True)
def fast_test_boundary(request, monkeypatch, tmp_path):
    if request.node.get_closest_marker("live"):
        yield
        return

    attempts = []

    # --- Outbound network ---
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock, address):
        host = _host_of(address)
        if sock.family != socket.AF_UNIX and not _is_local(host):
            attempts.append(f"connect {host}")
            raise OutboundNetworkBlocked(f"Outbound network blocked in fast tests: {host}")
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        host = _host_of(address)
        if sock.family != socket.AF_UNIX and not _is_local(host):
            attempts.append(f"connect_ex {host}")
            raise OutboundNetworkBlocked(f"Outbound network blocked in fast tests: {host}")
        return real_connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else (str(host) if host is not None else None)
        if not _is_local(name):
            attempts.append(f"dns {name}")
            raise OutboundNetworkBlocked(f"DNS lookup blocked in fast tests: {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)

    # --- Real OS keyring ---
    import keyring
    from keyring.backends import fail

    def refuse_write(*args, **kwargs):
        raise RealKeyringBlocked("Real OS keyring writes are blocked in fast tests")

    monkeypatch.setattr(keyring, "get_password", lambda service, key: None)
    monkeypatch.setattr(keyring, "set_password", refuse_write)
    monkeypatch.setattr(keyring, "delete_password", refuse_write)
    original_backend = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())

    # --- Speaker verification ---
    monkeypatch.setenv("EVIE_SPEAKER_REFERENCE_PATH", str(tmp_path / "evie_speaker" / "speaker_reference.npy"))
    monkeypatch.setenv("EVIE_SPEAKER_MODEL_DIR", str(tmp_path / "evie_speaker" / "no_model"))

    try:
        yield attempts  # a test may inspect (and clear) deliberate, expected attempts
    finally:
        keyring.set_keyring(original_backend)

    if attempts:
        pytest.fail(f"Fast test attempted outbound network access: {sorted(set(attempts))}", pytrace=False)
