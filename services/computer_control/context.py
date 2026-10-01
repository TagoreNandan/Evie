"""
Ephemeral, Bounded Goal Context for Evie Computer Control (Step 5).

Provides a small, typed context layer allowing natural follow-up computer goals
to refer safely to immediately relevant prior task and execution context.

Security & Architecture Invariants:
1. Context is strictly EPHEMERAL and in-memory (session-bounded, TTL-decayed).
2. Explicitly distinguishes untrusted USER_CONTEXT from authoritative VERIFIED_SYSTEM_CONTEXT.
3. User input can NEVER populate or mutate VERIFIED_SYSTEM_CONTEXT.
4. Stores NO screenshots, passwords, credentials, IPC tokens, raw AX pointers, coordinates,
   selectors, arbitrary UI dumps, or raw clipboard contents.
5. Performs NO action execution, NO target selection, and NO Safety Gate bypasses.
6. Strictly bounded size (max goal text lengths, max entries, extra="forbid").
"""

import time
from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerifiedSystemContext(BaseModel):
    """Authoritative system state populated ONLY from verified execution & observation."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_application: Optional[str] = Field(None, max_length=100)
    bundle_id: Optional[str] = Field(None, max_length=100)
    active_window_summary: Optional[str] = Field(None, max_length=200)
    last_verified_op: Optional[str] = Field(None, max_length=50)
    verified_at: Optional[float] = None

    @model_validator(mode="after")
    def validate_verified_security(self) -> "VerifiedSystemContext":
        for val in (self.active_application, self.bundle_id, self.active_window_summary, self.last_verified_op):
            if val:
                val_lower = val.lower()
                forbidden = (
                    "universalaction", "axuielement", "applescript", "subprocess", "exec(", "eval(",
                    "coords=", "selector=", "bypass_safety", "approved_bypass", "safety_override", "gate_override"
                )
                for term in forbidden:
                    if term in val_lower:
                        raise ValueError(f"Forbidden security primitive in verified context: {term}")
        return self


class UserContext(BaseModel):
    """Untrusted context derived from prior natural-language user requests."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_goal: Optional[str] = Field(None, max_length=500)
    previous_goal_id: Optional[str] = Field(None, max_length=64)
    previous_goal_state: Optional[str] = Field(None, max_length=50)
    recent_interaction: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def validate_user_context_security(self) -> "UserContext":
        for val in (self.previous_goal, self.previous_goal_id, self.previous_goal_state, self.recent_interaction):
            if val:
                val_lower = val.lower()
                forbidden = (
                    "universalaction", "axuielement", "applescript", "subprocess", "exec(", "eval(",
                    "coords=", "selector=", "bypass_safety", "approved_bypass", "safety_override", "gate_override"
                )
                for term in forbidden:
                    if term in val_lower:
                        raise ValueError(f"Forbidden security primitive in user context: {term}")
        return self


class GoalContext(BaseModel):
    """
    Closed, bounded goal context container passed to classifier and planner.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_context: UserContext = Field(default_factory=UserContext)
    verified_context: VerifiedSystemContext = Field(default_factory=VerifiedSystemContext)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


    @property
    def previous_goal(self) -> Optional[str]:
        return self.user_context.previous_goal

    @property
    def previous_goal_state(self) -> Optional[str]:
        return self.user_context.previous_goal_state

    @property
    def active_application(self) -> Optional[str]:
        return self.verified_context.active_application or self.verified_context.bundle_id


class GoalContextManager:
    """
    In-memory, session-bounded context manager enforcing ephemerality and TTL decay.
    """

    def __init__(self, ttl_s: float = 300.0):
        self.ttl_s = ttl_s
        self._contexts: Dict[str, GoalContext] = {}

    def get_context(self, session_id: str = "default_session") -> GoalContext:
        """Returns active context if within TTL limit, otherwise fresh context."""
        now = time.time()
        curr = self._contexts.get(session_id)
        if curr is None or (now - curr.updated_at > self.ttl_s):
            curr = GoalContext()
            self._contexts[session_id] = curr
        return curr

    def set_user_goal_start(self, goal_id: str, goal_text: str, session_id: str = "default_session") -> None:
        """Updates context when a new goal begins."""
        curr = self.get_context(session_id)
        new_user = UserContext(
            previous_goal=goal_text[:500],
            previous_goal_id=goal_id[:64],
            previous_goal_state="STARTED",
            recent_interaction=goal_text[:300]
        )
        self._contexts[session_id] = GoalContext(
            user_context=new_user,
            verified_context=curr.verified_context,
            created_at=curr.created_at,
            updated_at=time.time()
        )

    def update_verified_state(
        self,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
        window_title: Optional[str] = None,
        last_op: Optional[str] = None,
        session_id: str = "default_session"
    ) -> None:
        """Updates authoritative verified system context after independent observation/verification."""
        curr = self.get_context(session_id)
        new_ver = VerifiedSystemContext(
            active_application=app_name[:100] if app_name else curr.verified_context.active_application,
            bundle_id=bundle_id[:100] if bundle_id else curr.verified_context.bundle_id,
            active_window_summary=window_title[:200] if window_title else curr.verified_context.active_window_summary,
            last_verified_op=last_op[:50] if last_op else curr.verified_context.last_verified_op,
            verified_at=time.time()
        )
        self._contexts[session_id] = GoalContext(
            user_context=curr.user_context,
            verified_context=new_ver,
            created_at=curr.created_at,
            updated_at=time.time()
        )

    def set_goal_outcome(self, goal_id: str, status: str, reason: Optional[str] = None, session_id: str = "default_session") -> None:
        """Updates context outcome state upon goal termination."""
        curr = self.get_context(session_id)
        new_ver = curr.verified_context
        if status in ("CANCELLED", "FAILED", "BLOCKED", "BUDGET_EXCEEDED"):
            new_ver = VerifiedSystemContext(
                active_application=None,
                bundle_id=None,
                active_window_summary=None,
                last_verified_op=None,
                verified_at=None
            )

        new_user = UserContext(
            previous_goal=curr.user_context.previous_goal,
            previous_goal_id=goal_id[:64],
            previous_goal_state=status[:50],
            recent_interaction=f"Goal {goal_id} ended with state {status}"[:300]
        )
        self._contexts[session_id] = GoalContext(
            user_context=new_user,
            verified_context=new_ver,
            created_at=curr.created_at,
            updated_at=time.time()
        )

    def clear(self, session_id: Optional[str] = None) -> None:
        """Explicitly resets context state."""
        if session_id:
            self._contexts.pop(session_id, None)
        else:
            self._contexts.clear()
