"""
open_app general OS command (02_TRD.md Section 3; 06_Acceptance_Criteria.md Section 7.4).

Fast and mocked: subprocess is faked, installation checks are faked, and no application is ever
launched. The adapter's own pytest guard stays on unless a test explicitly swaps in a fake.
"""

import json
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

import main
from main import app, init_db
from services import llm_client, os_adapter, speaker_verification
from services.github_intelligence import init_github_tables
from services.gmail_intelligence import init_gmail_tables
from services.tool_registry import TOOL_SPECS

client = TestClient(app)

SAFE_ARGV = ["osascript", "-e", "on run argv", "-e", "tell application id (item 1 of argv) to activate", "-e", "end run"]


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("subprocess.run called without a test fake")
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.fixture
def fake_mac(monkeypatch):
    """A faked macOS environment where every allow-listed app is 'installed'."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(os_adapter, "_launch_blocked", lambda: False)
    monkeypatch.setattr(os_adapter.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(os_adapter.MacOSAdapter, "is_installed", lambda self, app: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "open_app.db")
    init_db(path)
    init_github_tables(path)
    init_gmail_tables(path)
    monkeypatch.setattr(main, "DB_PATH", path)
    monkeypatch.setattr(main, "LAST_CHAT_CONTEXT", {"topic": None, "data": None})

    def planner_unavailable(message, tools, context=""):
        raise llm_client.LLMUnavailable("LLM_REQUEST_FAILED")
    monkeypatch.setattr(llm_client, "plan_tool_call", planner_unavailable)
    return path


def _plan(monkeypatch, tool_input):
    monkeypatch.setattr(llm_client, "plan_tool_call",
                        lambda m, t, context="": llm_client.PlannerDecision("open_app", tool_input, None))


# --- Registry ---

def test_open_app_is_registered_non_sensitive_without_confirmation(db):
    spec = TOOL_SPECS["open_app"]
    assert spec.is_sensitive is False and spec.requires_confirmation is False
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT is_sensitive, requires_confirmation, parameters_schema FROM tool_registry WHERE tool_name = 'open_app'").fetchone()
    assert row[0] == 0 and row[1] == 0
    schema = json.loads(row[2])
    assert schema["required"] == ["app_name"] and schema["additionalProperties"] is False
    assert schema["properties"]["app_name"]["pattern"] == "^[A-Za-z0-9 ]+$"


@pytest.mark.parametrize("bad", [
    {"app_name": "/Applications/Safari.app"},
    {"app_name": "Safari; rm -rf /"},
    {"app_name": 'tell application "Safari" to activate'},
    {"app_name": "$(open -a Calculator)"},
    {"app_name": "`whoami`"},
    {"app_name": "Safari\ndo shell script \"id\""},
    {"app_name": "A" * 41},
    {"app_name": ""},
    {"app_name": "Safari", "script": "do shell script \"id\""},
    {},
])
def test_invalid_parameters_are_rejected_before_the_handler(db, monkeypatch, fake_mac, bad):
    _plan(monkeypatch, bad)
    res = client.post("/chat", json={"message": "open something"}).json()
    assert res["tool"]["reason"] == "INVALID_ARGUMENTS"
    assert fake_mac == []


# --- Allow-list resolution ---

@pytest.mark.parametrize("name,canonical", [
    ("Safari", "Safari"), ("  safari ", "Safari"), ("SAFARI", "Safari"),
    ("Chrome", "Google Chrome"), ("google chrome", "Google Chrome"),
    ("Firefox", "Firefox"), ("Finder", "Finder"), ("Terminal", "Terminal"),
    ("Visual Studio Code", "Visual Studio Code"), ("VS Code", "Visual Studio Code"),
])
def test_allow_listed_names_resolve(name, canonical):
    assert os_adapter.resolve_app(name).canonical == canonical


@pytest.mark.parametrize("name", [
    "Photoshop", "Calculator", "/Applications/Safari.app", "Safari.app", "/bin/sh", "bash",
    "rm -rf /", 'tell application "Safari" to activate', "do shell script \"id\"",
    "Safari; rm -rf /", "Safari && open -a Calculator", "Safari\"", "osascript", "code", None, 42,
])
def test_everything_else_is_rejected(name):
    assert os_adapter.resolve_app(name) is None
    with pytest.raises(os_adapter.OpenAppError, match="APP_NOT_ALLOWED"):
        os_adapter.open_app(name)


# --- macOS adapter ---

def test_macos_adapter_uses_fixed_script_and_argv_without_shell(fake_mac):
    assert os_adapter.open_app("Chrome") == "Google Chrome"
    call = fake_mac[0]
    assert call["cmd"] == SAFE_ARGV + ["com.google.Chrome"]
    assert call["shell"] is False
    assert call["timeout"] == os_adapter.LAUNCH_TIMEOUT_SECONDS


def test_injection_text_never_reaches_the_script(fake_mac):
    with pytest.raises(os_adapter.OpenAppError):
        os_adapter.open_app('Safari" to quit\ndo shell script "id')
    assert fake_mac == []
    os_adapter.open_app("safari")
    assert fake_mac[0]["cmd"][:-1] == SAFE_ARGV and fake_mac[0]["cmd"][-1] == "com.apple.Safari"


def test_not_installed_app_is_reported_without_launching(fake_mac, monkeypatch):
    monkeypatch.setattr(os_adapter.MacOSAdapter, "is_installed", lambda self, app: False)
    with pytest.raises(os_adapter.OpenAppError, match="APP_NOT_INSTALLED"):
        os_adapter.open_app("Firefox")
    assert fake_mac == []


def test_installation_check_only_uses_code_defined_paths(monkeypatch):
    seen = []
    monkeypatch.setattr(os_adapter.os.path, "isdir", lambda p: seen.append(p) or False)
    assert os_adapter.MacOSAdapter().is_installed(os_adapter.resolve_app("Safari")) is False
    assert seen == ["/Applications/Safari.app"]


def test_unsupported_os(fake_mac, monkeypatch):
    monkeypatch.setattr(os_adapter.platform, "system", lambda: "Linux")
    with pytest.raises(os_adapter.OpenAppError, match="UNSUPPORTED_OS"):
        os_adapter.open_app("Safari")
    assert fake_mac == []


@pytest.mark.parametrize("failure", [
    lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, b"", b"execution error: /Users/someone/secret path (-1728)"),
    lambda cmd, **k: (_ for _ in ()).throw(FileNotFoundError("osascript")),
    lambda cmd, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 10)),
])
def test_subprocess_failures_are_reported_generically(fake_mac, monkeypatch, db, failure):
    monkeypatch.setattr(subprocess, "run", failure)
    res = client.post("/chat", json={"message": "Open Safari"}).json()
    assert res["reply"] == "I couldn't open Safari."
    assert res["app"] == {"app_name": "Safari", "opened": False, "reason": "LAUNCH_FAILED"}
    assert "secret path" not in json.dumps(res) and "-1728" not in json.dumps(res)


def test_fast_suite_guard_blocks_real_launches(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(os_adapter.OpenAppError, match="LAUNCH_FAILED"):
        os_adapter.open_app("Safari")  # _launch_blocked() is True under pytest
    assert calls == []


# --- Router: chat and voice ---

@pytest.mark.parametrize("message,canonical", [
    ("Open Safari", "Safari"), ("Launch Safari", "Safari"), ("Start Safari", "Safari"),
    ("Open Google Chrome", "Google Chrome"), ("Can you open Chrome for me?", "Google Chrome"),
])
def test_chat_natural_variations_select_open_app(db, fake_mac, message, canonical):
    res = client.post("/chat", json={"message": message}).json()
    assert res["tool"] == {"name": "open_app", "planner": "keyword", "status": "executed"}
    assert res["reply"] == f"Opening {canonical}."
    assert res["app"] == {"app_name": canonical, "opened": True}


def test_voice_uses_the_same_router_without_verification_or_confirmation(db, fake_mac, monkeypatch):
    verify_calls = []
    monkeypatch.setattr(speaker_verification, "verify", lambda audio: verify_calls.append(audio))

    chat = client.post("/chat", json={"message": "Open Safari"}).json()
    voice = client.post("/voice/command", json={"transcript": "Open Safari"}).json()

    assert chat["tool"] == voice["tool"] == {"name": "open_app", "planner": "keyword", "status": "executed"}
    assert chat["reply"] == voice["reply"] == "Opening Safari."
    assert verify_calls == []
    assert "tool_proposal" not in voice and "proposal" not in voice
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM calendar_actions").fetchone()[0] == 0
        log = conn.execute("SELECT resolved_tool, resolved_parameters, speaker_verified, executed FROM voice_command_log").fetchall()
    assert log == [("open_app", json.dumps({"app_name": "Safari"}), None, 1)]
    assert [c["cmd"][-1] for c in fake_mac] == ["com.apple.Safari", "com.apple.Safari"]


def test_llm_planner_contract_is_structured_arguments_only(db, fake_mac, monkeypatch):
    _plan(monkeypatch, {"app_name": "Chrome"})
    res = client.post("/voice/command", json={"transcript": "could you pull up my browser"}).json()
    assert res["tool"] == {"name": "open_app", "planner": "llm", "status": "executed"}
    assert fake_mac[0]["cmd"] == SAFE_ARGV + ["com.google.Chrome"]


def test_unapproved_app_from_planner_is_refused_without_launch(db, fake_mac, monkeypatch):
    _plan(monkeypatch, {"app_name": "Photoshop"})
    res = client.post("/chat", json={"message": "open photoshop"}).json()
    assert res["app"]["reason"] == "APP_NOT_ALLOWED"
    assert "Safari, Google Chrome, Firefox, Finder, Terminal, Visual Studio Code" in res["reply"]
    assert res["ui"] == {"type": "notice", "data": {"reason": "APP_NOT_ALLOWED"}}
    assert fake_mac == []


def test_unsupported_os_reply_via_router(db, fake_mac, monkeypatch):
    monkeypatch.setattr(os_adapter.platform, "system", lambda: "Windows")
    res = client.post("/chat", json={"message": "Open Safari"}).json()
    assert res["reply"] == "Opening apps is only supported on macOS right now."
    assert fake_mac == []


def test_non_app_messages_are_not_captured(db):
    for message in ("start code review", "what PRs are open", "open findings", "What is my security score?"):
        res = client.post("/chat", json={"message": message}).json()
        assert res["tool"]["name"] != "open_app", message
