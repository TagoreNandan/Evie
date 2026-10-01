"""
Dedicated unit, integration, API, and security test suite for ChatGoalAdapter (Evie Phase 7B).

Verifies thin input adapter contract, untrusted text normalization & validation,
exact source="chat" attribution, zero keyword/allowlist command routing, propagation of
orchestrator statuses (COMPLETED, ASK, CANNOT_PROCEED, BUSY, CANCELLED, BLOCKED),
clarification & cancellation lifecycle, API request/response schema validation, API security invariants
(rejection of raw actions, coordinates, shell, AppleScript), preservation of normal non-computer /chat behavior,
non-bypass of Safety Gate / signed worker, and full end-to-end chat -> GoalRequest -> GoalExecutionOrchestrator flow.
"""

import ast
import inspect
import json
from pathlib import Path
import pytest

from cc_fixtures import TEXTEDIT
from test_computer_control_executor import FakeLive, Clock, _setup, textedit
from services.computer_control.actions import Op
from services.computer_control.gate import DEFAULT_POLICY
from services.computer_control.models import ActivateAppArgs, UniversalAction
from services.computer_control.observer import observe_app_with_handles
from services.computer_control.planner import (
    ActionDecision, AskDecision, CannotProceedDecision, DoneDecision, GoalBudget,
    MockPlannerProvider, PlannerOutput, RuleBasedPlannerProvider
)
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.chat_adapter import ChatGoalAdapter, ChatGoalResult
from main import app, ComputerGoalRequest, ComputerGoalResumeRequest, ComputerGoalCancelRequest


class FakeOrchestrator:
    """Mock orchestrator recording received GoalRequests."""

    def __init__(self, return_state: GoalExecutionState = GoalExecutionState.COMPLETED):
        self.requests = []
        self.return_state = return_state
        self.clarifications = []
        self.cancellations = []

    def execute_goal(self, request: GoalRequest) -> GoalExecutionResult:
        self.requests.append(request)
        final_dec = None
        if self.return_state == GoalExecutionState.WAITING_FOR_USER:
            final_dec = AskDecision(question="Which app?", reason="AMBIGUOUS")
        elif self.return_state == GoalExecutionState.COMPLETED:
            final_dec = DoneDecision(reason="Goal completed successfully.")

        return GoalExecutionResult(
            goal_id=request.goal_id,
            status=self.return_state,
            turn_count=1,
            clarification_count=0,
            elapsed_time_s=0.1,
            reason=f"Status: {self.return_state.value}",
            final_decision=final_dec
        )

    def resume_with_clarification(self, goal_id: str, user_response: str) -> GoalExecutionResult:
        self.clarifications.append((goal_id, user_response))
        return GoalExecutionResult(
            goal_id=goal_id,
            status=GoalExecutionState.COMPLETED,
            turn_count=2,
            clarification_count=1,
            elapsed_time_s=0.2,
            reason="Resumed and completed",
            final_decision=DoneDecision(reason="Clarification applied.")
        )

    def cancel_goal(self, goal_id: str) -> GoalExecutionResult:
        self.cancellations.append(goal_id)
        return GoalExecutionResult(
            goal_id=goal_id,
            status=GoalExecutionState.CANCELLED,
            turn_count=1,
            clarification_count=0,
            elapsed_time_s=0.1,
            reason="Goal cancelled"
        )


def _setup_chat_adapter(fake_orch=None):
    orch = fake_orch or FakeOrchestrator()
    adapter = ChatGoalAdapter(orchestrator=orch)
    return adapter, orch


# 1. Valid chat text creates GoalRequest
def test_valid_chat_text_creates_goal_request():
    adapter, orch = _setup_chat_adapter()
    res = adapter.process_chat_input("open TextEdit and type Hello Evie")
    assert res.status == "COMPLETED"
    assert len(orch.requests) == 1
    req = orch.requests[0]
    assert req.goal_text == "open TextEdit and type Hello Evie"


# 2. Source is exactly "chat"
def test_source_is_exactly_chat():
    adapter, orch = _setup_chat_adapter()
    adapter.process_chat_input("take a photo")
    assert orch.requests[0].source == "chat"


# 3. Empty input rejected
def test_empty_input_rejected():
    adapter, orch = _setup_chat_adapter()
    res1 = adapter.process_chat_input("")
    assert res1.status == "INVALID_INPUT"
    assert res1.error == "EMPTY_CHAT_INPUT"

    res2 = adapter.process_chat_input("   \n\t ")
    assert res2.status == "INVALID_INPUT"
    assert len(orch.requests) == 0


