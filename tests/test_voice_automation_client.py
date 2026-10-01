"""
Focused Unit Tests for FastAPI-side Voice Automation IPC Client.
"""

import json
import os
import socket
import threading
import time
import pytest

import keyring_utils
from services.computer_control.voice_automation_client import (
    AuthenticationError,
    ServiceUnavailableError,
    VoiceAutomationClient,
    VoiceAutomationClientError,
)

TEST_SOCKET = "/tmp/test_voice_automation_client.sock"
TEST_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def mock_keyring(monkeypatch):
    monkeypatch.setattr(
        "keyring_utils.get_credential",
        lambda service, key: TEST_TOKEN if key == "voice_automation_ipc_token" else None,
    )


class MockUdsServer:
    def __init__(self, socket_path: str = TEST_SOCKET):
        self.socket_path = socket_path
        self.server_sock = None
        self.thread = None
        self.running = False
        self.received_messages = []

    def start(self, handler_func=None):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(self.socket_path)
        self.server_sock.listen(1)
        self.server_sock.settimeout(1.0)
        self.running = True

        def run():
            while self.running:
                try:
                    conn, _ = self.server_sock.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break

                with conn:
                    conn.settimeout(2.0)
                    buffer = b""
                    while self.running:
                        try:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            buffer += chunk
                            if b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                msg = json.loads(line.decode("utf-8"))
                                self.received_messages.append(msg)
                                if handler_func:
                                    resp = handler_func(msg)
                                else:
                                    resp = self._default_handler(msg)
                                if resp:
                                    conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                        except socket.timeout:
                            continue
                        except Exception:
                            break

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def _default_handler(self, msg):
        op = msg.get("op")
        req_id = msg.get("request_id")
        if op == "AUTH":
            if msg.get("token") == TEST_TOKEN:
                return {"status": "OK", "request_id": req_id, "action_outcome": "AUTHENTICATED"}
            return {"status": "UNAUTHENTICATED", "request_id": req_id, "error": "AUTHENTICATION_FAILED"}
        elif op == "START":
            return {"status": "OK", "request_id": req_id, "session_id": msg.get("session_id"), "action_outcome": "SESSION_STARTED"}
        elif op == "COMMAND":
            if "ask" in msg.get("transcript", ""):
                return {"status": "ASKING", "request_id": req_id, "session_id": msg.get("session_id"), "ask_reason": "Which tab?"}
            return {"status": "OK", "request_id": req_id, "session_id": msg.get("session_id"), "action_outcome": "EXECUTED"}
        elif op == "STOP":
            return {"status": "OK", "request_id": req_id, "session_id": msg.get("session_id"), "action_outcome": "SESSION_STOPPED"}
        return {"status": "ERROR", "request_id": req_id, "error": "UNKNOWN_OP"}

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=1.0)


def test_client_service_unavailable():
    client = VoiceAutomationClient(socket_path="/tmp/nonexistent_evie_sock.sock")
    with pytest.raises(ServiceUnavailableError):
        client.start("sess_123")


def test_client_auth_failure(mock_keyring, monkeypatch):
    monkeypatch.setattr("keyring_utils.get_credential", lambda s, k: "wrong_token")
    server = MockUdsServer()
    server.start()
    try:
        client = VoiceAutomationClient(socket_path=TEST_SOCKET)
        with pytest.raises(AuthenticationError):
            client.start("sess_123")
    finally:
        server.stop()


def test_client_start_command_stop_lifecycle(mock_keyring):
    server = MockUdsServer()
    server.start()
    try:
        client = VoiceAutomationClient(socket_path=TEST_SOCKET)

        # 1. START
        res_start = client.start("sess_test")
        assert res_start["status"] == "OK"
        assert client.active_session_id == "sess_test"

        # 2. COMMAND
        res_cmd = client.command("sess_test", "open Safari")
        assert res_cmd["status"] == "OK"
        assert res_cmd["action_outcome"] == "EXECUTED"

        # 3. COMMAND ASKING
        res_ask = client.command("sess_test", "click ask tab")
        assert res_ask["status"] == "ASKING"
        assert res_ask["ask_reason"] == "Which tab?"

        # 4. STOP
        res_stop = client.stop("sess_test")
        assert res_stop["status"] == "OK"
        assert client.active_session_id is None
        assert not client.is_connected()
    finally:
        server.stop()


