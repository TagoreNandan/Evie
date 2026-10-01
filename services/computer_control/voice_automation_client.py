"""
FastAPI-side IPC Client for Evie Voice Automation Signed-Runtime Service.

Connects strictly via local Unix Domain Socket (UDS) to `voice_automation_service`,
authenticates using the OS keyring credential, and exposes a clean, bounded high-level
API (start, command, stop) for FastAPI endpoints.

Security Invariants:
- Never exposes credentials in logs, exceptions, or returns.
- Bounded message sizes (<= 64 KB).
- Fail-closed on timeout, socket errors, request_id mismatch, or authentication failure.
- No retries on COMMAND timeout (action outcome is unknown).
"""

import json
import logging
import os
import socket
import threading
import uuid
from typing import Any, Dict, Optional

import keyring_utils
from services.computer_control.voice_automation_service import (
    IPC_KEY_NAME,
    KEYRING_SERVICE_NAME,
    MAX_MESSAGE_BYTES,
    get_default_socket_path,
    get_or_create_ipc_token,
)

logger = logging.getLogger(__name__)


MAX_STALE_REPLIES = 8


class VoiceAutomationClientError(Exception):
    """Base exception for Voice Automation IPC client errors."""
    pass


class ServiceUnavailableError(VoiceAutomationClientError):
    """Raised when the signed voice_automation_service socket is missing or unreachable."""
    pass


class CommandTimeoutError(VoiceAutomationClientError):
    """ONE command produced no reply in time. The session and connection stay up; the late reply is discarded."""
    pass


class SessionEndedError(VoiceAutomationClientError):
    """The authenticated session itself is gone (connection lost / never started): START is required."""
    pass


class AuthenticationError(VoiceAutomationClientError):
    """Raised when IPC authentication with voice_automation_service fails."""
    pass


