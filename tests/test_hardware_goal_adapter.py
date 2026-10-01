"""
Tests for Phase 7C — Hardware Goal Adapter.
"""

import pytest
from unittest.mock import MagicMock
from pydantic import ValidationError

from services.computer_control.hardware_adapter import (
    HardwareGoalAdapter, HardwareGoalInput, HardwareGoalResult, FakeHardwareInputProvider
)
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.planner import (
    DoneDecision, AskDecision, CannotProceedDecision, GoalBudget
)
from services.computer_control.models import UniversalAction, ActivateAppArgs
from services.computer_control.gate import GatePolicy, DEFAULT_POLICY


class FakeOrchestrator:
    def __init__(self, response_result: GoalExecutionResult = None):
        self.submitted_requests = []
        self.resume_calls = []
        self.cancel_calls = []
        self._response = response_result or GoalExecutionResult(
            goal_id="hw-100",
            status=GoalExecutionState.COMPLETED,
            turn_count=1,
            clarification_count=0,
            elapsed_time_s=0.5,
            reason="Goal completed successfully",
            final_decision=DoneDecision(reason="Done"),
            verification_summary="Verified"
        )
        self.is_busy = False

    def execute_goal(self, request: GoalRequest) -> GoalExecutionResult:
        if self.is_busy:
            return GoalExecutionResult(
                goal_id=request.goal_id,
                status=GoalExecutionState.BUSY,
                reason="Another computer control goal is currently active"
            )
        self.submitted_requests.append(request)
        res = GoalExecutionResult(
            goal_id=request.goal_id,
            status=self._response.status,
            turn_count=self._response.turn_count,
            clarification_count=self._response.clarification_count,
            elapsed_time_s=self._response.elapsed_time_s,
            reason=self._response.reason,
            final_decision=self._response.final_decision,
            verification_summary=self._response.verification_summary
        )
        return res

    def resume_with_clarification(self, goal_id: str, user_response: str) -> GoalExecutionResult:
        self.resume_calls.append((goal_id, user_response))
        return GoalExecutionResult(
            goal_id=goal_id,
            status=GoalExecutionState.COMPLETED,
            turn_count=2,
            clarification_count=1,
            elapsed_time_s=1.0,
            reason="Resumed and completed",
            final_decision=DoneDecision(reason="Done")
        )

    def cancel_goal(self, goal_id: str) -> GoalExecutionResult:
        self.cancel_calls.append(goal_id)
        return GoalExecutionResult(
            goal_id=goal_id,
            status=GoalExecutionState.CANCELLED,
            reason="Goal cancelled by user"
        )


def test_valid_hardware_goal_creates_goal_request():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open TextEdit.")
    
    assert res.status == "COMPLETED"
    assert len(orch.submitted_requests) == 1
    req = orch.submitted_requests[0]
    assert req.goal_text == "Open TextEdit."
    assert req.source == "hardware"


def test_source_is_exactly_hardware():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    adapter.process_hardware_input("Open Calculator.")
    assert orch.submitted_requests[0].source == "hardware"


def test_empty_goal_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res1 = adapter.process_hardware_input("")
    res2 = adapter.process_hardware_input("   ")
    
    assert res1.status == "INVALID_INPUT"
    assert res1.error == "EMPTY_HARDWARE_INPUT"
    assert res2.status == "INVALID_INPUT"
    assert len(orch.submitted_requests) == 0


def test_oversized_goal_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch, max_input_length=50)
    oversized = "A" * 51
    res = adapter.process_hardware_input(oversized)
    
    assert res.status == "INVALID_INPUT"
    assert res.error == "HARDWARE_INPUT_TOO_LONG"
    assert len(orch.submitted_requests) == 0


def test_invalid_event_id_rejected():
    with pytest.raises(ValidationError):
        HardwareGoalInput(
            event_id="A" * 65,  # exceeds max_length 64
            goal_text="Valid text",
            device_id="dev-1"
        )


def test_bounded_device_metadata():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    inp = {
        "event_id": "evt-1234",
        "goal_text": "Open Photo Booth.",
        "device_id": "raspberry-pi-zero-w",
        "metadata": {"location": "desk_station"}
    }
    res = adapter.process_hardware_input(inp)
    assert res.status == "COMPLETED"
    req = orch.submitted_requests[0]
    assert req.metadata["event_id"] == "evt-1234"
    assert req.metadata["device_id"] == "raspberry-pi-zero-w"
    assert req.metadata["location"] == "desk_station"


def test_natural_language_multistep_goal_forwarded_unchanged():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    goal = "Open TextEdit and type Hello Evie and save file."
    adapter.process_hardware_input(goal)
    assert orch.submitted_requests[0].goal_text == goal


def test_no_keyword_routing():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    adapter.process_hardware_input("open TextEdit")
    adapter.process_hardware_input("click submit button")
    adapter.process_hardware_input("take a photo")
    
    assert len(orch.submitted_requests) == 3
    for req in orch.submitted_requests:
        assert req.source == "hardware"


def test_exactly_one_goal_request_submitted():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    adapter.process_hardware_input("Find button")
    assert len(orch.submitted_requests) == 1


def test_completed_result_propagation():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open TextEdit.")
    assert res.status == "COMPLETED"
    assert res.reason == "Goal completed successfully"


def test_ask_result_propagation():
    ask_res = GoalExecutionResult(
        goal_id="hw-ask",
        status=GoalExecutionState.WAITING_FOR_USER,
        turn_count=1,
        clarification_count=0,
        reason="Needs user feedback",
        final_decision=AskDecision(question="Which window should be focused?", reason="Ambiguous window")
    )
    orch = FakeOrchestrator(response_result=ask_res)
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Select main window.")
    assert res.status == "ASK"
    assert res.question == "Which window should be focused?"


