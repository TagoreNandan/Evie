"""
Chat/API Goal Adapter for Evie Computer Control (Phase 7B).

Thin input adapter connecting text chat and HTTP API endpoints to the authoritative
Unified GoalExecutionOrchestrator.

CONVERT TEXT INPUT -> GoalRequest(source="chat") -> GoalExecutionOrchestrator

This module performs NO keyword routing, NO regex matching, NO hardcoded command maps,
NO direct OS calls, NO direct action execution, NO Safety Gate bypasses, and NO direct vision/LLM calls.
"""

import re
import uuid
from typing import Any, Dict, Optional, Tuple

from pydantic import Field, model_validator

from services.computer_control.models import _Strict
from services.computer_control.planner import AskDecision
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)


class ChatGoalResult(_Strict):
    """Bounded result object returned by ChatGoalAdapter."""
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


class ChatGoalAdapter:
    """
    Thin chat/API adapter that normalizes untrusted text input and forwards it to the
    GoalExecutionOrchestrator as a normalized GoalRequest with source="chat".
    """

    def __init__(
        self,
        orchestrator: GoalExecutionOrchestrator,
        max_input_length: int = 500
    ):
        self._orchestrator = orchestrator
        self.max_input_length = max_input_length

    @property
    def orchestrator(self) -> GoalExecutionOrchestrator:
        return self._orchestrator

    def process_chat_input(
        self,
        chat_text: str,
        goal_id: Optional[str] = None,
        session_id: str = "default_session",
        metadata: Optional[Dict[str, Any]] = None
    ) -> ChatGoalResult:
        """
        Validates untrusted text input and submits it to GoalExecutionOrchestrator.
        """
        if not chat_text or not isinstance(chat_text, str) or not chat_text.strip():
            return ChatGoalResult(
                goal_id=goal_id or "",
                status="INVALID_INPUT",
                error="EMPTY_CHAT_INPUT",
                reason="Chat input text is empty or missing"
            )

        text_clean = " ".join(chat_text.strip().split())
        if len(text_clean) > self.max_input_length:
            return ChatGoalResult(
                goal_id=goal_id or "",
                status="INVALID_INPUT",
                error="CHAT_INPUT_TOO_LONG",
                reason=f"Chat input exceeds maximum length of {self.max_input_length} characters"
            )

        gid = goal_id if (goal_id and goal_id.strip()) else f"chat-{uuid.uuid4().hex[:8]}"

        # Construct normalized GoalRequest with source="chat"
        goal_req = GoalRequest(
            goal_id=gid,
            goal_text=text_clean,
            source="chat",
            session_id=session_id,
            metadata=metadata
        )

        orch_res = self._orchestrator.execute_goal(goal_req)
        return self._format_result(orch_res)

    def resume_clarification(
        self,
        goal_id: str,
        user_response: str
    ) -> ChatGoalResult:
        """
        Validates user clarification text and resumes goal execution via the orchestrator.
        """
        if not goal_id or not isinstance(goal_id, str) or not goal_id.strip():
            return ChatGoalResult(
                goal_id="",
                status="INVALID_INPUT",
                error="MISSING_GOAL_ID",
                reason="Goal ID is required for clarification resume"
            )

        if not user_response or not isinstance(user_response, str) or not user_response.strip():
            return ChatGoalResult(
                goal_id=goal_id,
                status="INVALID_INPUT",
                error="EMPTY_CLARIFICATION",
                reason="Clarification response text is empty"
            )

        text_clean = " ".join(user_response.strip().split())
        if len(text_clean) > self.max_input_length:
            return ChatGoalResult(
                goal_id=goal_id,
                status="INVALID_INPUT",
                error="CLARIFICATION_TOO_LONG",
                reason=f"Clarification text exceeds maximum length of {self.max_input_length} characters"
            )

        orch_res = self._orchestrator.resume_with_clarification(goal_id, text_clean)
        return self._format_result(orch_res)

    def cancel_goal(self, goal_id: str) -> ChatGoalResult:
        """
        Cancels an active goal via the orchestrator.
        """
        if not goal_id or not isinstance(goal_id, str) or not goal_id.strip():
            return ChatGoalResult(
                goal_id="",
                status="INVALID_INPUT",
                error="MISSING_GOAL_ID",
                reason="Goal ID is required for cancellation"
            )

        orch_res = self._orchestrator.cancel_goal(goal_id)
        return self._format_result(orch_res)

    def _format_result(self, res: GoalExecutionResult) -> ChatGoalResult:
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

        return ChatGoalResult(
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
