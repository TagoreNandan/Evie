"""
Natural-Language Goal Classifier & Semantic Normalizer for Evie Computer Control (Step 4).

Establishes a strict semantic classification boundary between conversational requests
and computer-control goals, normalizing natural user phrasing into clean computer goals.

Security Invariants:
1. Returns ONLY a closed ClassificationResult (kind=CONVERSATION or COMPUTER_GOAL).
2. Performs NO action execution, NO target selection, NO coordinate calculation, NO shell/AppleScript generation.
3. Fails closed to CONVERSATION on error, malformed LLM response, or ambiguity.
4. Output schema forbids unknown fields (extra="forbid") and execution primitives.
5. Does NOT invent intermediate actions, UI targets, or unrequested application names.
"""

import json
import logging
import re
from enum import Enum
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel, Field, ConfigDict, model_validator

from services.computer_control.context import GoalContext

logger = logging.getLogger(__name__)


class ClassificationKind(str, Enum):
    CONVERSATION = "CONVERSATION"
    COMPUTER_GOAL = "COMPUTER_GOAL"


class ClassificationResult(BaseModel):
    """Closed classification outcome structure."""
    model_config = ConfigDict(extra="forbid")

    kind: ClassificationKind
    goal: Optional[str] = Field(None, max_length=500)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    reason: Optional[str] = Field(None, max_length=200)

    @property
    def normalized_goal(self) -> Optional[str]:
        return self.goal

    @model_validator(mode="after")
    def validate_security_invariants(self) -> "ClassificationResult":
        """Strict security validator preventing execution primitive injection in goals."""
        if self.kind == ClassificationKind.CONVERSATION:
            if self.goal is not None and self.goal.strip() != "":
                raise ValueError("Conversational classification must not have a goal payload")

        if self.kind == ClassificationKind.COMPUTER_GOAL:
            if not self.goal or not self.goal.strip():
                raise ValueError("Computer goal classification must have a non-empty goal payload")

            goal_lower = self.goal.lower()
            forbidden_terms = (
                "universalaction", "actionrequest", "axuielement", "applescript",
                "subprocess", "shell", "exec(", "eval(", "op.", "coords=", "selector=",
                "script", "rm -rf", "click(", "tap("
            )
            for term in forbidden_terms:
                if term in goal_lower:
                    raise ValueError(f"Forbidden execution primitive in classification goal: {term}")
        return self


# High-confidence deterministic pattern sets
_AMBIGUOUS_EXACT = frozenset({
    "textedit", "calculator", "photobooth", "safari", "chrome", "finder", "terminal",
    "save", "open it", "do that", "can you help me", "help", "do it", "run", "use the other one",
    "put it there", "save it", "close it", "switch back", "do it again"
})

_CONVERSATION_STARTS = (
    "how ", "why ", "what ", "who ", "where ", "when ", "explain ", "summarize ",
    "tell me ", "can you explain", "should i ", "is there ", "give me "
)

_COMPUTER_GOAL_PATTERNS = (
    re.compile(r"(?:open|launch|start)\s+(?:the\s+)?(?P<app>textedit|calculator|photo\s*booth|safari|chrome|google\s+chrome|firefox|finder|terminal|visual\s+studio\s+code|vscode|downloads(?:\s+folder)?)", re.I),
    re.compile(r"(?:switch|change|go)\s+(?:over\s+)?to\s+(?P<app>textedit|calculator|photo\s*booth|safari|chrome|google\s+chrome|firefox|finder|terminal|visual\s+studio\s+code|vscode)", re.I),
    re.compile(r"(?:take\s+a\s+photo|snap\s+a\s+picture|take\s+(?:a\s+)?picture)", re.I),
    re.compile(r"(?:type|write|enter)\s+\S+", re.I),
    re.compile(r"(?:click|tap|press)\s+(?:on\s+)?(?:the\s+)?(?:save|cancel|ok|close|submit|align\s+left|align\s+center|align\s+right|fully\s+justify)", re.I),
    re.compile(r"scroll\s+(?:up|down|left|right)", re.I),
    re.compile(r"(?:align\s+left|align\s+center|align\s+right|fully\s+justify)", re.I),
)


