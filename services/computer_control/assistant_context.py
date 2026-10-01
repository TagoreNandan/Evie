"""
Unified Assistant Context Layer for Evie (Step 11).

Combines all contextual sources into a single, read-only-at-execution-time AssistantContext:
1. Current Conversation Context
2. Active Goal Context (GoalContext)
3. Short-Term Goal Memory (GoalMemoryManager)
4. Persistent Personal Memory (PersonalMemoryManager)
5. Verified System Context (VerifiedSystemContext)

Security Invariants:
1. Pure DATA/CONTEXT only — NEVER execution authority.
2. Cannot create UniversalAction objects, execute shell/AppleScript, or bypass Safety Gate.
3. Source Authority Hierarchy:
   Verified System Context (highest) > Active Goal Context > Current Conversation > Goal Memory > Personal Memory (lowest).
4. Strictly bounded lengths, counts, and schemas (extra="forbid", frozen=True).
5. Cross-channel consistency: Chat, Voice, and API share the exact same context builder.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.computer_control.context import (
    GoalContext,
    VerifiedSystemContext,
    UserContext,
    GoalContextManager,
)
from services.computer_control.goal_memory import GoalMemoryManager, GoalMemoryEntry
import redaction

logger = logging.getLogger(__name__)


class ConversationContext(BaseModel):
    """Bounded, normalized representation of current conversation turn."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field("default", max_length=64)
    recent_utterance: Optional[str] = Field(None, max_length=500)
    channel: str = Field("chat", max_length=20)

    @model_validator(mode="after")
    def validate_security(self) -> "ConversationContext":
        for val in (self.session_id, self.recent_utterance, self.channel):
            if val:
                val_lower = val.lower()
                forbidden = (
                    "universalaction", "axuielement", "applescript", "subprocess", "exec(", "eval(",
                    "coords=", "selector=", "bypass_safety", "approved_bypass", "safety_override", "gate_override"
                )
                for term in forbidden:
                    if term in val_lower:
                        raise ValueError(f"Forbidden security primitive in conversation context: {term}")
        return self