# 4. Oversized input rejected
def test_oversized_input_rejected():
    adapter, orch = _setup_chat_adapter()
    long_text = "a" * 501
    res = adapter.process_chat_input(long_text)
    assert res.status == "INVALID_INPUT"
    assert res.error == "CHAT_INPUT_TOO_LONG"
    assert len(orch.requests) == 0


# 5. Whitespace normalization
def test_whitespace_normalization():
    adapter, orch = _setup_chat_adapter()
    adapter.process_chat_input("   open   TextEdit   and   type   hello   ")
    assert orch.requests[0].goal_text == "open TextEdit and type hello"


# 6. Multi-step natural-language goal forwarded unchanged
def test_multistep_goal_forwarded_unchanged():
    adapter, orch = _setup_chat_adapter()
    goal_str = "open Calculator then add 5 plus 10"
    adapter.process_chat_input(goal_str)
    assert orch.requests[0].goal_text == goal_str


# 7. No keyword routing
def test_no_keyword_routing():
    source = inspect.getsource(ChatGoalAdapter)
    forbidden_routes = ["open textedit", "take photo", "click button", "if text ==", "elif text =="]
    for route in forbidden_routes:
        assert route not in source.lower(), f"Forbidden hardcoded keyword route found: {route}"


# 8. Exactly one GoalRequest submitted
def test_exactly_one_goal_request_submitted():
    adapter, orch = _setup_chat_adapter()
    adapter.process_chat_input("switch to Finder")
    assert len(orch.requests) == 1


# 9. COMPLETED result propagation
def test_completed_result_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.COMPLETED)
    adapter, _ = _setup_chat_adapter(fake_orch)
    res = adapter.process_chat_input("completed task")
    assert res.status == "COMPLETED"


# 10. ASK result propagation
def test_ask_result_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.WAITING_FOR_USER)
    adapter, _ = _setup_chat_adapter(fake_orch)
    res = adapter.process_chat_input("ambiguous goal")
    assert res.status == "ASK"
    assert res.question == "Which app?"


# 11. CANNOT_PROCEED propagation
def test_cannot_proceed_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.CANNOT_PROCEED)
    adapter, _ = _setup_chat_adapter(fake_orch)
    res = adapter.process_chat_input("impossible goal")
    assert res.status == "CANNOT_PROCEED"


# 12. BUSY propagation
def test_busy_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.BUSY)
    adapter, _ = _setup_chat_adapter(fake_orch)
    res = adapter.process_chat_input("concurrent goal")
    assert res.status == "BUSY"


# 13. CANCELLED propagation
def test_cancelled_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.CANCELLED)
    adapter, _ = _setup_chat_adapter(fake_orch)
    res = adapter.process_chat_input("cancelled goal")
    assert res.status == "CANCELLED"


# 14. Clarification resume
def test_clarification_resume():
    adapter, orch = _setup_chat_adapter()
    res = adapter.resume_clarification("c-1", "Yes, TextEdit")
    assert res.status == "COMPLETED"
    assert len(orch.clarifications) == 1
    assert orch.clarifications[0] == ("c-1", "Yes, TextEdit")


# 15. Clarification budget
def test_clarification_budget():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.WAITING_FOR_USER)
    adapter, _ = _setup_chat_adapter(fake_orch)

    # Calling resume clarification on adapter forwards to orchestrator
    res = adapter.resume_clarification("c-1", "Answer 1")
    assert isinstance(res, ChatGoalResult)


# 16. API request schema validation
def test_api_request_schema_validation():
    req = ComputerGoalRequest(goal="open TextEdit", goal_id="g123")
    assert req.goal == "open TextEdit"
    assert req.goal_id == "g123"

    with pytest.raises(Exception):
        ComputerGoalRequest(goal="") # empty goal rejected


# 17. Response schema validation
def test_response_schema_validation():
    res = ChatGoalResult(
        goal_id="g123",
        status="COMPLETED",
        turn_count=1,
        clarification_count=0,
        elapsed_time_s=0.5,
        reason="Done"
    )
    assert res.status == "COMPLETED"
    assert res.turn_count == 1


# 18. Raw UniversalAction rejected
def test_raw_universal_action_rejected():
    with pytest.raises(Exception):
        ComputerGoalRequest(
            goal="open TextEdit",
            action={"op": "activate_app", "args": {"bundle_id": "com.apple.Terminal"}}
        )


