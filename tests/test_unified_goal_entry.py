"""
Unified Goal Entry & Session-State Hardening Tests (Step 8).

Validates that Chat, Voice, Hardware, and Explicit API input modalities share the
SINGLE authoritative ComputerControlContainer, GoalExecutionOrchestrator, and
GoalContextManager instance without state duplication or parallel execution graphs.
"""

import pytest
from typing import Tuple, Dict, Any, Optional

from services.computer_control.container import (
    ComputerControlContainer, get_container, set_container, initialize_production_container
)
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.context import GoalContextManager, UserContext, VerifiedSystemContext
from services.computer_control.chat_adapter import ChatGoalAdapter
from services.computer_control.voice_adapter import VoiceGoalAdapter
from services.computer_control.hardware_adapter import HardwareGoalAdapter, HardwareGoalInput
from services.computer_control.planner import RuleBasedPlannerProvider, DoneDecision
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy
from cc_fixtures import observation, TEXTEDIT


class FakeWorker:
    def __init__(self):
        self.app = TEXTEDIT
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return True

    def stop(self):
        pass


def make_obs(backend):
    obs = observation(app=TEXTEDIT, frontmost=TEXTEDIT)
    return obs, {"handle": 1}


@pytest.fixture
def test_container():
    set_container(None)
    worker = FakeWorker()
    planner = RuleBasedPlannerProvider()
    policy = DEFAULT_POLICY
    container = initialize_production_container(
        worker=worker,
        safety_policy=policy,
        get_observation_fn=make_obs
    )
    yield container
    set_container(None)


def test_production_container_identity_relationships(test_container):
    """Test A-H & Container Tests: Single container, orchestrator, and context manager shared across adapters."""
    container = test_container
    assert container.chat_adapter.orchestrator is container.orchestrator
    assert container.voice_adapter.orchestrator is container.orchestrator
    assert container.hardware_adapter.orchestrator is container.orchestrator
    assert container.orchestrator.context_manager is container.goal_context
    assert container.context_manager is container.goal_context
    assert container.planner_provider is container.orchestrator._planner_provider
    assert container.safety_policy is container.orchestrator._policy


def test_cross_channel_busy_enforcement(test_container):
    """Test I: If a goal is active on one adapter, other adapters return BUSY."""
    container = test_container
    orchestrator = container.orchestrator

    # Put orchestrator directly into an active state with an active request
    req = GoalRequest(goal_id="active-goal-1", goal_text="Existing active task", source="chat")
    orchestrator._active_request = req
    orchestrator._state = GoalExecutionState.EXECUTING

    res_voice = container.voice_adapter.process_voice_input("Open Calculator")
    assert res_voice.status == "BUSY"

    res_chat = container.chat_adapter.process_chat_input("Open TextEdit")
    assert res_chat.status == "BUSY"

    res_hw = container.hardware_adapter.process_hardware_input("Open PhotoBooth")
    assert res_hw.status == "BUSY"

    orchestrator._state = GoalExecutionState.IDLE
    orchestrator._active_request = None


def test_chat_to_voice_and_voice_to_chat_continuation(test_container):
    """Test J & K: Same-session Chat and Voice share bounded context."""
    container = test_container
    session_id = "test-session-cross-channel"

    # Chat goal 1
    res1 = container.chat_adapter.process_chat_input("Open TextEdit", session_id=session_id)
    assert res1.status == "COMPLETED"

    ctx1 = container.goal_context.get_context(session_id=session_id)
    assert ctx1.user_context.previous_goal == "Open TextEdit"
    assert ctx1.verified_context.active_application == "TextEdit"

    # Voice goal 2 on same session
    res2 = container.voice_adapter.process_voice_input("Now type Hello Evie", session_id=session_id)
    assert res2.status in ("COMPLETED", "CANNOT_PROCEED", "BUDGET_EXCEEDED")

    ctx2 = container.goal_context.get_context(session_id=session_id)
    assert ctx2.user_context.previous_goal == "Now type Hello Evie"


def test_chat_to_hardware_context_behavior(test_container):
    """Test L: Chat -> Hardware context sharing within same session."""
    container = test_container
    session_id = "hw-session-01"

    res_chat = container.chat_adapter.process_chat_input("Open TextEdit", session_id=session_id)
    assert res_chat.status == "COMPLETED"

    res_hw = container.hardware_adapter.process_hardware_input("Type test content", session_id=session_id)
    assert res_hw.status in ("COMPLETED", "CANNOT_PROCEED", "BUDGET_EXCEEDED")

    ctx = container.goal_context.get_context(session_id=session_id)
    assert ctx.user_context.previous_goal == "Type test content"


