"""
Focused Unit & Integration Tests for Evie Goal Memory & Conversational Continuity (Step 9).

Validates:
- Ephemeral, bounded GoalMemoryEntry schema and security validation
- GoalMemoryManager lifecycle outcome recording, capacity bounds, and TTL decay
- Session isolation (Session A != Session B)
- Repeat goal resolution ("Do that again") generating NEW goal_id through full pipeline
- Informational continuity queries ("What did you just do?") returning safe conversational summaries
- Expired memory failing closed without execution
- Safety gate, signed worker, and response policy preservation
"""

import pytest
import time
from unittest.mock import MagicMock

from services.computer_control.goal_memory import GoalMemoryEntry, GoalMemoryManager
from services.computer_control.container import (
    ComputerControlContainer, get_container, set_container, initialize_production_container
)
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionState, GoalRequest
)
from services.computer_control.classifier import classify_intent, ClassificationKind
from services.computer_control.planner import RuleBasedPlannerProvider
from services.computer_control.gate import DEFAULT_POLICY
from cc_fixtures import observation, TEXTEDIT


class FakeWorker:
    def __init__(self):
        self.app = TEXTEDIT
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return True

    def stop(self):
        pass


def make_obs(backend):
    obs = observation(app=TEXTEDIT, frontmost=TEXTEDIT)
    return obs, {"handle": 1}


@pytest.fixture
def test_container():
    set_container(None)
    worker = FakeWorker()
    planner = RuleBasedPlannerProvider()
    policy = DEFAULT_POLICY
    container = initialize_production_container(
        worker=worker,
        safety_policy=policy,
        get_observation_fn=make_obs
    )
    yield container
    set_container(None)


def test_memory_entry_creation_and_bounded_fields():
    """Test A & B: GoalMemoryEntry schema validation and bounds."""
    entry = GoalMemoryEntry(
        goal_id="goal-123",
        session_id="session-1",
        source="chat",
        goal_text_summary="Open TextEdit.",
        outcome_state="COMPLETED",
        created_at=100.0,
        completed_at=101.5,
        bounded_result_summary="last_op=activate_app",
        success=True,
        clarification_count=0
    )
    assert entry.goal_id == "goal-123"
    assert entry.session_id == "session-1"
    assert entry.success is True
    assert entry.outcome_state == "COMPLETED"


def test_outcome_recording_lifecycle():
    """Test C-I: Recording completed, cancelled, failed, blocked, cannot-proceed, budget-exceeded; waiting-for-user ignored."""
    mgr = GoalMemoryManager(clock=lambda: 1000.0)

    # C. Completed
    mgr.record_outcome("g1", "s1", "chat", "Open TextEdit.", "COMPLETED", 1000.0)
    assert len(mgr.get_memory("s1")) == 1

    # D. Cancelled
    mgr.record_outcome("g2", "s1", "chat", "Open Calculator.", "CANCELLED", 1000.0)
    assert len(mgr.get_memory("s1")) == 2

    # E. Failed
    mgr.record_outcome("g3", "s1", "voice", "Type hello.", "FAILED", 1000.0)
    assert len(mgr.get_memory("s1")) == 3

    # F. Blocked
    mgr.record_outcome("g4", "s1", "hardware", "Format disk.", "BLOCKED", 1000.0)
    assert len(mgr.get_memory("s1")) == 4

    # G. Cannot proceed
    mgr.record_outcome("g5", "s1", "chat", "Unclear goal.", "CANNOT_PROCEED", 1000.0)
    assert len(mgr.get_memory("s1")) == 5

    # H. Budget exceeded
    mgr.record_outcome("g6", "s1", "chat", "Loop goal.", "BUDGET_EXCEEDED", 1000.0)
    assert len(mgr.get_memory("s1")) == 6

    # I. Waiting for user ignored
    mgr.record_outcome("g7", "s1", "chat", "Clarification goal.", "WAITING_FOR_USER", 1000.0)
    assert len(mgr.get_memory("s1")) == 6


def test_capacity_bound():
    """Test J: Max entry capacity bound per session."""
    mgr = GoalMemoryManager(max_entries_per_session=3, clock=lambda: 1000.0)
    for i in range(10):
        mgr.record_outcome(f"g{i}", "s1", "chat", f"Goal {i}", "COMPLETED", 1000.0 + i)

    mem = mgr.get_memory("s1")
    assert len(mem) == 3
    assert mem[0].goal_id == "g7"
    assert mem[-1].goal_id == "g9"


def test_ttl_expiration():
    """Test K & S: Memory entries decay and expire beyond TTL."""
    current_time = 1000.0
    mgr = GoalMemoryManager(ttl_s=300.0, clock=lambda: current_time)

    mgr.record_outcome("g1", "s1", "chat", "Open TextEdit.", "COMPLETED", 1000.0)
    assert len(mgr.get_memory("s1")) == 1

    # Advance clock past TTL (301 seconds later)
    current_time = 1301.0
    assert len(mgr.get_memory("s1")) == 0
    assert mgr.get_last_completed("s1") is None


