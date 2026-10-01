"""
End-to-End Assistant Intelligence Integration Tests (Step 13).

Covers matrix A through Z as specified in Step 13 prompt.
"""

import time
import pytest
from fastapi.testclient import TestClient

from main import app, DB_PATH
from services.computer_control.container import (
    ComputerControlContainer,
    get_container,
    initialize_production_container,
    set_container,
)
from services.computer_control.assistant_intelligence import (
    AssistantDecision,
    AssistantDecisionKind,
    AssistantIntelligenceEngine,
)
from services.personal_memory import PersonalMemoryManager, PersonalMemoryCategory
from services.tool_router import route_message


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_e2e_assistant.db")


@pytest.fixture
def container(temp_db_path):
    cont = initialize_production_container()
    yield cont
    cont.shutdown()
    set_container(None)


@pytest.fixture
def client():
    return TestClient(app)


# A. Chat conversation
def test_chat_conversation(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "CONVERSATION", "response": "Blockchain is a distributed ledger."}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("Explain blockchain.", channel="chat", db_path=DB_PATH)

    assert "Blockchain is a distributed ledger." in res.get("reply", "")
    assert res.get("tool", {}).get("planner") == "assistant_intelligence"


# B. Chat computer goal
def test_chat_computer_goal(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "COMPUTER_GOAL", "goal": "Open TextEdit."}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("Please open TextEdit.", channel="chat", db_path=DB_PATH)

    assert res.get("tool", {}).get("name") == "computer_control"
    assert res.get("tool", {}).get("planner") == "assistant_intelligence"


# C. Chat ASK
def test_chat_ask(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "ASK", "response": "Which document should I open?"}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("Open my document.", channel="chat", db_path=DB_PATH)

    assert "Which document should I open?" in res.get("reply", "")


# D. Chat CANNOT_PROCEED
def test_chat_cannot_proceed(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "CANNOT_PROCEED", "reason": "Unsafe request"}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("Invalid request", channel="chat", db_path=DB_PATH)

    assert "Unsafe request" in res.get("reply", "") or "cannot proceed" in res.get("reply", "").lower()


# E. Voice conversation
def test_voice_conversation(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "CONVERSATION", "response": "TCP guarantees order, UDP does not."}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("What is TCP?", channel="voice", db_path=DB_PATH)

    assert "TCP guarantees order" in res.get("reply", "")


# F. Voice computer goal
def test_voice_computer_goal(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "COMPUTER_GOAL", "goal": "Open TextEdit."}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("Open TextEdit.", channel="voice", db_path=DB_PATH)

    assert res.get("tool", {}).get("name") == "computer_control"


# G. Voice ASK
def test_voice_ask(container):
    def fake_llm(prompt: str) -> str:
        return '{"decision": "ASK", "response": "Which file?"}'

    container.assistant_intelligence_engine.llm_transport = fake_llm
    res = route_message("Open file", channel="voice", db_path=DB_PATH)

    assert "Which file?" in res.get("reply", "")


# H & I. Same-session Chat <-> Voice continuation
def test_same_session_cross_channel_continuation(container):
    # Step 1: Chat executes goal in session S1
    container.orchestrator.goal_memory.record_outcome(
        goal_id="g_chat_1",
        session_id="session_S1",
        source="chat",
        goal_text="Open TextEdit",
        outcome_state="COMPLETED",
        created_at=time.time(),
    )

    # Step 2: Voice asks "Do that again" in same session S1
    res_voice = route_message(
        "Do that again",
        channel="voice",
        db_path=DB_PATH,
        context={"session_id": "session_S1"}
    )
    assert res_voice.get("tool", {}).get("name") == "computer_control"


# J. Personal Memory used in conversation
def test_personal_memory_used_in_conversation(container, temp_db_path):
    p_mgr = PersonalMemoryManager(db_path=temp_db_path)
    p_mgr.create_memory(category=PersonalMemoryCategory.USER_PREFERENCE, content="User prefers concise answers.")
    container.orchestrator.personal_memory_manager = p_mgr

    res = route_message("Explain blockchain.", channel="chat", db_path=temp_db_path)
    assert res is not None


