"""
Silent Execution Response Policy for Evie Computer Control (Step 7).

Establishes a pure execution-result response policy that separates internal goal execution
results (verification, status, context, audit) from user-facing conversational replies.

By default:
- Successful computer control goals (COMPLETED) are SILENT (user_reply="").
- Clarification requests (WAITING_FOR_USER / ASK) produce user-facing questions.
- Meaningful execution failures (BLOCKED, FAILED, CANNOT_PROCEED, BUDGET_EXCEEDED, BUSY)
  produce concise, bounded error responses without exposing internal metadata.
- Cancellation (CANCELLED) is SILENT by default (user_reply="").

Security & Architecture Invariants:
1. Performs NO action execution, NO target selection, NO coordinate/selector calculation,
   NO worker invocation, NO Safety Gate bypasses, and NO AppleScript/shell execution.
2. Does NOT synthesize TTS or alter internal execution results, verification, audit events, or context.
3. Suppresses conversational prose ("Done.") on COMPLETED goals while keeping structured API output intact.
"""

from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field


class ResponseKind(str, Enum):
    SILENT = "SILENT"
    ASK = "ASK"
    RESPOND = "RESPOND"


class ExecutionResponseDecision(BaseModel):
    """Closed response decision model."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResponseKind
    user_reply: str = Field("", max_length=500)
    reason: Optional[str] = Field(None, max_length=200)


def determine_execution_response(
    status: str,
    verification_summary: Optional[str] = None,
    question: Optional[str] = None,
    reason: str = ""
) -> ExecutionResponseDecision:
    """
    Authoritative response policy mapping execution status to user-facing reply decision.
    """
    stat_upper = (status or "").upper()

    if stat_upper == "COMPLETED":
        return ExecutionResponseDecision(
            kind=ResponseKind.SILENT,
            user_reply="",
            reason="COMPLETED_SILENT_BY_DEFAULT"
        )
    elif stat_upper in ("ASK", "WAITING_FOR_USER"):
        reply = question or reason or "Which document do you want me to use?"
        return ExecutionResponseDecision(
            kind=ResponseKind.ASK,
            user_reply=reply[:500],
            reason="CLARIFICATION_REQUIRED"
        )
    elif stat_upper == "CANCELLED":
        return ExecutionResponseDecision(
            kind=ResponseKind.SILENT,
            user_reply="",
            reason="CANCELLED_SILENT"
        )
    elif stat_upper == "BLOCKED":
        return ExecutionResponseDecision(
            kind=ResponseKind.RESPOND,
            user_reply="I can't perform that action.",
            reason="ACTION_BLOCKED"
        )
    elif stat_upper == "BUSY":
        return ExecutionResponseDecision(
            kind=ResponseKind.RESPOND,
            user_reply="Another goal is already in progress.",
            reason="ORCHESTRATOR_BUSY"
        )
    elif stat_upper == "BUDGET_EXCEEDED":
        return ExecutionResponseDecision(
            kind=ResponseKind.RESPOND,
            user_reply="I stopped because execution limits were reached.",
            reason="BUDGET_EXCEEDED"
        )
    elif stat_upper in ("CANNOT_PROCEED", "FAILED"):
        return ExecutionResponseDecision(
            kind=ResponseKind.RESPOND,
            user_reply="I couldn't complete that task.",
            reason="EXECUTION_FAILED"
        )
    else:
        return ExecutionResponseDecision(
            kind=ResponseKind.RESPOND,
            user_reply="I couldn't complete that task.",
            reason="UNKNOWN_STATUS"
        )
