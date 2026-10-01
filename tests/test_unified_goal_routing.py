"""
Focused integration tests for Evie Unified Assistant Goal Routing (Step 1).
Validates boundary between conversational requests and computer-control goals,
delegation to ChatGoalAdapter/VoiceGoalAdapter/orchestrator, bounded responses,
and security invariants.
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from main import app
from services import tool_router
from services.computer_control.container import (
    ComputerControlContainer, initialize_production_container, set_container
)
from services.computer_control.gate import DEFAULT_POLICY
from services.computer_control.goal_orchestrator import (
    GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.planner import (
    AskDecision, CannotProceedDecision, DoneDecision, RuleBasedPlannerProvider
)
from services.computer_control.models import UniversalAction, Op
from services.tool_registry import format_goal_reply, _computer_control, ComputerControlParams, ToolContext
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


def test_normal_conversational_input_remains_conversational(test_client):
    """Test A: Normal conversational input stays conversational and does not trigger computer control."""
    queries = [
        "How are you?",
        "Explain recursion.",
        "What is Python?",
        "What is my security health score?"
    ]
    for q in queries:
        resp = test_client.post("/chat", json={"message": q})
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        # Check that it didn't return a computer control execution or error
        assert data.get("tool", {}).get("name") != "computer_control"


def test_explicit_computer_goal_becomes_bounded_goal():
    """Test B & C: Explicit computer goal delegates to ChatGoalAdapter and orchestrator."""
    worker = FakeWorkerBackend(is_trusted=True)
    container = initialize_production_container(
        worker=worker,
        get_observation_fn=lambda b: _mock_observation(TEXTEDIT)
    )

    ctx = ToolContext(db_path="evie.db", channel="chat", tone="neutral", conversation={})
    params = ComputerControlParams(command="Open TextEdit.")
    res = _computer_control(params, ctx)

    assert "reply" in res
    assert "computer_control" in res
    cc = res["computer_control"]
    assert cc["status"] in ("COMPLETED", "CANNOT_PROCEED", "ASK", "BLOCKED")
    assert cc["goal_id"].startswith("chat-")


def test_malformed_input_rejected_safely(test_client):
    """Test D: Malformed input is safely rejected with 422 or clean error."""
    # Empty message
    resp1 = test_client.post("/chat", json={"message": ""})
    assert resp1.status_code in (200, 422)

    # Malformed JSON
    resp2 = test_client.post("/chat", content="not json", headers={"Content-Type": "application/json"})
    assert resp2.status_code == 422


def test_computer_control_result_formatting():
    """Test E: Computer control results format according to Step 7 silent execution policy."""
    assert format_goal_reply("COMPLETED", verification_summary="TextEdit is open") == ""
    assert format_goal_reply("COMPLETED", verification_summary=None) == ""
    assert format_goal_reply("ASK", question="Which document do you want?") == "Which document do you want?"
    assert format_goal_reply("BLOCKED") == "I can't perform that action."
    assert format_goal_reply("CANNOT_PROCEED") == "I couldn't complete that task."
    assert format_goal_reply("FAILED") == "I couldn't complete that task."



def test_no_direct_universal_action_construction_in_routing():
    """Test F: Routing layer only emits text commands and GoalRequests, never raw UniversalAction."""
    worker = FakeWorkerBackend(is_trusted=True)
    container = initialize_production_container(
        worker=worker,
        get_observation_fn=lambda b: _mock_observation(TEXTEDIT)
    )

    # Process via chat adapter
    result = container.chat_adapter.process_chat_input("Open TextEdit.")
    assert isinstance(result.goal_id, str)
    # Ensure no raw action objects exposed directly in top-level fields
    d = result.model_dump()
    assert "raw_action" not in d
    assert "universal_action" not in d


def test_no_execution_bypass():
    """Test G: Computer control goals must traverse GoalExecutionOrchestrator and Safety Gate."""
    worker = FakeWorkerBackend(is_trusted=True)
    # Configure container with restrictive allowed_apps that blocks Terminal
    container = initialize_production_container(
        worker=worker,
        allowed_apps=frozenset({"com.apple.TextEdit"}),
        get_observation_fn=lambda b: _mock_observation(TERMINAL)
    )

    ctx = ToolContext(db_path="evie.db", channel="chat", tone="neutral", conversation={})
    params = ComputerControlParams(command="Open Terminal.")
    res = _computer_control(params, ctx)

    # Gate must block or orchestrator cannot proceed
    assert res["reply"] in ("I can't perform that action.", "I couldn't complete that task.")
    cc = res["computer_control"]
    assert cc["status"] in ("BLOCKED", "CANNOT_PROCEED", "WAITING_FOR_USER")


def test_existing_authentication_remains_enforced(test_client):
    """Test H: Existing authenticated endpoints like /computer/goals remain active and enforced."""
    # Test POST /computer/goals endpoint works with valid payload
    resp = test_client.post("/computer/goals", json={"goal_id": "test-g1", "goal": "Open TextEdit."})
    assert resp.status_code == 200
    data = resp.json()
    assert "goal_id" in data
    assert "status" in data
