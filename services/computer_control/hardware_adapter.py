"""
Hardware Goal Adapter for Evie Computer Control (Phase 7C).

Thin input adapter connecting Raspberry Pi / Taz / hardware event inputs to the
authoritative Unified GoalExecutionOrchestrator.

CONVERT HARDWARE INPUT -> GoalRequest(source="hardware") -> GoalExecutionOrchestrator

This module performs NO keyword routing, NO direct OS calls, NO direct action execution,
NO GPIO control, NO shell/AppleScript execution, and NO Safety Gate bypasses.
"""

import uuid
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from pydantic import Field, model_validator

from services.computer_control.models import _Strict
from services.computer_control.planner import AskDecision
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)


class HardwareGoalInput(_Strict):
    """
    Bounded hardware input representation containing user intent.
    Strictly forbids raw action payloads, coordinates, shell, AppleScript, or AX pointers.
    """
    event_id: str = Field(..., max_length=64)
    goal_text: str = Field(..., max_length=500)
    device_id: str = Field("hardware-device-01", max_length=64)
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def validate_no_forbidden_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            forbidden = {
                "action", "op", "click", "coordinates", "x", "y", "selectors",
                "target_spec", "shell", "applescript", "ax_pointer", "executable_path",
                "bypass_gate", "planner_instructions"
            }
            found = forbidden.intersection(set(data.keys()))
            if found:
                raise ValueError(f"Forbidden raw execution payload keys found in hardware input: {found}")
        return data


class HardwareGoalResult(_Strict):
    """Bounded result object returned by HardwareGoalAdapter."""
    goal_id: str = Field(..., max_length=64)
    status: str = Field(..., max_length=64)
    turn_count: int = Field(0, ge=0)
    clarification_count: int = Field(0, ge=0)
    elapsed_time_s: float = Field(0.0, ge=0.0)
    reason: str = Field("", max_length=500)
    question: Optional[str] = Field(None, max_length=300)
    error: Optional[str] = Field(None, max_length=200)
    verification_summary: Optional[str] = Field(None, max_length=500)
    orchestrator_result: Optional[GoalExecutionResult] = None


@runtime_checkable
class HardwareInputProvider(Protocol):
    """Abstract hardware transport boundary interface."""
    def receive_event(self, raw_event: Dict[str, Any]) -> HardwareGoalInput:
        ...


class FakeHardwareInputProvider:
    """
    Deterministic fake hardware event provider for testing transport ingestion.
    Validates inbound event dicts into HardwareGoalInput schemas.
    """
    def receive_event(self, raw_event: Dict[str, Any]) -> HardwareGoalInput:
        if not isinstance(raw_event, dict):
            raise ValueError("Hardware event must be a dictionary")
        return HardwareGoalInput.model_validate(raw_event)