def test_client_request_id_mismatch(mock_keyring):
    def bad_req_id_handler(msg):
        if msg.get("op") in ("AUTH", "START"):
            return {"status": "OK", "request_id": msg.get("request_id")}
        return {"status": "OK", "request_id": "mismatched_id_123"}

    server = MockUdsServer()
    server.start(handler_func=bad_req_id_handler)
    try:
        client = VoiceAutomationClient(socket_path=TEST_SOCKET, timeout_s=1.0)
        client.start("sess_mismatch")
        # A reply for another request is stale and skipped (never accepted); with no matching reply the COMMAND
        # times out - the session itself stays active.
        with pytest.raises(VoiceAutomationClientError, match="timeout"):
            client.command("sess_mismatch", "type hello")
        assert client.active_session_id == "sess_mismatch"
    finally:
        server.stop()


def test_client_no_retry_on_command_timeout(mock_keyring):
    command_attempts = []

    def timeout_handler(msg):
        if msg.get("op") == "AUTH":
            return {"status": "OK", "request_id": msg.get("request_id")}
        if msg.get("op") == "START":
            return {"status": "OK", "request_id": msg.get("request_id")}
        if msg.get("op") == "COMMAND":
            command_attempts.append(msg)
            time.sleep(0.5)  # Simulate slow server response
            return {"status": "OK", "request_id": msg.get("request_id")}
        return {"status": "OK", "request_id": msg.get("request_id")}

    server = MockUdsServer()
    server.start(handler_func=timeout_handler)
    try:
        # Client timeout is set very small (0.1s)
        client = VoiceAutomationClient(socket_path=TEST_SOCKET, timeout_s=0.1)
        client.start("sess_timeout")
        with pytest.raises(VoiceAutomationClientError):
            client.command("sess_timeout", "open Safari")

        # Verify command was attempted exactly ONCE (no retries)
        assert len(command_attempts) == 1
    finally:
        server.stop()


def test_client_token_privacy_in_errors(mock_keyring):
    server = MockUdsServer()
    server.start()
    try:
        client = VoiceAutomationClient(socket_path=TEST_SOCKET)
        try:
            client._send_recv({"op": "INVALID_OP", "request_id": "r1"})
        except Exception as exc:
            err_str = str(exc)
            assert TEST_TOKEN not in err_str
            assert "0123456789" not in err_str
    finally:
        server.stop()


def test_client_command_requires_explicit_start_when_autostart_disabled(mock_keyring):
    server = MockUdsServer()
    server.start()
    try:
        client = VoiceAutomationClient(socket_path=TEST_SOCKET)
        with pytest.raises(VoiceAutomationClientError, match="Must call START first"):
            client.command("sess_unstarted", "open Safari", allow_autostart=False)
    finally:
        server.stop()


def test_client_failed_stop_delivery_returns_distinguishable_error(mock_keyring):
    def breaking_stop_handler(msg):
        if msg.get("op") == "STOP":
            raise Exception("Force socket failure")
        return MockUdsServer._default_handler(MockUdsServer(), msg)

    server = MockUdsServer()
    server.start(handler_func=breaking_stop_handler)
    try:
        client = VoiceAutomationClient(socket_path=TEST_SOCKET)
        client.start("sess_stop_fail")
        res = client.stop("sess_stop_fail")
        assert res["status"] == "ERROR"
        assert res["error"] == "STOP_DELIVERY_FAILED"
        assert client.active_session_id is None
        assert not client.is_connected()
    finally:
        server.stop()

