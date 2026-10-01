"""
Focused Unit Tests for VoiceAutomationService UDS IPC Protocol & Launcher Integration.
"""

import json
import os
import socket
import threading
import time
import pytest

from services.computer_control.voice_automation_service import (
    IPC_KEY_NAME,
    KEYRING_SERVICE_NAME,
    MAX_MESSAGE_BYTES,
    VoiceAutomationService,
    get_or_create_ipc_token,
)


@pytest.fixture
def mock_keyring(monkeypatch):
    vault = {}

    def mock_get(service, key):
        return vault.get(f"{service}:{key}")

    def mock_set(service, key, value):
        vault[f"{service}:{key}"] = value

    monkeypatch.setattr("keyring_utils.get_credential", mock_get)
    monkeypatch.setattr("keyring_utils.set_credential", mock_set)
    return vault


def test_ipc_token_creation_and_retrieval(mock_keyring):
    token1 = get_or_create_ipc_token()
    assert isinstance(token1, str)
    assert len(token1) == 64  # hex 32 bytes

    token2 = get_or_create_ipc_token()
    assert token1 == token2
    assert mock_keyring.get(f"{KEYRING_SERVICE_NAME}:{IPC_KEY_NAME}") == token1


def test_handle_message_protocol_validation(mock_keyring):
    svc = VoiceAutomationService(db_path=":memory:")
    token = get_or_create_ipc_token()

    # Oversized message rejection
    oversized = b"a" * (MAX_MESSAGE_BYTES + 1)
    res, auth = svc.handle_message(oversized, False)
    assert res["status"] == "ERROR"
    assert res["error"] == "OVERSIZED_MESSAGE"

    # Malformed non-JSON rejection
    res, auth = svc.handle_message(b"invalid json {{{", False)
    assert res["status"] == "ERROR"
    assert res["error"] == "MALFORMED_MESSAGE"

    # Unauthenticated request rejection
    res, auth = svc.handle_message(json.dumps({"op": "START", "session_id": "s1"}).encode("utf-8"), False)
    assert res["status"] == "UNAUTHENTICATED"
    assert res["error"] == "AUTHENTICATION_REQUIRED"

    # AUTH with wrong token
    res, auth = svc.handle_message(json.dumps({"op": "AUTH", "token": "wrong_token"}).encode("utf-8"), False)
    assert res["status"] == "UNAUTHENTICATED"
    assert res["error"] == "AUTHENTICATION_FAILED"
    assert auth is False

    # AUTH with valid token
    res, auth = svc.handle_message(json.dumps({"op": "AUTH", "token": token}).encode("utf-8"), False)
    assert res["status"] == "OK"
    assert res["action_outcome"] == "AUTHENTICATED"
    assert auth is True


def test_voice_automation_service_lifecycle(mock_keyring, monkeypatch):
    svc = VoiceAutomationService(db_path=":memory:")
    token = get_or_create_ipc_token()

    # Mock runtime and identity check
    class MockRuntime:
        def identity(self):
            return (True, [])

    svc.start_runtime = lambda: None
    svc.runtime = MockRuntime()

    # Mock computer_control_router.handle
    monkeypatch.setattr(
        "services.computer_control_router.handle",
        lambda transcript: {"status": "SESSION_ENDED", "final_state": "DONE_VERIFIED"}
    )

    auth = True

    # COMMAND without START fails
    res, auth = svc.handle_message(json.dumps({"op": "COMMAND", "session_id": "s1", "transcript": "type hello"}).encode("utf-8"), auth)
    assert res["status"] == "ERROR"
    assert res["error"] == "INVALID_SESSION"

    # START session
    res, auth = svc.handle_message(json.dumps({"op": "START", "request_id": "r1", "session_id": "sess_100"}).encode("utf-8"), auth)
    assert res["status"] == "OK"
    assert res["session_id"] == "sess_100"
    assert res["action_outcome"] == "SESSION_STARTED"
    assert svc.active_session_id == "sess_100"

    # COMMAND with valid active session
    res, auth = svc.handle_message(json.dumps({"op": "COMMAND", "request_id": "r2", "session_id": "sess_100", "transcript": "type hello"}).encode("utf-8"), auth)
    assert res["status"] == "OK"
    assert res["action_outcome"] == "EXECUTED"

    # COMMAND with mismatched session_id fails
    res, auth = svc.handle_message(json.dumps({"op": "COMMAND", "request_id": "r3", "session_id": "sess_WRONG", "transcript": "type hello"}).encode("utf-8"), auth)
    assert res["status"] == "ERROR"
    assert res["error"] == "INVALID_SESSION"

    # STOP session
    res, auth = svc.handle_message(json.dumps({"op": "STOP", "request_id": "r4", "session_id": "sess_100"}).encode("utf-8"), auth)
    assert res["status"] == "OK"
    assert res["action_outcome"] == "SESSION_STOPPED"
    assert svc.active_session_id is None

    # Disconnect cleanup
    svc.active_session_id = "stale_sess"
    svc.on_disconnect()
    assert svc.active_session_id is None


def test_voice_automation_uds_socket_server(mock_keyring, monkeypatch):
    sock_path = f"/tmp/test_evie_{os.getpid()}.sock"
    svc = VoiceAutomationService(db_path=":memory:", socket_path=sock_path)
    token = get_or_create_ipc_token()

    svc.start_runtime = lambda: None

    stop_event = threading.Event()
    server_thread = threading.Thread(target=svc.run_server, kwargs={"stop_event": stop_event}, daemon=True)
    server_thread.start()

    time.sleep(0.1)
    assert os.path.exists(sock_path)

    # Client connection test
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)

    # Send AUTH
    client.sendall(json.dumps({"op": "AUTH", "token": token}).encode("utf-8") + b"\n")
    resp_raw = client.recv(4096)
    resp = json.loads(resp_raw.decode("utf-8"))
    assert resp["status"] == "OK"
    assert resp["action_outcome"] == "AUTHENTICATED"

    # Send START
    client.sendall(json.dumps({"op": "START", "request_id": "req_1", "session_id": "sess_uds"}).encode("utf-8") + b"\n")
    resp_raw = client.recv(4096)
    resp = json.loads(resp_raw.decode("utf-8"))
    assert resp["status"] == "OK"
    assert resp["session_id"] == "sess_uds"

    client.close()
    stop_event.set()
    server_thread.join(timeout=2.0)
    assert not os.path.exists(sock_path)


def test_launcher_swift_mode_acceptance():
    with open("packaging/macos/EvieLauncher.swift", "r") as f:
        code = f.read()
    assert '"voice_automation_service": "services.computer_control.voice_automation_service"' in code
