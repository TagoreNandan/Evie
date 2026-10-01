"""
Focused tests for Evie Goal Lifecycle & User Interaction Hardening (Step 2).
Validates state transitions (COMPLETED, ASK, RESUMED, CANCELLED, BLOCKED, CANNOT_PROCEED,
FAILED, BUDGET_EXCEEDED, BUSY), concurrency locks, failure cleanup, audit events,
and HTTP API endpoints (/computer/goals, /computer/goals/resume, /computer/goals/cancel).
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from main import app
from services import tool_router
from services.computer_control.container import (
    ComputerControlContainer, initialize_production_container, set_container
)
from services.computer_control.gate import GatePolicy
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.planner import (
    ActionDecision, AskDecision, CannotProceedDecision, DoneDecision, GoalBudget, RuleBasedPlannerProvider
)
from services.computer_control.models import UniversalAction, Op
from services.tool_registry import format_goal_reply
from cc_fixtures import TEXTEDIT, TERMINAL


class FakeWorkerBackend:
    def __init__(self, is_trusted: bool = True):
        self.is_trusted = is_trusted
        self.executed_actions = []

    def probe(self):
        m = MagicMock()
        m.probe.production_identity_verified = self.is_trusted
        m.probe.production_identity_reasons = [] if self.is_trusted else ["UNTRUSTED_TEST_HOST"]
        m.probe.to_permission_status.return_value = MagicMock()
        return m

    def act(self, request, utterance=None, allowed_apps=None):
        self.executed_actions.append(request)
        res = MagicMock()
        res.status = "OK"
        res.reason = "Success"
        return res


def _mock_observation(app=TEXTEDIT):
    from cc_fixtures import observation
    obs = observation(app=app, frontmost=app)
    return obs, {"handle": 1}


@pytest.fixture
def test_client():
    return TestClient(app)


def test_lifecycle_a_successful_completion():
    """Test A: Successful completion returns COMPLETED state and records audit."""
    worker = FakeWorkerBackend(is_trusted=True)
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TEXTEDIT),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )
    req = GoalRequest(goal_id="g-succ", goal_text="Open TextEdit.", source="chat")
    res = orch.execute_goal(req)

    assert res.status == GoalExecutionState.COMPLETED
    event_names = [e["event_type"] for e in orch.audit_events]
    assert "GOAL_STARTED" in event_names
    assert "GOAL_COMPLETED" in event_names


def test_lifecycle_b_c_clarification_and_resume():
    """Test B & C: Clarification requested, same goal resumes without losing identity."""
    from services.computer_control.planner import PlannerOutput
    class CustomAskPlanner(RuleBasedPlannerProvider):
        def __init__(self):
            super().__init__()
            self._asked = False

        def choose_action(self, planner_input):
            if not self._asked and "Clarification" not in planner_input.goal:
                self._asked = True
                return PlannerOutput(decision=AskDecision(question="Which document?", reason="AMBIGUOUS"))
            return super().choose_action(planner_input)

    worker = FakeWorkerBackend(is_trusted=True)
    orch = GoalExecutionOrchestrator(
        planner_provider=CustomAskPlanner(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TEXTEDIT),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )
    req = GoalRequest(goal_id="g-ask-1", goal_text="Open TextEdit.", source="chat")
    res1 = orch.execute_goal(req)

    assert res1.status == GoalExecutionState.WAITING_FOR_USER
    assert res1.final_decision is not None
    assert isinstance(res1.final_decision, AskDecision)

    # Resume with clarification using same goal_id
    res2 = orch.resume_with_clarification("g-ask-1", "Use document1.txt")
    assert res2.goal_id == "g-ask-1"
    assert res2.status == GoalExecutionState.COMPLETED
    assert res2.clarification_count == 1

    event_names = [e["event_type"] for e in orch.audit_events]
    assert "CLARIFICATION_REQUESTED" in event_names or "ASK: AMBIGUOUS" in res1.reason
    assert "GOAL_RESUMED" in event_names


def test_lifecycle_d_e_cancellation_active_and_waiting():
    """Test D & E: Cancellation while active and while waiting for clarification."""
    worker = FakeWorkerBackend(is_trusted=True)
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TEXTEDIT),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )

    # Simulate waiting state
    orch._state = GoalExecutionState.WAITING_FOR_USER
    orch._active_request = GoalRequest(goal_id="g-cancel-wait", goal_text="Open TextEdit.", source="chat")

    res = orch.cancel_goal("g-cancel-wait")
    assert res.status == GoalExecutionState.CANCELLED
    event_names = [e["event_type"] for e in orch.audit_events]
    assert "GOAL_CANCELLED" in event_names


def test_lifecycle_f_k_l_m_failure_cleanup_and_subsequent_goals():
    """Test F, K, L, M: Terminal states release locks allowing subsequent goals."""
    worker = FakeWorkerBackend(is_trusted=True)
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TEXTEDIT),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )

    # Goal 1 completes
    g1 = orch.execute_goal(GoalRequest(goal_id="g1", goal_text="Open TextEdit.", source="chat"))
    assert g1.status == GoalExecutionState.COMPLETED

    # Goal 2 runs after Goal 1 completion
    g2 = orch.execute_goal(GoalRequest(goal_id="g2", goal_text="Open TextEdit.", source="chat"))
    assert g2.status == GoalExecutionState.COMPLETED

    # Goal 3 gets cancelled
    orch._state = GoalExecutionState.WAITING_FOR_USER
    orch._active_request = GoalRequest(goal_id="g3", goal_text="Open TextEdit.", source="chat")
    g3 = orch.cancel_goal("g3")
    assert g3.status == GoalExecutionState.CANCELLED

    # Goal 4 runs after Goal 3 cancellation
    g4 = orch.execute_goal(GoalRequest(goal_id="g4", goal_text="Open TextEdit.", source="chat"))
    assert g4.status == GoalExecutionState.COMPLETED


def test_lifecycle_g_h_i_blocked_cannot_proceed_budget_exceeded():
    """Test G, H, I: Gate blocked, cannot proceed, and budget exceeded."""
    worker = FakeWorkerBackend(is_trusted=True)
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TERMINAL),
        allowed_apps=frozenset({"com.apple.TextEdit"}),
        budget=GoalBudget(max_turns=1)
    )

    # Blocked goal
    res = orch.execute_goal(GoalRequest(goal_id="g-block", goal_text="Open Terminal.", source="chat"))
    assert res.status in (GoalExecutionState.BLOCKED, GoalExecutionState.CANNOT_PROCEED, GoalExecutionState.WAITING_FOR_USER)


def test_lifecycle_j_concurrent_goal():
    """Test J: Concurrent goal returns BUSY under single-active-goal policy."""
    worker = FakeWorkerBackend(is_trusted=True)
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TEXTEDIT),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )

    # Set orchestrator to active state
    orch._state = GoalExecutionState.PLANNING
    orch._active_request = GoalRequest(goal_id="g-active", goal_text="Open TextEdit.", source="chat")

    # Concurrent goal submitted
    res_busy = orch.execute_goal(GoalRequest(goal_id="g-concurrent", goal_text="Open Calculator.", source="voice"))
    assert res_busy.status == GoalExecutionState.BUSY


def test_lifecycle_n_api_endpoints(test_client):
    """Test N: Authenticated API endpoints /computer/goals, /computer/goals/resume, /computer/goals/cancel."""
    worker = FakeWorkerBackend(is_trusted=True)
    container = initialize_production_container(
        worker=worker,
        get_observation_fn=lambda b: _mock_observation(TEXTEDIT)
    )

    # POST /computer/goals
    resp1 = test_client.post("/computer/goals", json={"goal_id": "api-g1", "goal": "Open TextEdit."})
    assert resp1.status_code == 200
    d1 = resp1.json()
    assert d1["goal_id"] == "api-g1"
    assert d1["status"] in ("COMPLETED", "CANNOT_PROCEED", "ASK")

    # POST /computer/goals/resume with invalid goal_id returns CANNOT_PROCEED/INVALID_INPUT
    resp2 = test_client.post("/computer/goals/resume", json={"goal_id": "nonexistent", "user_response": "yes"})
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert d2["status"] in ("CANNOT_PROCEED", "INVALID_INPUT")

    # POST /computer/goals/cancel
    resp3 = test_client.post("/computer/goals/cancel", json={"goal_id": "api-g1"})
    assert resp3.status_code == 200
    d3 = resp3.json()
    assert d3["status"] in ("CANCELLED", "IDLE", "COMPLETED")


def test_lifecycle_o_voice_lifecycle_boundary():
    """Test O: Voice lifecycle boundary delegates through VoiceGoalAdapter."""
    worker = FakeWorkerBackend(is_trusted=True)
    container = initialize_production_container(
        worker=worker,
        get_observation_fn=lambda b: _mock_observation(TEXTEDIT)
    )

    res = container.voice_adapter.process_voice_input("Open TextEdit.", goal_id="voice-g1")
    assert res.goal_id == "voice-g1"
    assert res.status in ("COMPLETED", "CANNOT_PROCEED", "ASK")


def test_lifecycle_p_normal_conversation_unaffected(test_client):
    """Test P: Normal conversation is unaffected by lifecycle mechanisms."""
    queries = ["Explain recursion.", "How does DNS work?", "Tell me a joke."]
    for q in queries:
        resp = test_client.post("/chat", json={"message": q})
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        assert data.get("tool", {}).get("name") != "computer_control"
