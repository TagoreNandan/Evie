"""
Focused Unit & Integration Tests for Evie Voice Automation Mode.
"""

import os
import pytest
from fastapi.testclient import TestClient

from main import app, DB_PATH, init_db
from services.computer_control.voice_automation_client import ServiceUnavailableError

TEST_DB = "test_voice_automation_evie.db"


class FakeVoiceAutomationClient:
    def __init__(self):
        self.active_session_id = None
        self.commands_sent = []
        self.fail_start = False
        self.fail_stop = False

    def start(self, session_id):
        if self.fail_start:
            return {"status": "ERROR", "error": "Signed service failed to initialize."}
        self.active_session_id = session_id
        return {"status": "OK", "session_id": session_id, "action_outcome": "SESSION_STARTED"}

    def command(self, session_id, transcript, allow_autostart=True):
        self.commands_sent.append((session_id, transcript))
        if "ask" in transcript:
            return {"status": "ASKING", "action_outcome": "ASK", "ask_reason": "Which tab do you mean?"}
        if "fail" in transcript:
            return {"status": "ERROR", "error": "Safety gate blocked operation."}
        return {"status": "OK", "action_outcome": "EXECUTED"}

    def stop(self, session_id=None):
        if self.fail_stop:
            return {"status": "ERROR", "error": "STOP_DELIVERY_FAILED", "action_outcome": "SESSION_STOP_FAILED"}
        self.active_session_id = None
        return {"status": "OK", "action_outcome": "SESSION_STOPPED"}


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    init_db(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_ui_contains_separate_voice_automation_controls():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="automationBtn"' in response.text
    assert 'id="automationStatusText"' in response.text
    assert 'Start Voice Automation' in response.text


def test_static_app_js_has_voice_automation_session_guards():
    client = TestClient(app)
    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    js_text = res_js.text

    # Lifecycle invariants
    assert "toggleVoiceAutomation" in js_text
    assert "executeVoiceAutomationCommand" in js_text
    assert "stopVoiceAutomation" in js_text
    assert "currentAutomationSessionId" in js_text
    assert "lastExecutedResultKey" in js_text

    # START invariant: backend START called before setting active session and starting recognition
    assert "/voice/automation/start" in js_text
    assert "data.status !== 'OK'" in js_text

    # STOP invariant: synchronous local session invalidation before fetch
    assert "currentAutomationSessionId = null" in js_text
    assert "/voice/automation/stop" in js_text

    # Race safety: re-check session_id after await fetch and res.json
    assert "currentAutomationSessionId !== sessionId" in js_text

    # Silence on success
    assert "data.status === 'ASKING'" in js_text
    assert "data.status === 'ERROR'" in js_text


def test_voice_automation_endpoint_uses_ipc_client_no_open_app_bypass(monkeypatch):
    client = TestClient(app)
    fake_client = FakeVoiceAutomationClient()
    monkeypatch.setattr("main.get_voice_automation_client", lambda: fake_client)

    # "open Safari" used to bypass to os_adapter.open_app. Now it must go through IPC client!
    res = client.post("/voice/automation", json={"transcript": "open Safari", "session_id": "sess_123"}).json()
    assert res["status"] == "EXECUTED"
    assert res["reply"] is None
    assert ("sess_123", "open Safari") in fake_client.commands_sent


def test_voice_automation_endpoint_isolated_from_chat(monkeypatch):
    client = TestClient(app)
    fake_client = FakeVoiceAutomationClient()
    monkeypatch.setattr("main.get_voice_automation_client", lambda: fake_client)

    res = client.post("/voice/automation", json={"transcript": "open Terminal", "session_id": "sess_456"}).json()
    assert "reply" in res
    assert res["reply"] is None
    assert "tone" not in res
    assert "ui" not in res


def test_voice_automation_endpoint_service_unavailable(monkeypatch):
    client = TestClient(app)

    class OfflineClient:
        def command(self, session_id, transcript, allow_autostart=True):
            raise ServiceUnavailableError("Signed service socket not found.")

    monkeypatch.setattr("main.get_voice_automation_client", lambda: OfflineClient())
    res = client.post("/voice/automation", json={"transcript": "type hello", "session_id": "sess_789"}).json()
    assert res["status"] == "SESSION_ENDED"                 # the session itself is gone: the UI must reflect it
    assert "start it again" in res["reply"]


def test_voice_automation_clarification_response(monkeypatch):
    client = TestClient(app)
    fake_client = FakeVoiceAutomationClient()
    monkeypatch.setattr("main.get_voice_automation_client", lambda: fake_client)

    res = client.post("/voice/automation", json={"transcript": "click ask tab", "session_id": "sess_ask"}).json()
    assert res["status"] == "ASKING"
    assert res["reply"] == "Which tab do you mean?"


def test_voice_automation_start_stop_endpoints(monkeypatch):
    client = TestClient(app)
    fake_client = FakeVoiceAutomationClient()
    monkeypatch.setattr("main.get_voice_automation_client", lambda: fake_client)

    res_start = client.post("/voice/automation/start", json={"session_id": "sess_lifecycle"}).json()
    assert res_start["status"] == "OK"
    assert fake_client.active_session_id == "sess_lifecycle"

    res_stop = client.post("/voice/automation/stop", json={"session_id": "sess_lifecycle"}).json()
    assert res_stop["status"] == "OK"
    assert fake_client.active_session_id is None


def test_voice_automation_start_stop_error_handling(monkeypatch):
    client = TestClient(app)
    fake_client = FakeVoiceAutomationClient()
    fake_client.fail_start = True
    fake_client.fail_stop = True
    monkeypatch.setattr("main.get_voice_automation_client", lambda: fake_client)

    res_start = client.post("/voice/automation/start", json={"session_id": "sess_fail_start"}).json()
    assert res_start["status"] == "ERROR"

    res_stop = client.post("/voice/automation/stop", json={"session_id": "sess_fail_stop"}).json()
    assert res_stop["status"] == "ERROR"