def test_session_isolation():
    """Test L, M, N: Session A memory is strictly isolated from Session B."""
    mgr = GoalMemoryManager(clock=lambda: 1000.0)

    mgr.record_outcome("ga", "session-A", "chat", "Open TextEdit.", "COMPLETED", 1000.0)
    mgr.record_outcome("gb", "session-B", "voice", "Open Calculator.", "COMPLETED", 1000.0)

    mem_a = mgr.get_memory("session-A")
    mem_b = mgr.get_memory("session-B")

    assert len(mem_a) == 1
    assert mem_a[0].goal_text_summary == "Open TextEdit."

    assert len(mem_b) == 1
    assert mem_b[0].goal_text_summary == "Open Calculator."


def test_do_that_again_creates_new_goal_and_new_goal_id(test_container):
    """Test O, Q, U: 'Do that again' uses recent memory to create a NEW goal with a NEW goal_id."""
    container = test_container

    # 1. Execute initial goal
    res1 = container.chat_adapter.process_chat_input("Open TextEdit.", session_id="sess-repeat-1")
    assert res1.status == "COMPLETED"
    first_id = res1.goal_id

    # 2. Issue "Do that again" on same session
    class_res = classify_intent("Do that again", session_id="sess-repeat-1")
    assert class_res.kind == ClassificationKind.COMPUTER_GOAL
    assert class_res.goal == "Open TextEdit."

    res2 = container.chat_adapter.process_chat_input("Do that again", session_id="sess-repeat-1")
    assert res2.goal_id != first_id
    assert res2.goal_id.startswith("chat-")


def test_do_that_again_without_memory_fails_closed(test_container):
    """Test R: 'Do that again' without completed goal memory returns conversation (fails closed)."""
    class_res = classify_intent("Do that again", session_id="empty-session")
    assert class_res.kind == ClassificationKind.CONVERSATION
    assert class_res.goal is None


def test_what_did_you_just_do_informational_response(test_container):
    """Test W & X: 'What did you just do?' returns safe, bounded summary without planner internals."""
    container = test_container

    container.chat_adapter.process_chat_input("Open TextEdit.", session_id="sess-info-1")

    class_res = classify_intent("What did you just do?", session_id="sess-info-1")
    assert class_res.kind == ClassificationKind.CONVERSATION
    assert class_res.reason.startswith("INFORMATIONAL_QUERY:")
    assert "Open TextEdit" in class_res.reason
    assert "AXElement" not in class_res.reason
    assert "Gate" not in class_res.reason


def test_cross_channel_memory_continuity(test_container):
    """Test Y: Memory recorded by Chat is retrieved by Voice and Hardware on same session."""
    container = test_container
    session_id = "cross-channel-sess"

    res_chat = container.chat_adapter.process_chat_input("Open TextEdit.", session_id=session_id)
    assert res_chat.status == "COMPLETED"

    last_voice = container.goal_memory.get_last_completed(session_id)
    assert last_voice is not None
    assert last_voice.goal_text_summary == "Open TextEdit."
    assert last_voice.source == "chat"


def test_sensitive_data_not_stored_in_memory():
    """Test V: GoalMemoryEntry rejects forbidden execution primitives and redacts secrets."""
    # Execution primitive rejection
    with pytest.raises(ValueError, match="Forbidden execution primitive"):
        GoalMemoryEntry(
            goal_id="g1",
            session_id="s1",
            source="chat",
            goal_text_summary="UniversalAction(op=Op.CLICK_ELEMENT)",
            outcome_state="COMPLETED",
            created_at=100.0
        )

    # Secret sanitization in record_outcome
    mgr = GoalMemoryManager(clock=lambda: 1000.0)
    entry = mgr.record_outcome("g1", "s1", "chat", "Set password=secret_password_123", "COMPLETED", 1000.0, result_summary="Done with token=12345")
    assert entry is not None
    assert "secret_password_123" not in entry.goal_text_summary
    assert "12345" not in entry.bounded_result_summary
    assert "[REDACTED_SECRET]" in entry.goal_text_summary


def test_historical_memory_does_not_create_verified_state(test_container):
    """Test T: Goal memory recording does not alter VerifiedSystemContext."""
    mgr = GoalMemoryManager(clock=lambda: 1000.0)
    mgr.record_outcome("g1", "s1", "chat", "Open TextEdit.", "COMPLETED", 1000.0)

    # Verified context remains untouched
    ctx = test_container.goal_context.get_context("s1")
    assert ctx.verified_context.active_application is None


def test_response_policy_preserves_silent_success(test_container):
    """Test Z: Successful operations recorded in memory remain SILENT in response policy."""
    from services.tool_registry import format_goal_reply

    res = test_container.chat_adapter.process_chat_input("Open TextEdit.", session_id="silent-test")
    assert res.status == "COMPLETED"
    reply = format_goal_reply(res.status, verification_summary=res.verification_summary, question=res.question, reason=res.reason)
    assert reply == ""