class AssistantContext(BaseModel):
    """
    Unified, read-only data model encapsulating all context sources.
    Used for reasoning in classifier, planner, and assistant responses.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_context: ConversationContext = Field(default_factory=ConversationContext)
    active_goal_context: GoalContext = Field(default_factory=GoalContext)
    recent_goal_memory: List[str] = Field(default_factory=list, max_length=10)
    relevant_personal_memory: List[str] = Field(default_factory=list, max_length=10)
    verified_system_context: VerifiedSystemContext = Field(default_factory=VerifiedSystemContext)
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_security(self) -> "AssistantContext":
        for mem_list in (self.recent_goal_memory, self.relevant_personal_memory):
            for item in mem_list:
                item_lower = item.lower()
                forbidden = (
                    "universalaction", "axuielement", "applescript", "subprocess", "exec(", "eval(",
                    "coords=", "selector=", "bypass_safety", "approved_bypass", "safety_override", "gate_override"
                )
                for term in forbidden:
                    if term in item_lower:
                        raise ValueError(f"Forbidden security primitive in assistant context item: {term}")
        return self


CONTEXT_REDACT_PATTERNS = [
    re.compile(r"(?:secret|password|passwd|token|bearer|api_key|apikey|private_key|credential)\s*(?:[:=]|\bis\b)\s*\S+", re.IGNORECASE),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
]


def sanitize_context_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    clean = str(text)
    clean = re.sub(r"<[^>]+>", "", clean).strip()
    for pat in CONTEXT_REDACT_PATTERNS:
        clean = pat.sub("[REDACTED_SECRET]", clean)
    return clean[:500]


class AssistantContextBuilder:
    """
    Authoritative, execution-free context assembly component.
    Gathers bounded context from managers without mutating execution or memory states.
    """

    def __init__(
        self,
        context_manager: Optional[GoalContextManager] = None,
        goal_memory_manager: Optional[GoalMemoryManager] = None,
        personal_memory_manager: Optional[Any] = None,
    ):
        self.context_manager = context_manager
        self.goal_memory_manager = goal_memory_manager
        self.personal_memory_manager = personal_memory_manager

    def build_context(
        self,
        session_id: str = "default",
        user_utterance: Optional[str] = None,
        channel: str = "chat",
        max_personal_memories: int = 5,
        max_goal_memories: int = 5,
    ) -> AssistantContext:
        """
        Assemble unified AssistantContext safely across all sources.
        Fails safe on missing or erroring memory sources.
        """
        clean_utterance = sanitize_context_text(user_utterance)
        conv_ctx = ConversationContext(
            session_id=session_id[:64],
            recent_utterance=clean_utterance,
            channel=channel[:20],
        )

        active_ctx = GoalContext()
        if self.context_manager:
            try:
                active_ctx = self.context_manager.get_context(session_id)
            except Exception as e:
                logger.warning(f"Error fetching goal context: {e}")

        goal_mems: List[str] = []
        if self.goal_memory_manager:
            try:
                raw_goal_mems = self.goal_memory_manager.get_memory(session_id)
                for entry in raw_goal_mems[:max_goal_memories]:
                    s = f"[{entry.outcome_state}] {entry.goal_text_summary}"
                    clean_s = sanitize_context_text(s)
                    if clean_s:
                        goal_mems.append(clean_s)
            except Exception as e:
                logger.warning(f"Error fetching goal memory: {e}")

        personal_mems: List[str] = []
        if self.personal_memory_manager and clean_utterance:
            try:
                relevant = self.personal_memory_manager.get_relevant_memories(
                    query=clean_utterance,
                    max_results=max_personal_memories,
                )
                for pm in relevant:
                    s = f"[{pm.category.value}] {pm.content}"
                    clean_s = sanitize_context_text(s)
                    if clean_s:
                        personal_mems.append(clean_s)
            except Exception as e:
                logger.warning(f"Error fetching personal memory: {e}")

        verified_ctx = active_ctx.verified_context

        return AssistantContext(
            conversation_context=conv_ctx,
            active_goal_context=active_ctx,
            recent_goal_memory=goal_mems,
            relevant_personal_memory=personal_mems,
            verified_system_context=verified_ctx,
            created_at=time.time(),
        )


def format_assistant_context_prompt(ctx: AssistantContext) -> str:
    """
    Format AssistantContext safely into delimited XML data for LLM reasoning prompts.
    Strips raw tags from all values to defend against prompt injection.
    """
    lines = ["<assistant_context>"]

    lines.append("<verified_system_context>")
    if ctx.verified_system_context.active_application:
        lines.append(f"active_application: {ctx.verified_system_context.active_application}")
    if ctx.verified_system_context.bundle_id:
        lines.append(f"bundle_id: {ctx.verified_system_context.bundle_id}")
    if ctx.verified_system_context.active_window_summary:
        lines.append(f"active_window_summary: {ctx.verified_system_context.active_window_summary}")
    lines.append("</verified_system_context>")

    lines.append("<active_goal_context>")
    if ctx.active_goal_context.user_context.previous_goal:
        lines.append(f"previous_goal: {ctx.active_goal_context.user_context.previous_goal}")
    if ctx.active_goal_context.user_context.previous_goal_state:
        lines.append(f"previous_goal_state: {ctx.active_goal_context.user_context.previous_goal_state}")
    lines.append("</active_goal_context>")

    if ctx.recent_goal_memory:
        lines.append("<recent_goal_memory>")
        for gm in ctx.recent_goal_memory:
            clean_gm = re.sub(r"<[^>]+>", "", gm).strip()
            lines.append(f"- {clean_gm}")
        lines.append("</recent_goal_memory>")

    if ctx.relevant_personal_memory:
        lines.append("<relevant_personal_memory>")
        for pm in ctx.relevant_personal_memory:
            clean_pm = re.sub(r"<[^>]+>", "", pm).strip()
            lines.append(f"- {clean_pm}")
        lines.append("</relevant_personal_memory>")

    lines.append("<conversation_context>")
    if ctx.conversation_context.recent_utterance:
        clean_utt = re.sub(r"<[^>]+>", "", ctx.conversation_context.recent_utterance).strip()
        lines.append(f"utterance: {clean_utt}")
    lines.append(f"channel: {ctx.conversation_context.channel}")
    lines.append("</conversation_context>")

    lines.append("</assistant_context>")
    return "\n".join(lines)