def test_cannot_proceed_propagation():
    cant_res = GoalExecutionResult(
        goal_id="hw-cant",
        status=GoalExecutionState.CANNOT_PROCEED,
        turn_count=1,
        reason="Target application missing",
        final_decision=CannotProceedDecision(reason="Target missing", explanation="Target missing")
    )
    orch = FakeOrchestrator(response_result=cant_res)
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open MissingApp.")
    assert res.status == "CANNOT_PROCEED"


def test_busy_propagation():
    orch = FakeOrchestrator()
    orch.is_busy = True
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open TextEdit.")
    assert res.status == "BUSY"


def test_cancelled_propagation():
    cancel_res = GoalExecutionResult(
        goal_id="hw-cancel",
        status=GoalExecutionState.CANCELLED,
        reason="User cancelled goal"
    )
    orch = FakeOrchestrator(response_result=cancel_res)
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open TextEdit.")
    assert res.status == "CANCELLED"


def test_clarification_resume_boundary():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.resume_clarification("hw-100", "Focus window 1")
    assert res.status == "COMPLETED"
    assert len(orch.resume_calls) == 1
    assert orch.resume_calls[0] == ("hw-100", "Focus window 1")


def test_raw_universal_action_payload_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    malicious = {
        "event_id": "evt-1",
        "goal_text": "Valid text",
        "action": {"op": "activate_app", "app_name": "Terminal"}
    }
    res = adapter.process_hardware_input(malicious)
    assert res.status == "INVALID_INPUT"
    assert res.error == "MALFORMED_HARDWARE_EVENT"


def test_raw_coordinates_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    malicious = {
        "event_id": "evt-2",
        "goal_text": "Valid text",
        "x": 100,
        "y": 200
    }
    res = adapter.process_hardware_input(malicious)
    assert res.status == "INVALID_INPUT"
    assert res.error == "MALFORMED_HARDWARE_EVENT"


def test_selector_payload_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    malicious = {
        "event_id": "evt-3",
        "goal_text": "Valid text",
        "selectors": [".button"]
    }
    res = adapter.process_hardware_input(malicious)
    assert res.status == "INVALID_INPUT"


def test_shell_payload_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    malicious = {
        "event_id": "evt-4",
        "goal_text": "Valid text",
        "shell": "rm -rf /"
    }
    res = adapter.process_hardware_input(malicious)
    assert res.status == "INVALID_INPUT"


def test_applescript_payload_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    malicious = {
        "event_id": "evt-5",
        "goal_text": "Valid text",
        "applescript": "tell app TextEdit to quit"
    }
    res = adapter.process_hardware_input(malicious)
    assert res.status == "INVALID_INPUT"


def test_raw_ax_pointer_payload_rejected():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    malicious = {
        "event_id": "evt-6",
        "goal_text": "Valid text",
        "ax_pointer": 0x123456
    }
    res = adapter.process_hardware_input(malicious)
    assert res.status == "INVALID_INPUT"


def test_adapter_cannot_access_executor_directly():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    assert not hasattr(adapter, "execute_action")
    assert not hasattr(adapter, "_executor")


def test_adapter_cannot_modify_goal_budget():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    adapter.process_hardware_input("Open Calculator.")
    assert not hasattr(adapter, "budget")


def test_adapter_cannot_modify_safety_gate_policy():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    adapter.process_hardware_input("Open Calculator.")
    assert not hasattr(adapter, "safety_policy")


def test_source_metadata_cannot_change_security_behavior():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    adapter.process_hardware_input("Open TextEdit.")
    req = orch.submitted_requests[0]
    assert req.source == "hardware"


def test_concurrent_hardware_goal_returns_busy():
    orch = FakeOrchestrator()
    orch.is_busy = True
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open Calculator.")
    assert res.status == "BUSY"


def test_result_redaction():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    res = adapter.process_hardware_input("Open TextEdit.")
    
    dict_res = res.model_dump()
    assert "screenshot" not in dict_res
    assert "api_key" not in dict_res
    assert "ax_pointer" not in dict_res


def test_fake_hardware_provider_lifecycle():
    provider = FakeHardwareInputProvider()
    raw = {
        "event_id": "hw-evt-99",
        "goal_text": "Open Calculator.",
        "device_id": "taz-pi-01"
    }
    parsed = provider.receive_event(raw)
    assert parsed.event_id == "hw-evt-99"
    assert parsed.goal_text == "Open Calculator."
    assert parsed.device_id == "taz-pi-01"


def test_malformed_hardware_event_rejected():
    provider = FakeHardwareInputProvider()
    with pytest.raises(ValueError):
        provider.receive_event("not a dict")


def test_full_hardware_to_orchestrator_lifecycle():
    orch = FakeOrchestrator()
    adapter = HardwareGoalAdapter(orchestrator=orch)
    
    raw_event = {
        "event_id": "evt-live-1",
        "goal_text": "Open TextEdit and type Hello Evie.",
        "device_id": "pi-zero-01"
    }
    
    res = adapter.process_hardware_input(raw_event)
    assert res.status == "COMPLETED"
    assert len(orch.submitted_requests) == 1
    req = orch.submitted_requests[0]
    assert req.goal_text == "Open TextEdit and type Hello Evie."
    assert req.source == "hardware"
    assert req.metadata["event_id"] == "evt-live-1"
    assert req.metadata["device_id"] == "pi-zero-01"