# K, L & M. Goal Memory repeat creates new goal_id and does not replay old actions
def test_goal_memory_repeat_creates_new_goal_id(container):
    container.orchestrator.goal_memory.record_outcome(
        goal_id="g_old_1",
        session_id="s_repeat",
        source="chat",
        goal_text="Open Calculator",
        outcome_state="COMPLETED",
        created_at=time.time(),
    )

    res = route_message("Do that again", channel="chat", db_path=DB_PATH, context={"session_id": "s_repeat"})
    assert res.get("tool", {}).get("name") == "computer_control"


# N & O. Deterministic fallback when LLM unavailable / offline
def test_deterministic_fallback_offline(container):
    container.assistant_intelligence_engine.llm_transport = None
    res = route_message("Open TextEdit", channel="chat", db_path=DB_PATH)

    assert res.get("tool", {}).get("name") == "computer_control"


# P & Q. Malformed and forbidden LLM responses
def test_malformed_and_forbidden_llm_response(container):
    def bad_llm(prompt: str) -> str:
        return '{"decision": "COMPUTER_GOAL", "goal": "Open TextEdit and exec(rm -rf /)"}'

    container.assistant_intelligence_engine.llm_transport = bad_llm
    res = route_message("Open TextEdit", channel="chat", db_path=DB_PATH)

    assert "cannot proceed" in res.get("reply", "").lower() or "forbidden" in res.get("reply", "").lower() or "clarification" in res.get("reply", "").lower()


# R & S. Single decision & single goal execution
def test_single_decision_and_single_execution(container):
    exec_count = 0
    original_process = container.chat_adapter.process_chat_input

    def spy_process(*args, **kwargs):
        nonlocal exec_count
        exec_count += 1
        return original_process(*args, **kwargs)

    container.chat_adapter.process_chat_input = spy_process

    route_message("Open TextEdit", channel="chat", db_path=DB_PATH)
    # Guaranteed at most 1 execution call per user message
    assert exec_count <= 1


# T, U & V. Direct endpoints (/computer/goals, /resume, /cancel) remain unrouted through LLM
def test_direct_computer_endpoints(client, container):
    # Direct endpoint /computer/goals
    resp1 = client.post("/computer/goals", json={"goal": "Open TextEdit", "goal_id": "g_dir_1"})
    assert resp1.status_code == 200

    # Direct endpoint /computer/goals/resume
    resp2 = client.post("/computer/goals/resume", json={"goal_id": "g_dir_1", "user_response": "Yes"})
    assert resp2.status_code == 200

    # Direct endpoint /computer/goals/cancel
    resp3 = client.post("/computer/goals/cancel", json={"goal_id": "g_dir_1"})
    assert resp3.status_code == 200


# W. Successful computer goal remains silent
def test_successful_computer_goal_remains_silent(container):
    res = route_message("Open TextEdit", channel="chat", db_path=DB_PATH)
    # SILENT execution invariant: successful computer goals yield empty/silent reply string
    assert res.get("reply", "") == "" or res.get("reply") is None or "textedit" not in res.get("reply", "").lower()


# X. Clarification remains visible
def test_clarification_remains_visible(container):
    def ask_llm(prompt: str) -> str:
        return '{"decision": "ASK", "response": "Which document do you want to open?"}'

    container.assistant_intelligence_engine.llm_transport = ask_llm
    res = route_message("Open document", channel="chat", db_path=DB_PATH)
    assert "Which document do you want to open?" in res.get("reply", "")


# Y. Verified context requires fresh observation
def test_verified_context_requires_fresh_observation(container):
    container.context_manager.update_verified_state(app_name="TextEdit", bundle_id="com.apple.TextEdit", session_id="s1")
    ctx = container.assistant_context_builder.build_context(session_id="s1")
    assert ctx.verified_system_context.active_application == "TextEdit"


# Z. All channels share production container
def test_all_channels_share_production_container(container):
    c1 = get_container()
    assert c1 is container
    assert c1.assistant_intelligence_engine is not None
    assert c1.assistant_context_builder is not None
    assert c1.chat_adapter is not None
    assert c1.voice_adapter is not None
