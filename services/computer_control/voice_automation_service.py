"""
Persistent Voice Automation Service (docs/07_Computer_Control.md Section 34).

Provides a local Unix domain socket (UDS) IPC service running inside the signed Evie
identity process. Exposes a closed IPC protocol (START, COMMAND, STOP) authenticated
via the OS keyring vault (`keyring_utils.py`).

Security Boundary:
- Runs as a persistent accessory process under signed Evie.app launcher mode `voice_automation_service`.
- Requires and verifies production identity (`assess_production_identity`) before executing commands.
- Local Unix domain socket at `~/.evie/voice_automation.sock` with restrictive `0o600` permissions.
- Authenticated via secret stored in OS keyring (`keyring_utils.py`), constant-time compared.
- Reject fail-closed on malformed, oversized, unauthenticated, or unexpected IPC messages.
- Never accepts raw code, shell commands, AppleScript, or selectors from IPC clients.
"""

import json
import logging
import os
import secrets
import socket
import sys
from typing import Any, Dict, Optional, Tuple

import keyring_utils
from services import computer_control_router
from services.computer_control_log import SqliteStepLog
from services.computer_control_router import ComputerControlRuntime, set_runtime
from services.computer_control.worker import ComputerControlWorker

logger = logging.getLogger(__name__)

KEYRING_SERVICE_NAME = keyring_utils.SERVICE_NAME
IPC_KEY_NAME = "voice_automation_ipc_token"
MAX_MESSAGE_BYTES = 65536


def get_default_socket_path() -> str:
    evie_dir = os.path.expanduser("~/.evie")
    os.makedirs(evie_dir, mode=0o700, exist_ok=True)
    return os.path.join(evie_dir, "voice_automation.sock")


def get_or_create_ipc_token() -> str:
    token = keyring_utils.get_credential(KEYRING_SERVICE_NAME, IPC_KEY_NAME)
    if not token:
        token = secrets.token_hex(32)
        keyring_utils.set_credential(KEYRING_SERVICE_NAME, IPC_KEY_NAME, token)
    return token


DEFAULT_DB_PATH = os.getenv("EVIE_DB_PATH", os.path.expanduser("~/.evie/evie.db"))


