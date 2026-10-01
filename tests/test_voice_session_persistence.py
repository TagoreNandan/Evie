"""
Regression: a failed / blocked / raising / timed-out COMMAND must not end the voice automation session.
Real VoiceAutomationService + real VoiceAutomationClient over a real Unix socket; only the computer-control
runtime and router are faked (no worker, no macOS).
"""

import os
import threading
import time
import uuid

import pytest

from services.computer_control import voice_automation_client as client_mod
from services.computer_control import voice_automation_service as service_mod
from services.computer_control.voice_automation_client import (
    CommandTimeoutError, SessionEndedError, VoiceAutomationClient,
)

TOKEN = "f" * 64
OK = {"status": "SESSION_ENDED", "final_state": "DONE_VERIFIED", "actions": [{"op": "activate_app"}]}
BLOCKED = {"status": "SESSION_ENDED", "final_state": "BLOCKED", "stop_reason": "CHOOSER_BLOCKED:CONTROL_NOT_FOUND",
           "actions": []}


class FakeRuntime:
    def identity(self):
        return True, []


@pytest.fixture
def service(monkeypatch, tmp_path):
    path = f"/tmp/evie-vs-{uuid.uuid4().hex[:8]}.sock"
    monkeypatch.setattr(service_mod, "get_or_create_ipc_token", lambda: TOKEN)
    monkeypatch.setattr(client_mod, "get_or_create_ipc_token", lambda: TOKEN)
    svc = service_mod.VoiceAutomationService(db_path=str(tmp_path / "v.db"), socket_path=path)
    starts = []

    def start_runtime():
        starts.append(1)
        svc.runtime = FakeRuntime()
    monkeypatch.setattr(svc, "start_runtime", start_runtime)
    monkeypatch.setattr(svc, "stop_runtime", lambda: setattr(svc, "runtime", None))
    script = {}

    def handle(transcript):
        outcome = script.get(transcript, OK)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, float):
            time.sleep(outcome)
            return OK
        return outcome
    monkeypatch.setattr(service_mod.computer_control_router, "handle", handle)
    stop = threading.Event()
    thread = threading.Thread(target=svc.run_server, kwargs={"stop_event": stop}, daemon=True)
    thread.start()
    for _ in range(100):
        if os.path.exists(path):
            break
        time.sleep(0.02)
    yield svc, path, script, starts
    stop.set()
    thread.join(timeout=3)


def test_failed_command_does_not_end_the_session(service):
    svc, path, script, starts = service
    script["press 2 plus 2"] = BLOCKED
    script["explode"] = RuntimeError("worker said no")
    c = VoiceAutomationClient(socket_path=path, timeout_s=5)
    assert c.start("s1")["status"] == "OK"
    assert c.command("s1", "open calculator", allow_autostart=False)["action_outcome"] == "EXECUTED"
    assert c.active_session_id == "s1" and svc.active_session_id == "s1"

    r = c.command("s1", "press 2 plus 2", allow_autostart=False)            # bounded failure
    assert r["status"] == "ERROR" and "control" in r["error"]
    assert c.active_session_id == "s1" and svc.active_session_id == "s1"

    r = c.command("s1", "explode", allow_autostart=False)                   # exception inside the service
    assert r["status"] == "ERROR" and r["error"].startswith("COMMAND_FAILED") and not r.get("session_failed")
    assert c.active_session_id == "s1" and svc.active_session_id == "s1"

    assert c.command("s1", "open textedit", allow_autostart=False)["action_outcome"] == "EXECUTED"
    assert c.active_session_id == "s1" and starts == [1]                     # no implicit START
    assert c.stop("s1")["status"] == "OK"
    assert c.active_session_id is None and svc.active_session_id is None


def test_timed_out_command_keeps_the_session_and_the_late_reply_is_discarded(service):
    svc, path, script, _ = service
    script["slow"] = 1.5
    c = VoiceAutomationClient(socket_path=path, timeout_s=0.5)
    c.start("s2")
    with pytest.raises(CommandTimeoutError):
        c.command("s2", "slow", allow_autostart=False)
    assert c.active_session_id == "s2"
    c.timeout_s = 5
    c._sock.settimeout(5)
    r = c.command("s2", "next", allow_autostart=False)                       # late "slow" reply skipped
    assert r["action_outcome"] == "EXECUTED" and r["request_id"].startswith("cmd_")
    c.stop("s2")


def test_concurrent_commands_from_the_http_thread_pool_do_not_corrupt_the_session(service):
    svc, path, script, _ = service
    script["a"] = 0.3
    c = VoiceAutomationClient(socket_path=path, timeout_s=5)
    c.start("s3")
    results = []
    threads = [threading.Thread(target=lambda t=t: results.append(c.command("s3", t, allow_autostart=False)))
               for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r["action_outcome"] for r in results] == ["EXECUTED"] * 3 and c.active_session_id == "s3"
    c.stop("s3")


def test_lost_connection_is_a_session_failure_not_a_command_failure(service):
    svc, path, script, _ = service
    c = VoiceAutomationClient(socket_path=path, timeout_s=5)
    c.start("s4")
    c._sock.close()                                                          # the transport is gone
    with pytest.raises(SessionEndedError):
        c.command("s4", "anything", allow_autostart=False)
    with pytest.raises(SessionEndedError):
        c.command("s4", "anything", allow_autostart=False)                  # START required, no blind retry