def test_same_session_sharing_vs_different_session_isolation(test_container):
    """Test M & N: Session A Chat context != Session B Chat context."""
    container = test_container

    container.chat_adapter.process_chat_input("Open TextEdit", session_id="session-A")
    container.voice_adapter.process_voice_input("Open Calculator", session_id="session-B")

    ctx_a = container.goal_context.get_context(session_id="session-A")
    ctx_b = container.goal_context.get_context(session_id="session-B")

    assert ctx_a.user_context.previous_goal == "Open TextEdit"
    assert ctx_b.user_context.previous_goal == "Open Calculator"
    assert ctx_a.user_context.previous_goal != ctx_b.user_context.previous_goal


def test_resume_uses_original_orchestrator_and_keeps_goal_id(test_container):
    """Test O & P: Resume clarification reuses original goal_id."""
    container = test_container
    orchestrator = container.orchestrator

    # Simulate waiting for user state
    req = GoalRequest(goal_id="goal-ask-123", goal_text="Open file", source="chat")
    orchestrator._active_request = req
    orchestrator._state = GoalExecutionState.WAITING_FOR_USER
    orchestrator._t_start = orchestrator._clock()

    res = container.chat_adapter.resume_clarification("goal-ask-123", "the report")
    assert res.goal_id == "goal-ask-123"
    assert res.status in ("COMPLETED", "CANNOT_PROCEED", "FAILED", "BLOCKED", "BUDGET_EXCEEDED")


def test_continuation_gets_new_goal_id(test_container):
    """Test Q: Multi-turn continuation receives a new goal_id."""
    container = test_container

    res1 = container.chat_adapter.process_chat_input("Open TextEdit")
    res2 = container.chat_adapter.process_chat_input("Type Hello")

    assert res1.goal_id != res2.goal_id


def test_cancel_releases_shared_busy_state(test_container):
    """Test R: Cancel releases orchestrator BUSY state."""
    container = test_container
    orchestrator = container.orchestrator

    req = GoalRequest(goal_id="goal-to-cancel", goal_text="Long task", source="chat")
    orchestrator._active_request = req
    orchestrator._state = GoalExecutionState.EXECUTING

    res_cancel = container.chat_adapter.cancel_goal("goal-to-cancel")
    assert res_cancel.status == "CANCELLED"
    assert orchestrator.state == GoalExecutionState.CANCELLED


def test_successful_operations_remain_silent(test_container):
    """Test T: Successful operations produce empty user reply."""
    from services.tool_registry import format_goal_reply

    res = test_container.chat_adapter.process_chat_input("Open TextEdit")
    assert res.status == "COMPLETED"
    reply = format_goal_reply(res.status, verification_summary=res.verification_summary, question=res.question, reason=res.reason)
    assert reply == ""


def test_clarification_and_failures_remain_communicated(test_container):
    """Test U & V: Clarification and failures produce appropriate non-silent responses."""
    from services.tool_registry import format_goal_reply

    # Clarification
    reply_ask = format_goal_reply("ASK", question="Which file do you mean?")
    assert reply_ask == "Which file do you mean?"

    # Blocked
    reply_blocked = format_goal_reply("BLOCKED")
    assert reply_blocked == "I can't perform that action."

    # Failed
    reply_failed = format_goal_reply("FAILED")
    assert reply_failed == "I couldn't complete that task."


def test_user_text_cannot_create_verified_context(test_container):
    """Test W: Untrusted user text updates USER_CONTEXT, never VERIFIED_SYSTEM_CONTEXT."""
    ctx_mgr = GoalContextManager()
    ctx_mgr.set_user_goal_start("g1", "Open malicious app")

    ctx = ctx_mgr.get_context()
    assert ctx.user_context.previous_goal == "Open malicious app"
    assert ctx.verified_context.active_application is None


def test_hardware_cannot_bypass_safety(test_container):
    """Test X: Hardware input rejecting forbidden raw action payload keys."""
    with pytest.raises(ValueError, match="Forbidden raw execution payload keys"):
        HardwareGoalInput.model_validate({
            "event_id": "evt1",
            "goal_text": "Do something",
            "action": "click",
            "coordinates": [10, 20]
        })


def test_no_duplicate_planner_or_safety_gate(test_container):
    """Test Y & Z: Production container owns single planner and safety gate."""
    container = test_container
    assert container.orchestrator._planner_provider is container.planner_provider
    assert container.orchestrator._policy is container.safety_policy