class HardwareGoalAdapter:
    """
    Thin hardware adapter that normalizes untrusted hardware input and forwards it to the
    GoalExecutionOrchestrator as a normalized GoalRequest with source="hardware".
    """

    def __init__(
        self,
        orchestrator: GoalExecutionOrchestrator,
        max_input_length: int = 500,
        provider: Optional[HardwareInputProvider] = None
    ):
        self._orchestrator = orchestrator
        self.max_input_length = max_input_length
        self._provider = provider or FakeHardwareInputProvider()

    @property
    def orchestrator(self) -> GoalExecutionOrchestrator:
        return self._orchestrator

    def process_hardware_input(
        self,
        hardware_input: Any,
        goal_id: Optional[str] = None,
        session_id: str = "default_session"
    ) -> HardwareGoalResult:
        """
        Validates untrusted hardware goal input and submits it to GoalExecutionOrchestrator.
        Supports receiving HardwareGoalInput, raw event dicts (parsed via provider), or text strings.
        """
        if isinstance(hardware_input, dict):
            try:
                inp = self._provider.receive_event(hardware_input)
                goal_text = inp.goal_text
                event_id = inp.event_id
                device_id = inp.device_id
                meta = inp.metadata
            except Exception as e:
                return HardwareGoalResult(
                    goal_id=goal_id or "",
                    status="INVALID_INPUT",
                    error="MALFORMED_HARDWARE_EVENT",
                    reason=f"Hardware event validation failed: {str(e)}"
                )
        elif isinstance(hardware_input, HardwareGoalInput):
            goal_text = hardware_input.goal_text
            event_id = hardware_input.event_id
            device_id = hardware_input.device_id
            meta = hardware_input.metadata
        elif isinstance(hardware_input, str):
            goal_text = hardware_input
            event_id = f"hw-evt-{uuid.uuid4().hex[:8]}"
            device_id = "hardware-device-01"
            meta = None
        else:
            return HardwareGoalResult(
                goal_id=goal_id or "",
                status="INVALID_INPUT",
                error="INVALID_HARDWARE_INPUT",
                reason="Hardware input must be a text string, dictionary, or HardwareGoalInput"
            )

        if not goal_text or not isinstance(goal_text, str) or not goal_text.strip():
            return HardwareGoalResult(
                goal_id=goal_id or "",
                status="INVALID_INPUT",
                error="EMPTY_HARDWARE_INPUT",
                reason="Hardware goal text is empty or missing"
            )

        text_clean = " ".join(goal_text.strip().split())
        if len(text_clean) > self.max_input_length:
            return HardwareGoalResult(
                goal_id=goal_id or "",
                status="INVALID_INPUT",
                error="HARDWARE_INPUT_TOO_LONG",
                reason=f"Hardware goal text exceeds maximum length of {self.max_input_length} characters"
            )

        gid = goal_id if (goal_id and goal_id.strip()) else f"hw-{uuid.uuid4().hex[:8]}"

        req_meta = {
            "event_id": event_id,
            "device_id": device_id
        }
        if meta:
            req_meta.update(meta)

        # Construct normalized GoalRequest with source="hardware"
        goal_req = GoalRequest(
            goal_id=gid,
            goal_text=text_clean,
            source="hardware",
            session_id=session_id,
            metadata=req_meta
        )

        orch_res = self._orchestrator.execute_goal(goal_req)
        return self._format_result(orch_res)

    def resume_clarification(
        self,
        goal_id: str,
        user_response: str
    ) -> HardwareGoalResult:
        """
        Validates user clarification text and resumes goal execution via the orchestrator.
        """
        if not goal_id or not isinstance(goal_id, str) or not goal_id.strip():
            return HardwareGoalResult(
                goal_id="",
                status="INVALID_INPUT",
                error="MISSING_GOAL_ID",
                reason="Goal ID is required for clarification resume"
            )

        if not user_response or not isinstance(user_response, str) or not user_response.strip():
            return HardwareGoalResult(
                goal_id=goal_id,
                status="INVALID_INPUT",
                error="EMPTY_CLARIFICATION",
                reason="Clarification response text is empty"
            )

        text_clean = " ".join(user_response.strip().split())
        if len(text_clean) > self.max_input_length:
            return HardwareGoalResult(
                goal_id=goal_id,
                status="INVALID_INPUT",
                error="CLARIFICATION_TOO_LONG",
                reason=f"Clarification text exceeds maximum length of {self.max_input_length} characters"
            )

        orch_res = self._orchestrator.resume_with_clarification(goal_id, text_clean)
        return self._format_result(orch_res)

    def cancel_goal(self, goal_id: str) -> HardwareGoalResult:
        """
        Cancels an active goal via the orchestrator.
        """
        if not goal_id or not isinstance(goal_id, str) or not goal_id.strip():
            return HardwareGoalResult(
                goal_id="",
                status="INVALID_INPUT",
                error="MISSING_GOAL_ID",
                reason="Goal ID is required for cancellation"
            )

        orch_res = self._orchestrator.cancel_goal(goal_id)
        return self._format_result(orch_res)

    def _format_result(self, res: GoalExecutionResult) -> HardwareGoalResult:
        status_map = {
            GoalExecutionState.COMPLETED: "COMPLETED",
            GoalExecutionState.WAITING_FOR_USER: "ASK",
            GoalExecutionState.CANNOT_PROCEED: "CANNOT_PROCEED",
            GoalExecutionState.BLOCKED: "BLOCKED",
            GoalExecutionState.CANCELLED: "CANCELLED",
            GoalExecutionState.BUSY: "BUSY",
            GoalExecutionState.BUDGET_EXCEEDED: "BUDGET_EXCEEDED",
            GoalExecutionState.FAILED: "FAILED",
        }

        status_str = status_map.get(res.status, res.status.value)
        question_text = None

        if res.final_decision and isinstance(res.final_decision, AskDecision):
            question_text = res.final_decision.question

        return HardwareGoalResult(
            goal_id=res.goal_id,
            status=status_str,
            turn_count=res.turn_count,
            clarification_count=res.clarification_count,
            elapsed_time_s=res.elapsed_time_s,
            reason=res.reason,
            question=question_text,
            verification_summary=res.verification_summary,
            orchestrator_result=res
        )
