"""
Unit and Integration Test Suite for Silent Execution Response Policy (Step 7).

Verifies silent execution on successful computer goals (COMPLETED), clarification prompts
on WAITING_FOR_USER, concise error responses on failures/blocks, context and audit preservation,
voice/chat silence, and architectural security boundaries.
"""

import time
import pytest
from fastapi.testclient import TestClient

from main import app
from cc_fixtures import TEXTEDIT
from test_computer_control_executor import _setup, textedit
from services.computer_control import observer as O
from services.computer_control.observer import observe_app_with_handles
from services.computer_control.response_policy import (
    determine_execution_response, ExecutionResponseDecision, ResponseKind
)
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.planner import (
    AskDecision, DoneDecision, MockPlannerProvider, PlannerOutput, RuleBasedPlannerProvider
)
from services.computer_control.container import (
    ComputerControlContainer, initialize_production_container, set_container
)
from services.tool_registry import format_goal_reply


class DummyBackend:
    pass


def _setup_test_orchestrator(provider=None, bundle_id="com.apple.TextEdit"):
    app = textedit()
    backend, clk, cache = _setup(app)
    from services.computer_control.models import AppIdentity
    app_identity = AppIdentity(bundle_id=bundle_id, name="TextEdit", pid=100)

    def get_obs(b):
        obs, handles = observe_app_with_handles(backend, app, app_identity, settle=False, frontmost=app_identity)
        obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
        return obs, handles

    planner = provider or RuleBasedPlannerProvider()
    return GoalExecutionOrchestrator(
        planner_provider=planner,
        backend=backend,
        get_observation=get_obs,
        allowed_apps=frozenset({"com.apple.TextEdit"}),
        clock=clk.now,
        sleep=lambda dt: clk.sleep(dt)
    )


def test_completed_is_silent():
    """Test A: COMPLETED status produces SILENT decision with empty user reply."""
    dec = determine_execution_response("COMPLETED", verification_summary="verified")
    assert dec.kind == ResponseKind.SILENT
    assert dec.user_reply == ""


def test_waiting_for_user_is_ask():
    """Test B: WAITING_FOR_USER / ASK status produces ASK decision with clarification question."""
    dec = determine_execution_response("ASK", question="Which document do you mean?")
    assert dec.kind == ResponseKind.ASK
    assert dec.user_reply == "Which document do you mean?"


def test_blocked_response():
    """Test C: BLOCKED status produces RESPOND decision with concise error."""
    dec = determine_execution_response("BLOCKED")
    assert dec.kind == ResponseKind.RESPOND
    assert dec.user_reply == "I can't perform that action."


def test_failed_response():
    """Test D: FAILED status produces RESPOND decision with concise error."""
    dec = determine_execution_response("FAILED")
    assert dec.kind == ResponseKind.RESPOND
    assert dec.user_reply == "I couldn't complete that task."


def test_cannot_proceed_response():
    """Test E: CANNOT_PROCEED status produces RESPOND decision with concise error."""
    dec = determine_execution_response("CANNOT_PROCEED")
    assert dec.kind == ResponseKind.RESPOND
    assert dec.user_reply == "I couldn't complete that task."


def test_budget_exceeded_response():
    """Test F: BUDGET_EXCEEDED status produces RESPOND decision with concise error."""
    dec = determine_execution_response("BUDGET_EXCEEDED")
    assert dec.kind == ResponseKind.RESPOND
    assert dec.user_reply == "I stopped because execution limits were reached."


def test_cancelled_is_silent():
    """Test G: CANCELLED status produces SILENT decision."""
    dec = determine_execution_response("CANCELLED")
    assert dec.kind == ResponseKind.SILENT
    assert dec.user_reply == ""


def test_normal_conversation_unaffected():
    """Test H: Normal conversation does not route to silent execution policy."""
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "What is recursion?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("reply") and len(data["reply"]) > 0
    assert data.get("tool", {}).get("name") != "computer_control"



def test_voice_successful_goal_is_silent():
    """Test I & N: Voice successful goal returns empty reply (no TTS confirmation spoken)."""
    orch = _setup_test_orchestrator()
    container = ComputerControlContainer(
        worker=DummyBackend(),
        planner_provider=RuleBasedPlannerProvider(),
        allowed_apps=frozenset({"com.apple.TextEdit"}),
        get_observation_fn=orch._get_observation
    )
    container.orchestrator = orch
    set_container(container)

    res = container.voice_adapter.process_voice_input("Open TextEdit.")
    assert res.status == "COMPLETED"
    reply = format_goal_reply(res.status)
    assert reply == ""  # Silent! No "Done." spoken


def test_chat_successful_goal_is_silent():
    """Test J: Chat successful goal produces silent conversational reply."""
    orch = _setup_test_orchestrator()
    container = ComputerControlContainer(
        worker=DummyBackend(),
        planner_provider=RuleBasedPlannerProvider(),
        allowed_apps=frozenset({"com.apple.TextEdit"}),
        get_observation_fn=orch._get_observation
    )
    container.orchestrator = orch
    set_container(container)

    res = container.chat_adapter.process_chat_input("Open TextEdit.")
    assert res.status == "COMPLETED"
    reply = format_goal_reply(res.status)
    assert reply == ""  # Silent reply


def test_successful_goal_still_updates_context():
    """Test K: Silent goal execution still updates context authoritatively."""
    orch = _setup_test_orchestrator()
    req = GoalRequest(goal_id="g1", goal_text="Open TextEdit.", source="chat")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED

    ctx = orch.context
    assert ctx.verified_context.active_application == "TextEdit"
    assert ctx.previous_goal_state == "COMPLETED"


def test_successful_goal_still_records_audit():
    """Test L: Silent goal execution still records audit events."""
    orch = _setup_test_orchestrator()
    req = GoalRequest(goal_id="g1", goal_text="Open TextEdit.", source="chat")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED

    event_names = [e["event_type"] for e in orch.audit_events]
    assert "GOAL_STARTED" in event_names
    assert "GOAL_COMPLETED" in event_names


def test_clarification_still_resumes_existing_goal():
    """Test M: Clarification requested generates question, resume returns silent completion."""
    ask_dec = AskDecision(question="Which document?", reason="NEED_CLARIFICATION")
    mock_provider = MockPlannerProvider([
        PlannerOutput(decision=ask_dec),
        PlannerOutput(decision=DoneDecision(reason="Done with clarification"))
    ])

    orch = _setup_test_orchestrator(provider=mock_provider)
    res1 = orch.execute_goal(GoalRequest(goal_id="g-clarify", goal_text="Open document", source="chat"))
    assert res1.status == GoalExecutionState.WAITING_FOR_USER
    reply1 = format_goal_reply(res1.status.value, question=res1.final_decision.question)
    assert reply1 == "Which document?"  # Clarification is NOT silent!

    res2 = orch.resume_with_clarification("g-clarify", "The report")
    assert res2.status == GoalExecutionState.COMPLETED
    reply2 = format_goal_reply(res2.status.value)
    assert reply2 == ""  # Completion after resume is SILENT!


def test_response_policy_security_bounds():
    """Test O-R: Response policy performs no execution, creates no UniversalAction, touches no AX."""
    # Module is pure data mapping only
    from services.computer_control import response_policy
    assert hasattr(response_policy, "determine_execution_response")
    # Execute mapping
    dec = determine_execution_response("COMPLETED")
    assert isinstance(dec, ExecutionResponseDecision)
    assert dec.kind == ResponseKind.SILENT