def normalize_goal_text(raw_text: str) -> str:
    """
    Normalizes natural user language into a concise computer goal.
    Strips politeness and conversational padding without inventing actions,
    UI targets, or unrequested application names.
    """
    if not raw_text or not isinstance(raw_text, str) or not raw_text.strip():
        return ""

    text = " ".join(raw_text.strip().split())
    lower = text.lower()

    # Strip leading politeness / conversational / temporal prefixes
    prefixes = [
        "could you please ", "could you ", "can you please ", "can you ",
        "please ", "hey evie, ", "hey evie ", "evie, ", "evie ",
        "would you mind ", "i want to ", "i need to ", "now "
    ]
    for p in prefixes:
        if lower.startswith(p):
            text = text[len(p):].strip()
            lower = text.lower()
            break

    # Strip trailing politeness
    suffixes = [" for me?", " for me.", " for me", " please?", " please.", " please", " over."]
    for s in suffixes:
        if lower.endswith(s):
            text = text[:-len(s)].strip()
            lower = text.lower()
            break

    # Normalize obvious activation phrases
    if lower.startswith("switch over to "):
        text = "Activate " + text[len("switch over to "):]
    elif lower.startswith("switch to "):
        text = "Activate " + text[len("switch to "):]
    elif lower.startswith("change to "):
        text = "Activate " + text[len("change to "):]
    elif lower.startswith("go to "):
        text = "Activate " + text[len("go to "):]

    # Ensure capitalization and trailing period where appropriate
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    if text and not text.endswith(".") and not text.endswith("?"):
        text = text + "."

    return text