class VoiceAutomationService:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, socket_path: Optional[str] = None):
        self.db_path = db_path
        self.socket_path = socket_path or get_default_socket_path()
        self.worker: Optional[ComputerControlWorker] = None
        self.runtime: Optional[ComputerControlRuntime] = None
        self.active_session_id: Optional[str] = None

    def start_runtime(self) -> None:
        if self.worker is None:
            # act must outlast the executor's own bounds (cold-launch verification 10 s + settle + re-observation);
            # a shorter timeout abandons a running action and desynchronises the next reply.
            self.worker = ComputerControlWorker(start_timeout_s=10, probe_timeout_s=10, observe_timeout_s=40,
                                                act_timeout_s=45)
            self.worker.start()
            step_log = SqliteStepLog(self.db_path)
            self.runtime = ComputerControlRuntime(self.worker, step_log)
            set_runtime(self.runtime)

    def stop_runtime(self) -> None:
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None
            self.runtime = None
            set_runtime(None)

    def handle_message(self, raw_data: bytes, authenticated: bool) -> Tuple[Dict[str, Any], bool]:
        """
        A failure while handling ONE message is a bounded error reply on the same connection - never a reason to
        drop the connection, the session or the runtime. Only a dead worker is reported as a SESSION failure.
        """
        try:
            return self._handle_message(raw_data, authenticated)
        except Exception as e:
            try:
                req = json.loads(raw_data.decode("utf-8"))
                request_id = str(req.get("request_id") or "") if isinstance(req, dict) else ""
                session_id = str(req.get("session_id") or "") if isinstance(req, dict) else ""
            except Exception:
                request_id = session_id = ""
            logger.warning("Message handling failed: %s", type(e).__name__)
            if self._worker_failed():
                return {"status": "ERROR", "request_id": request_id, "session_id": session_id,
                        "error": "SESSION_FAILED", "session_failed": True}, authenticated
            return {"status": "ERROR", "request_id": request_id, "session_id": session_id,
                    "error": f"COMMAND_FAILED:{type(e).__name__}"[:120]}, authenticated

    def _worker_failed(self) -> bool:
        from services.computer_control.worker import WorkerState
        status = getattr(self.worker, "status", None) if self.worker is not None else None
        return status is not None and status.state in (WorkerState.FAILED, WorkerState.STOPPED)

    def _handle_message(self, raw_data: bytes, authenticated: bool) -> Tuple[Dict[str, Any], bool]:
        if len(raw_data) > MAX_MESSAGE_BYTES:
            return {"status": "ERROR", "error": "OVERSIZED_MESSAGE"}, authenticated

        try:
            req = json.loads(raw_data.decode("utf-8"))
        except Exception:
            return {"status": "ERROR", "error": "MALFORMED_MESSAGE"}, authenticated

        if not isinstance(req, dict):
            return {"status": "ERROR", "error": "MALFORMED_MESSAGE"}, authenticated

        op = req.get("op")
        request_id = str(req.get("request_id") or "")
        session_id = str(req.get("session_id") or "")

        if not authenticated:
            if op == "AUTH":
                token = req.get("token")
                expected_token = get_or_create_ipc_token()
                if token and isinstance(token, str) and secrets.compare_digest(token, expected_token):
                    return {"status": "OK", "request_id": request_id, "action_outcome": "AUTHENTICATED"}, True
                return {"status": "UNAUTHENTICATED", "request_id": request_id, "error": "AUTHENTICATION_FAILED"}, False
            return {"status": "UNAUTHENTICATED", "request_id": request_id, "error": "AUTHENTICATION_REQUIRED"}, False

        # Authenticated operations
        if op == "START":
            if not session_id:
                return {"status": "ERROR", "request_id": request_id, "error": "SESSION_ID_REQUIRED"}, True
            self.active_session_id = session_id
            self.start_runtime()
            ok, reasons = self.runtime.identity() if self.runtime else (False, ["NO_RUNTIME"])
            diag = {}
            try:
                probe = self.worker.status.probe if (self.worker and hasattr(self.worker, "status") and self.worker.status) else None
                if probe:
                    ident = probe.identity
                    resp = ident.responsible if ident else None
                    wrk = ident.worker if ident else None
                    diag = {
                        "responsible_bundle_id": str(resp.bundle_id) if resp and resp.bundle_id else None,
                        "responsible_executable": str(resp.executable) if resp and resp.executable else None,
                        "worker_executable": str(wrk.executable) if wrk and wrk.executable else None,
                        "signature_valid": bool(resp.signature.valid) if resp and resp.signature else False,
                        "worker_signature_valid": bool(wrk.signature.valid) if wrk and wrk.signature else False,
                        "team_id": str(resp.signature.team_id) if resp and resp.signature else None,
                        "accessibility_trust": str(probe.accessibility.status.value) if probe.accessibility and hasattr(probe.accessibility.status, "value") else str(probe.accessibility.status if probe.accessibility else None),
                        "functional_ax_read": str(probe.functional.status) if probe.functional else None,
                        "production_identity_verified": bool(ok),
                        "production_identity_reasons": [str(r) for r in reasons],
                    }
            except Exception as e:
                logger.warning("Error assembling diagnostics: %s", e)
            return {
                "status": "OK",
                "request_id": request_id,
                "session_id": session_id,
                "action_outcome": "SESSION_STARTED",
                "diagnostics": diag,
            }, True

        elif op == "COMMAND":
            if not session_id or session_id != self.active_session_id:
                return {"status": "ERROR", "request_id": request_id, "session_id": session_id, "error": "INVALID_SESSION"}, True

            transcript = req.get("transcript")
            if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 2000:
                return {"status": "ERROR", "request_id": request_id, "session_id": session_id, "error": "INVALID_TRANSCRIPT"}, True

            if self.runtime:
                ok, reasons = self.runtime.identity()
                if not ok:
                    return {
                        "status": "ERROR",
                        "request_id": request_id,
                        "session_id": session_id,
                        "error": "IDENTITY_NOT_VERIFIED",
                        "reasons": list(reasons),
                    }, True

            summary = computer_control_router.handle(transcript)
            status = summary.get("status")
            final_state = summary.get("final_state")

            if final_state == "ASKING":
                ask_reason = computer_control_router.question_for(summary.get("ask_reason"))
                return {
                    "status": "ASKING",
                    "request_id": request_id,
                    "session_id": session_id,
                    "ask_reason": ask_reason,
                    "action_outcome": "ASKING",
                    "summary": summary,
                }, True

            if status == "SESSION_ENDED" and final_state in ("DONE_VERIFIED", "DONE_UNVERIFIED"):
                return {
                    "status": "OK",
                    "request_id": request_id,
                    "session_id": session_id,
                    "action_outcome": "EXECUTED",
                    "summary": summary,
                }, True

            error_reply = computer_control_router.reply_for(summary)
            return {
                "status": "ERROR",
                "request_id": request_id,
                "session_id": session_id,
                "error": error_reply,
                "summary": summary,
            }, True

        elif op == "STOP":
            self.active_session_id = None
            self.stop_runtime()
            return {"status": "OK", "request_id": request_id, "session_id": session_id, "action_outcome": "SESSION_STOPPED"}, True

        else:
            return {"status": "ERROR", "request_id": request_id, "error": "UNKNOWN_OPERATION"}, True

    def on_disconnect(self) -> None:
        """Clean up active session state when client disconnects."""
        self.active_session_id = None
        self.stop_runtime()

    def run_server(self, stop_event=None) -> None:
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server_sock.listen(5)
        server_sock.settimeout(1.0)

        logger.info("VoiceAutomationService listening on %s", self.socket_path)

        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    conn, _ = server_sock.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break

                with conn:
                    conn.settimeout(5.0)
                    authenticated = False
                    buffer = b""
                    while stop_event is None or not stop_event.is_set():
                        try:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            buffer += chunk
                            if b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                resp, authenticated = self.handle_message(line, authenticated)
                                conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                                if resp.get("status") == "UNAUTHENTICATED":
                                    break
                        except socket.timeout:
                            continue
                        except Exception:
                            break
                    self.on_disconnect()
        finally:
            try:
                server_sock.close()
            except Exception:
                pass
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
            self.stop_runtime()


def main():
    service = VoiceAutomationService()
    service.run_server()


if __name__ == "__main__":
    main()