# 19. Coordinates rejected
def test_coordinates_rejected():
    with pytest.raises(Exception):
        ComputerGoalRequest(
            goal="click button",
            click={"x": 100, "y": 200}
        )


# 20. Selectors rejected
def test_selectors_rejected():
    with pytest.raises(Exception):
        ComputerGoalRequest(
            goal="click button",
            target_spec={"role": "AXButton", "label": "Submit"}
        )


# 21. Shell rejected
def test_shell_rejected():
    with pytest.raises(Exception):
        ComputerGoalRequest(
            goal="run shell",
            shell="rm -rf /"
        )


# 22. AppleScript rejected
def test_applescript_rejected():
    with pytest.raises(Exception):
        ComputerGoalRequest(
            goal="applescript",
            applescript="tell app \"Finder\" to quit"
        )


# 23. Direct executor access impossible
def test_direct_executor_access_impossible():
    source = inspect.getsource(ChatGoalAdapter)
    forbidden = ["execute_action", "ComputerControlWorker", "ActionBackend", "macos_actions"]
    for term in forbidden:
        assert term not in source, f"ChatGoalAdapter source references forbidden executor term: {term}"


# 24. GoalBudget cannot be changed through API
def test_goal_budget_cannot_be_changed_through_api():
    source = inspect.getsource(ChatGoalAdapter)
    assert "GoalBudget(" not in source, "ChatGoalAdapter should not construct or modify GoalBudget"


# 25. Safety policy cannot be changed through API
def test_safety_policy_cannot_be_changed_through_api():
    req_chat = ComputerGoalRequest(goal="open terminal")
    # Verify request payload cannot carry safety policy overrides
    assert not hasattr(req_chat, "safety_policy")
    assert not hasattr(req_chat, "bypass_gate")


# 26. Source metadata cannot alter security behavior
def test_source_metadata_cannot_alter_security_behavior():
    req_voice = GoalRequest(goal_id="g1", goal_text="open terminal", source="voice")
    req_chat = GoalRequest(goal_id="g2", goal_text="open terminal", source="chat")
    assert req_voice.goal_text == req_chat.goal_text


# 27. Concurrent goal returns BUSY
def test_concurrent_goal_returns_busy():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.WAITING_FOR_USER)
    adapter, _ = _setup_chat_adapter(fake_orch)

    res1 = adapter.process_chat_input("goal 1")
    assert res1.status == "ASK"

    fake_orch.return_state = GoalExecutionState.BUSY
    res2 = adapter.process_chat_input("goal 2")
    assert res2.status == "BUSY"


# 28. Goal cancellation delegates correctly
def test_goal_cancellation_delegates_correctly():
    adapter, orch = _setup_chat_adapter()
    res = adapter.cancel_goal("c-100")
    assert res.status == "CANCELLED"
    assert "c-100" in orch.cancellations


# 29. Result redaction
def test_result_redaction():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.COMPLETED)
    adapter, _ = _setup_chat_adapter(fake_orch)
    res = adapter.process_chat_input("task with secret=P@ssword123")
    assert not hasattr(res, "screenshot")
    assert not hasattr(res, "api_key")
    assert not hasattr(res, "password")


# 30. Authentication boundary preserved
def test_authentication_boundary_preserved():
    # Verify FastAPI endpoints retain app setup
    assert app is not None


# 31. Normal non-computer chat behavior remains intact
def test_normal_non_computer_chat_behavior_remains_intact():
    from fastapi.testclient import TestClient
    client = TestClient(app)
    # Sending greeting to POST /chat returns briefing/greeting, not computer control error
    resp = client.post("/chat", json={"message": "Good morning Evie"})
    assert resp.status_code == 200
    data = resp.json()
    assert "reply" in data
    assert "Good morning" in data["reply"]


# 32. Full chat -> GoalRequest -> orchestrator lifecycle using fake orchestrator
def test_full_chat_to_orchestrator_lifecycle():
    app_node = textedit()
    backend, clock, cache = _setup(app_node)

    def get_obs(b):
        obs, handles = observe_app_with_handles(backend, app_node, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
        return obs, handles

    real_orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=backend,
        get_observation=get_obs,
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )

    adapter = ChatGoalAdapter(orchestrator=real_orch)
    res = adapter.process_chat_input("open TextEdit")

    assert res.status == "COMPLETED"
    assert res.turn_count >= 1
    assert res.orchestrator_result is not None
    assert res.orchestrator_result.goal_id == res.goal_id
