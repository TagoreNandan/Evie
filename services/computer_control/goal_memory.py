"""
Bounded, Ephemeral Goal Memory for Evie Computer Control (Step 9).

Provides short-term conversational continuity and history awareness for computer goals
without creating unrestricted permanent memory or compromising security invariants.

Security & Architecture Invariants:
1. Strictly SHORT-TERM, EPHEMERAL, and IN-MEMORY (session-scoped, TTL-decayed).
2. Stores NO screenshots, raw AX trees, raw AX pointers, coordinates, selectors,
   credentials, secrets, API keys, clipboard contents, secure-field values, shell commands,
   AppleScript, executable paths, UniversalAction objects, or hidden planner reasoning.
3. Observational & contextual ONLY — NEVER an execution authority.
4. "Do that again" resolves to a NEW goal request with a NEW goal_id, passing through
   the full pipeline (Observe -> Plan -> Safety Gate -> Signed Worker -> Verify).
   NEVER replays old actions directly.
5. Keyed by session_id. Unrelated sessions NEVER share Goal Memory.
"""

import re
import time
from typing import Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field, model_validator


REDACT_PATTERNS = [
    re.compile(r"(?:secret|password|token|bearer|api_key|private_key|credential)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
]


def sanitize_memory_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    clean = str(text)
    for pat in REDACT_PATTERNS:
        clean = pat.sub("[REDACTED_SECRET]", clean)
    return clean[:500]


class GoalMemoryEntry(BaseModel):
    """Bounded, typed model representing a completed or terminated historical user goal."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_id: str = Field(..., min_length=1, max_length=64)
    session_id: str = Field(..., min_length=1, max_length=64)
    source: str = Field(..., min_length=1, max_length=64)
    goal_text_summary: str = Field(..., min_length=1, max_length=500)
    outcome_state: str = Field(..., min_length=1, max_length=50)
    created_at: float = Field(..., ge=0.0)
    completed_at: Optional[float] = Field(None, ge=0.0)
    bounded_result_summary: Optional[str] = Field(None, max_length=500)
    success: bool = False
    clarification_count: int = Field(0, ge=0)

    @model_validator(mode="after")
    def validate_memory_security(self) -> "GoalMemoryEntry":
        """Strict validator preventing execution primitive storage in GoalMemoryEntry."""
        for text in (self.goal_text_summary, self.bounded_result_summary):
            if text:
                text_lower = text.lower()
                forbidden = (
                    "universalaction", "actionrequest", "axuielement", "applescript",
                    "subprocess", "exec(", "eval(", "coords=", "selector=",
                    "bypass_safety", "safety_override"
                )
                for term in forbidden:
                    if term in text_lower:
                        raise ValueError(f"Forbidden execution primitive in GoalMemoryEntry: {term}")
        return self


class GoalMemoryManager:
    """
    In-memory, session-bounded Goal Memory manager enforcing capacity limits and TTL decay.
    """

    def __init__(
        self,
        max_entries_per_session: int = 20,
        ttl_s: float = 600.0,
        clock: Callable[[], float] = time.time
    ):
        self.max_entries_per_session = max_entries_per_session
        self.ttl_s = ttl_s
        self.clock = clock
        self._store: Dict[str, List[GoalMemoryEntry]] = {}

    def record_outcome(
        self,
        goal_id: str,
        session_id: str,
        source: str,
        goal_text: str,
        outcome_state: str,
        created_at: float,
        completed_at: Optional[float] = None,
        result_summary: Optional[str] = None,
        clarification_count: int = 0
    ) -> Optional[GoalMemoryEntry]:
        """
        Records a completed or terminal goal outcome into session memory.
        Ignores non-terminal states such as WAITING_FOR_USER.
        """
        if outcome_state == "WAITING_FOR_USER":
            return None

        now = self.clock()
        entries = self._prune_and_get(session_id, now)

        clean_text = sanitize_memory_text(goal_text.strip()) or "Computer Goal"
        clean_result = sanitize_memory_text(result_summary) if result_summary else None

        entry = GoalMemoryEntry(
            goal_id=goal_id[:64],
            session_id=session_id[:64],
            source=source[:64],
            goal_text_summary=clean_text[:500],
            outcome_state=outcome_state[:50],
            created_at=created_at,
            completed_at=completed_at or now,
            bounded_result_summary=clean_result[:500] if clean_result else None,
            success=(outcome_state == "COMPLETED"),
            clarification_count=clarification_count
        )

        entries.append(entry)
        if len(entries) > self.max_entries_per_session:
            entries = entries[-self.max_entries_per_session:]
        self._store[session_id] = entries
        return entry

    def get_memory(self, session_id: str = "default_session") -> Tuple[GoalMemoryEntry, ...]:
        """Returns active, non-expired memory entries for a session."""
        now = self.clock()
        entries = self._prune_and_get(session_id, now)
        return tuple(entries)

    def get_last_completed(self, session_id: str = "default_session") -> Optional[GoalMemoryEntry]:
        """Returns the most recent successfully completed goal entry for a session."""
        entries = self.get_memory(session_id)
        for e in reversed(entries):
            if e.success:
                return e
        return None

    def get_last_goal(self, session_id: str = "default_session") -> Optional[GoalMemoryEntry]:
        """Returns the most recent recorded goal entry (any terminal state) for a session."""
        entries = self.get_memory(session_id)
        if entries:
            return entries[-1]
        return None

    def _prune_and_get(self, session_id: str, now: float) -> List[GoalMemoryEntry]:
        raw = self._store.get(session_id, [])
        valid = [e for e in raw if (now - e.created_at) <= self.ttl_s]
        self._store[session_id] = valid
        return valid

    def clear(self, session_id: Optional[str] = None) -> None:
        """Clears memory for a specific session or all sessions."""
        if session_id:
            self._store.pop(session_id, None)
        else:
            self._store.clear()