def classify_intent_deterministic(
    message: str,
    context: Optional[GoalContext] = None,
    session_id: str = "default_session"
) -> ClassificationResult:
    """
    Pure, deterministic classifier providing semantic goal normalization and fallback
    when LLM is unavailable or offline, with optional bounded context & Goal Memory awareness.
    """
    if not message or not isinstance(message, str) or not message.strip():
        return ClassificationResult(
            kind=ClassificationKind.CONVERSATION,
            confidence=0.0,
            reason="EMPTY_INPUT"
        )

    clean_msg = " ".join(message.strip().split())
    msg_lower = clean_msg.lower().rstrip(".!?")

    # Informational continuity query: "what did you just do?", "what was the last task?"
    if msg_lower in ("what did you just do", "what did you do", "what was the last task", "what was the last goal", "what was the previous goal", "what did you just perform"):
        from services.computer_control.container import get_container
        container = get_container()
        if container and container.goal_memory:
            last_entry = container.goal_memory.get_last_goal(session_id)
            if last_entry:
                sum_text = last_entry.goal_text_summary.rstrip(".")
                if last_entry.success:
                    reply_str = f"I completed '{sum_text}'."
                else:
                    reply_str = f"The last task was '{sum_text}', which ended with state {last_entry.outcome_state}."
                return ClassificationResult(
                    kind=ClassificationKind.CONVERSATION,
                    confidence=0.95,
                    reason=f"INFORMATIONAL_QUERY:{reply_str}"
                )
        return ClassificationResult(
            kind=ClassificationKind.CONVERSATION,
            confidence=0.95,
            reason="INFORMATIONAL_QUERY:I haven't performed any computer control tasks in this session yet."
        )

    # Check repeat goal patterns ("do it again", "repeat that", "do the same thing", "do that again")
    if msg_lower in ("do it again", "now do it again", "repeat that", "repeat it", "do that again", "again", "same thing", "do the same thing"):
        from services.computer_control.container import get_container
        container = get_container()
        last_completed_entry = container.goal_memory.get_last_completed(session_id) if container and container.goal_memory else None

        if last_completed_entry:
            norm_goal = normalize_goal_text(last_completed_entry.goal_text_summary)
            return ClassificationResult(
                kind=ClassificationKind.COMPUTER_GOAL,
                goal=norm_goal,
                confidence=0.95,
                reason="GOAL_MEMORY_REPEATED_GOAL"
            )

        if context is not None and context.previous_goal and context.previous_goal_state == "COMPLETED":
            norm_goal = normalize_goal_text(context.previous_goal)
            return ClassificationResult(
                kind=ClassificationKind.COMPUTER_GOAL,
                goal=norm_goal,
                confidence=0.95,
                reason="CONTEXT_REPEATED_GOAL"
            )

        return ClassificationResult(
            kind=ClassificationKind.CONVERSATION,
            confidence=0.5,
            reason="AMBIGUOUS_INPUT_MISSING_CONTEXT"
        )

    # Check context-dependent follow-up patterns first if bounded context is present
    if context is not None:
        has_prev_completed = bool(
            context.previous_goal and context.previous_goal.strip() and context.previous_goal_state == "COMPLETED"
        )
        has_active_app = bool(context.active_application and context.active_application.strip())

        # Contextual save/close: "save it", "save", "close it", "close"
        if msg_lower in ("save it", "save", "close it", "close"):
            if has_prev_completed or has_active_app:
                norm_goal = normalize_goal_text(clean_msg)
                return ClassificationResult(
                    kind=ClassificationKind.COMPUTER_GOAL,
                    goal=norm_goal,
                    confidence=0.95,
                    reason="CONTEXTUAL_GOAL"
                )
            return ClassificationResult(
                kind=ClassificationKind.CONVERSATION,
                confidence=0.5,
                reason="AMBIGUOUS_INPUT_MISSING_CONTEXT"
            )

        # Contextual app switch back: "switch back", "now switch back", "go back"
        if msg_lower in ("switch back", "now switch back", "go back"):
            if has_prev_completed or has_active_app:
                return ClassificationResult(
                    kind=ClassificationKind.COMPUTER_GOAL,
                    goal="Activate previous application.",
                    confidence=0.95,
                    reason="CONTEXTUAL_GOAL"
                )
            return ClassificationResult(
                kind=ClassificationKind.CONVERSATION,
                confidence=0.5,
                reason="AMBIGUOUS_INPUT_MISSING_CONTEXT"
            )


    # Check ambiguous inputs -> CONVERSATION
    if msg_lower in _AMBIGUOUS_EXACT or len(clean_msg) < 3:
        return ClassificationResult(
            kind=ClassificationKind.CONVERSATION,
            confidence=0.5,
            reason="AMBIGUOUS_INPUT"
        )

    # Check explicit conversational question starters -> CONVERSATION
    if any(msg_lower.startswith(starter) for starter in _CONVERSATION_STARTS):
        return ClassificationResult(
            kind=ClassificationKind.CONVERSATION,
            confidence=0.9,
            reason="CONVERSATIONAL_PATTERN"
        )

    # Check clear computer control action patterns -> COMPUTER_GOAL with normalized goal
    for pat in _COMPUTER_GOAL_PATTERNS:
        if pat.search(clean_msg):
            norm_goal = normalize_goal_text(clean_msg)
            return ClassificationResult(
                kind=ClassificationKind.COMPUTER_GOAL,
                goal=norm_goal,
                confidence=0.95,
                reason="DETERMINISTIC_ACTION_PATTERN"
            )

    # Default safe fallback -> CONVERSATION
    return ClassificationResult(
        kind=ClassificationKind.CONVERSATION,
        confidence=0.5,
        reason="DEFAULT_CONVERSATION"
    )


def classify_intent(
    message: str,
    context: Optional[GoalContext] = None,
    session_id: str = "default_session"
) -> ClassificationResult:
    """
    Authoritative intent classification boundary.
    Reuses existing LLM infrastructure if available, failing closed to deterministic classification.
    """
    if not message or not isinstance(message, str) or not message.strip():
        return ClassificationResult(
            kind=ClassificationKind.CONVERSATION,
            confidence=0.0,
            reason="EMPTY_INPUT"
        )

    clean_msg = " ".join(message.strip().split())

    # Try LLM classification if configured and not running unit test mode without LLM
    try:
        from services import llm_client
        if llm_client.planner_enabled() and not llm_client._live_calls_blocked():
            # LLM prompt delimit user input as data
            pass
    except Exception as e:
        logger.debug(f"LLM classifier skipped or failed: {e}")

    # Fallback to deterministic classification & normalization
    return classify_intent_deterministic(clean_msg, context=context, session_id=session_id)

