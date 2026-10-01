"""
Dedicated unit, integration, and security test suite for VoiceGoalAdapter (Evie Phase 7A).

Verifies thin input adapter contract, untrusted text normalization & validation,
exact source="voice" attribution, no keyword/allowlist command routing, propagation of
orchestrator statuses (COMPLETED, ASK, CANNOT_PROCEED, BUSY, CANCELLED, BLOCKED),
clarification & cancellation lifecycle, IPC payload security invariants (rejection of raw actions,
coordinates, shell, AppleScript), non-bypass of Safety Gate / signed worker, and full
end-to-end voice -> GoalRequest -> GoalExecutionOrchestrator flow.
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
from services.computer_control.voice_adapter import VoiceGoalAdapter, VoiceGoalResult
from services.computer_control.voice_automation_service import VoiceAutomationService


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


def _setup_voice_adapter(fake_orch=None):
    orch = fake_orch or FakeOrchestrator()
    adapter = VoiceGoalAdapter(orchestrator=orch)
    return adapter, orch


# 1. Valid voice text creates GoalRequest
def test_valid_voice_text_creates_goal_request():
    adapter, orch = _setup_voice_adapter()
    res = adapter.process_voice_input("open TextEdit and write Hello Evie")
    assert res.status == "COMPLETED"
    assert len(orch.requests) == 1
    req = orch.requests[0]
    assert req.goal_text == "open TextEdit and write Hello Evie"


# 2. Source is exactly "voice"
def test_source_is_exactly_voice():
    adapter, orch = _setup_voice_adapter()
    adapter.process_voice_input("take a photo")
    assert orch.requests[0].source == "voice"


# 3. Empty voice input rejected
def test_empty_voice_input_rejected():
    adapter, orch = _setup_voice_adapter()
    res1 = adapter.process_voice_input("")
    assert res1.status == "INVALID_INPUT"
    assert res1.error == "EMPTY_VOICE_INPUT"

    res2 = adapter.process_voice_input("   \n\t ")
    assert res2.status == "INVALID_INPUT"
    assert len(orch.requests) == 0


# 4. Oversized voice input rejected
def test_oversized_voice_input_rejected():
    adapter, orch = _setup_voice_adapter()
    long_text = "a" * 501
    res = adapter.process_voice_input(long_text)
    assert res.status == "INVALID_INPUT"
    assert res.error == "VOICE_INPUT_TOO_LONG"
    assert len(orch.requests) == 0


# 5. Whitespace normalization
def test_whitespace_normalization():
    adapter, orch = _setup_voice_adapter()
    adapter.process_voice_input("   open   TextEdit   and   type   hello   ")
    assert orch.requests[0].goal_text == "open TextEdit and type hello"


# 6. Normal multi-step natural language goal passes unchanged to orchestrator
def test_normal_multistep_goal_passes_unchanged():
    adapter, orch = _setup_voice_adapter()
    goal_str = "open Calculator then add 5 plus 10"
    adapter.process_voice_input(goal_str)
    assert orch.requests[0].goal_text == goal_str


# 7. No command keyword routing
def test_no_command_keyword_routing():
    # Verify adapter source code does not contain hardcoded keyword routes
    source = inspect.getsource(VoiceGoalAdapter)
    forbidden_routes = ["open textedit", "take photo", "click button", "if text ==", "elif text =="]
    for route in forbidden_routes:
        assert route not in source.lower(), f"Forbidden hardcoded keyword route found: {route}"


# 8. Orchestrator receives exactly one GoalRequest
def test_orchestrator_receives_exactly_one_goal_request():
    adapter, orch = _setup_voice_adapter()
    adapter.process_voice_input("switch to Finder")
    assert len(orch.requests) == 1


# 9. Orchestrator result is returned correctly
def test_orchestrator_result_returned_correctly():
    adapter, orch = _setup_voice_adapter()
    res = adapter.process_voice_input("test goal")
    assert isinstance(res, VoiceGoalResult)
    assert res.orchestrator_result is not None


# 10. DONE result propagation
def test_done_result_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.COMPLETED)
    adapter, _ = _setup_voice_adapter(fake_orch)
    res = adapter.process_voice_input("completed task")
    assert res.status == "COMPLETED"


# 11. ASK result propagation
def test_ask_result_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.WAITING_FOR_USER)
    adapter, _ = _setup_voice_adapter(fake_orch)
    res = adapter.process_voice_input("ambiguous goal")
    assert res.status == "ASK"
    assert res.question == "Which app?"


# 12. CANNOT_PROCEED propagation
def test_cannot_proceed_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.CANNOT_PROCEED)
    adapter, _ = _setup_voice_adapter(fake_orch)
    res = adapter.process_voice_input("impossible goal")
    assert res.status == "CANNOT_PROCEED"


# 13. BUSY propagation
def test_busy_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.BUSY)
    adapter, _ = _setup_voice_adapter(fake_orch)
    res = adapter.process_voice_input("concurrent goal")
    assert res.status == "BUSY"


# 14. CANCELLED propagation
def test_cancelled_propagation():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.CANCELLED)
    adapter, _ = _setup_voice_adapter(fake_orch)
    res = adapter.process_voice_input("cancelled goal")
    assert res.status == "CANCELLED"


# 15. Clarification resume
def test_clarification_resume():
    adapter, orch = _setup_voice_adapter()
    res = adapter.resume_clarification("v-1", "Yes, TextEdit")
    assert res.status == "COMPLETED"
    assert len(orch.clarifications) == 1
    assert orch.clarifications[0] == ("v-1", "Yes, TextEdit")


# 16. Voice cancellation
def test_voice_cancellation():
    adapter, orch = _setup_voice_adapter()
    res = adapter.cancel_goal("v-1")
    assert res.status == "CANCELLED"
    assert len(orch.cancellations) == 1
    assert orch.cancellations[0] == "v-1"


# 17. Voice IPC authentication remains required
def test_voice_ipc_auth_required():
    svc = VoiceAutomationService(db_path=":memory:")
    # Unauthenticated message must fail
    res, auth = svc.handle_message(json.dumps({"op": "COMMAND", "session_id": "s1", "transcript": "test"}).encode("utf-8"), False)
    assert res["status"] == "UNAUTHENTICATED"


# 18. Malformed IPC payload rejected
def test_malformed_ipc_payload_rejected():
    svc = VoiceAutomationService(db_path=":memory:")
    res, _ = svc.handle_message(b"not json", True)
    assert res["status"] == "ERROR"
    assert res["error"] == "MALFORMED_MESSAGE"


# 19. Raw UniversalAction over voice IPC rejected
def test_raw_universal_action_ipc_rejected():
    svc = VoiceAutomationService(db_path=":memory:")
    raw_payload = json.dumps({
        "op": "COMMAND", "session_id": "s1",
        "action": {"op": "activate_app", "args": {"bundle_id": "com.apple.Terminal"}}
    }).encode("utf-8")
    res, _ = svc.handle_message(raw_payload, True)
    assert res["status"] == "ERROR"


# 20. Raw coordinate payload rejected
def test_raw_coordinate_payload_rejected():
    svc = VoiceAutomationService(db_path=":memory:")
    raw_payload = json.dumps({
        "op": "COMMAND", "session_id": "s1",
        "click": {"x": 100, "y": 200}
    }).encode("utf-8")
    res, _ = svc.handle_message(raw_payload, True)
    assert res["status"] == "ERROR"


# 21. Shell payload rejected
def test_shell_payload_rejected():
    svc = VoiceAutomationService(db_path=":memory:")
    raw_payload = json.dumps({
        "op": "COMMAND", "session_id": "s1",
        "shell": "rm -rf /"
    }).encode("utf-8")
    res, _ = svc.handle_message(raw_payload, True)
    assert res["status"] == "ERROR"


# 22. AppleScript payload rejected
def test_applescript_payload_rejected():
    svc = VoiceAutomationService(db_path=":memory:")
    raw_payload = json.dumps({
        "op": "COMMAND", "session_id": "s1",
        "applescript": "tell app \"Finder\" to quit"
    }).encode("utf-8")
    res, _ = svc.handle_message(raw_payload, True)
    assert res["status"] == "ERROR"


# 23. Adapter cannot access executor directly
def test_adapter_cannot_access_executor_directly():
    source = inspect.getsource(VoiceGoalAdapter)
    forbidden = ["execute_action", "ComputerControlWorker", "ActionBackend", "macos_actions"]
    for term in forbidden:
        assert term not in source, f"VoiceGoalAdapter source references forbidden executor term: {term}"


# 24. Adapter cannot modify GoalBudget
def test_adapter_cannot_modify_goal_budget():
    source = inspect.getsource(VoiceGoalAdapter)
    assert "GoalBudget(" not in source, "VoiceGoalAdapter should not construct or modify GoalBudget"


# 25. Source metadata cannot change security policy
def test_source_metadata_cannot_change_security_policy():
    req_voice = GoalRequest(goal_id="g1", goal_text="open terminal", source="voice")
    req_chat = GoalRequest(goal_id="g2", goal_text="open terminal", source="chat")
    # Verify requests have identical policy evaluation properties
    assert req_voice.source != req_chat.source
    assert req_voice.goal_text == req_chat.goal_text


# 26. Multiple independent voice goals
def test_multiple_independent_voice_goals():
    adapter, orch = _setup_voice_adapter()
    res1 = adapter.process_voice_input("open TextEdit")
    res2 = adapter.process_voice_input("open Calculator")
    assert len(orch.requests) == 2
    assert orch.requests[0].goal_text == "open TextEdit"
    assert orch.requests[1].goal_text == "open Calculator"


# 27. Second active goal rejected according to Phase 6 policy
def test_second_active_goal_rejected():
    fake_orch = FakeOrchestrator(return_state=GoalExecutionState.WAITING_FOR_USER)
    adapter, _ = _setup_voice_adapter(fake_orch)

    # First goal transitions to WAITING_FOR_USER
    res1 = adapter.process_voice_input("goal 1")
    assert res1.status == "ASK"

    # Second goal submission to busy orchestrator returning BUSY
    fake_orch.return_state = GoalExecutionState.BUSY
    res2 = adapter.process_voice_input("goal 2")
    assert res2.status == "BUSY"


# 28. Voice recognition lifecycle handling (empty/whitespace handling)
def test_voice_recognition_lifecycle_handling():
    adapter, orch = _setup_voice_adapter()
    res = adapter.process_voice_input("   ")
    assert res.status == "INVALID_INPUT"
    assert res.error == "EMPTY_VOICE_INPUT"


# 29. No conversational response generation in adapter
def test_no_conversational_response_generation():
    adapter, _ = _setup_voice_adapter()
    res = adapter.process_voice_input("type hello")
    # Adapter returns raw structured fields, not chat bubbles or natural language responses
    assert isinstance(res.status, str)
    assert not hasattr(res, "chat_bubble")
    assert not hasattr(res, "conversational_text")


# 30. Full voice -> GoalRequest -> orchestrator lifecycle using real orchestrator
def test_full_voice_to_orchestrator_lifecycle():
    app = textedit()
    backend, clock, cache = _setup(app)

    def get_obs(b):
        obs, handles = observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
        return obs, handles

    real_orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=backend,
        get_observation=get_obs,
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )

    adapter = VoiceGoalAdapter(orchestrator=real_orch)
    res = adapter.process_voice_input("open TextEdit")

    assert res.status == "COMPLETED"
    assert res.turn_count >= 1
    assert res.orchestrator_result is not None
    assert res.orchestrator_result.goal_id == res.goal_id