class VoiceAutomationClient:
    def __init__(self, socket_path: Optional[str] = None, timeout_s: float = 60.0):   # > service observe + act bounds
        self.socket_path = socket_path or get_default_socket_path()
        self.timeout_s = timeout_s
        self._sock: Optional[socket.socket] = None
        self.active_session_id: Optional[str] = None
        # The HTTP endpoint runs requests on a thread pool: one transaction at a time on the one socket.
        self._lock = threading.RLock()
        self._rbuf = bytearray()                         # bytes after the last complete line (kept across reads)

    def is_connected(self) -> bool:
        return self._sock is not None

    def _connect(self) -> None:
        if self._sock is not None:
            return

        if not os.path.exists(self.socket_path):
            raise ServiceUnavailableError("Signed voice_automation_service socket not found.")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        try:
            sock.connect(self.socket_path)
        except Exception as e:
            sock.close()
            raise ServiceUnavailableError(f"Failed to connect to voice_automation_service: {e}")

        token = get_or_create_ipc_token()
        if not token:
            sock.close()
            raise AuthenticationError("IPC authentication token not found in OS keyring.")

        req_id = f"auth_{uuid.uuid4().hex[:8]}"
        auth_msg = {"op": "AUTH", "request_id": req_id, "token": token}

        try:
            self._send_line(sock, auth_msg)
            resp = self._recv_line(sock)
        except Exception as e:
            sock.close()
            raise AuthenticationError("Authentication transport failure.") from None

        if resp.get("status") != "OK" or resp.get("request_id") != req_id:
            sock.close()
            raise AuthenticationError("IPC authentication rejected by signed service.")

        self._sock = sock

    def _send_line(self, sock: socket.socket, msg: Dict[str, Any]) -> None:
        try:
            raw = (json.dumps(msg) + "\n").encode("utf-8")
        except Exception as e:
            raise VoiceAutomationClientError("Failed to serialize IPC message.") from e

        if len(raw) > MAX_MESSAGE_BYTES:
            raise VoiceAutomationClientError("IPC message payload exceeds maximum size limit.")

        try:
            sock.sendall(raw)
        except Exception as e:
            raise VoiceAutomationClientError(f"IPC socket write error: {e}") from e

    def _recv_line(self, sock: socket.socket) -> Dict[str, Any]:
        while b"\n" not in self._rbuf:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                raise CommandTimeoutError("IPC socket read timeout.") from None
            except Exception as e:
                raise VoiceAutomationClientError(f"IPC socket read error: {e}") from e
            if not chunk:
                raise VoiceAutomationClientError("Connection closed by signed service.")
            self._rbuf.extend(chunk)
            if len(self._rbuf) > MAX_MESSAGE_BYTES:
                raise VoiceAutomationClientError("IPC response payload exceeds maximum size limit.")
        line, rest = bytes(self._rbuf).split(b"\n", 1)
        self._rbuf = bytearray(rest)
        try:
            data = json.loads(line.decode("utf-8"))
        except Exception as e:
            raise VoiceAutomationClientError(f"Failed to parse IPC JSON response: {e}") from e
        if not isinstance(data, dict):
            raise VoiceAutomationClientError("IPC response is not a valid JSON object.")
        return data

    def _send_recv(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._connect()
            assert self._sock is not None
            req_id = msg.get("request_id")
            try:
                self._send_line(self._sock, msg)
                # Replies arrive in request order; a reply to an earlier, timed-out request is stale: skip it.
                for _ in range(MAX_STALE_REPLIES + 1):
                    resp = self._recv_line(self._sock)
                    if resp.get("request_id") == req_id:
                        return resp
            except CommandTimeoutError:
                raise                                    # this command failed; the session and socket remain
            except Exception as e:
                self.close()                             # the transport itself is gone: the session ended
                raise SessionEndedError(f"IPC transaction failed: {e}") from e
            self.close()
            raise SessionEndedError("IPC request_id mismatch in server response.")

    def start(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            if not session_id or not isinstance(session_id, str):
                raise VoiceAutomationClientError("Invalid session_id.")

            if self.active_session_id and self.active_session_id != session_id:
                try:
                    self.stop(self.active_session_id)
                except Exception:
                    self.close()

            req_id = f"start_{uuid.uuid4().hex[:8]}"
            req = {"op": "START", "request_id": req_id, "session_id": session_id}
            resp = self._send_recv(req)
            if resp.get("status") == "OK":
                self.active_session_id = session_id
            return resp

    def command(self, session_id: str, transcript: str, allow_autostart: bool = True) -> Dict[str, Any]:
        with self._lock:
            if not session_id or not isinstance(session_id, str):
                raise VoiceAutomationClientError("Invalid session_id.")

            if not transcript or not isinstance(transcript, str) or len(transcript) > 2000:
                raise VoiceAutomationClientError("Invalid transcript length.")

            if self.active_session_id != session_id or self._sock is None:
                if allow_autostart:
                    self.start(session_id)
                else:
                    raise SessionEndedError("No active voice automation session. Must call START first.")

            req_id = f"cmd_{uuid.uuid4().hex[:8]}"
            req = {"op": "COMMAND", "request_id": req_id, "session_id": session_id, "transcript": transcript}
            # COMMAND does NOT retry on error/timeout because action state is unknown
            return self._send_recv(req)

    def stop(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            target_session = session_id or self.active_session_id
            if not target_session or self._sock is None:
                self.close()
                return {"status": "OK", "action_outcome": "SESSION_STOPPED"}

            req_id = f"stop_{uuid.uuid4().hex[:8]}"
            req = {"op": "STOP", "request_id": req_id, "session_id": target_session}
            try:
                resp = self._send_recv(req)
            except Exception as e:
                logger.warning("Error sending STOP to voice_automation_service: %s", e)
                resp = {"status": "ERROR", "error": "STOP_DELIVERY_FAILED", "action_outcome": "SESSION_STOP_FAILED"}
            finally:
                self.close()

            return resp

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self.active_session_id = None
        self._rbuf = bytearray()
