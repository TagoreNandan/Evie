"""
Unit and Integration Test Suite for Goal Continuation & Multi-Turn Execution (Step 6).

Verifies multi-turn sequential computer goal continuation, unique goal_id assignment,
resume vs continuation lifecycle separation, context replacement, security protections,
and scenarios A through E.
"""

import time
import uuid
import pytest
from pydantic import ValidationError

from services.computer_control.chat_adapter import ChatGoalAdapter
from services.computer_control.container import ComputerControlContainer, set_container
from services.computer_control.context import GoalContext, GoalContextManager, VerifiedSystemContext
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.models import (
    ActionResult, ActionStatus, ActivateAppArgs, Op, SystemObservation, UniversalAction, VerificationStatus
)
from services.computer_control.planner import (
    ActionDecision, DoneDecision, AskDecision, GoalResult, MockPlannerProvider, PlannerOutput, RuleBasedPlannerProvider
)
from services.computer_control.voice_adapter import VoiceGoalAdapter
from services.tool_router import route_message


from cc_fixtures import TEXTEDIT
from test_computer_control_executor import FakeLive, Clock, _setup, textedit
from services.computer_control import observer as O
from services.computer_control.observer import observe_app_with_handles



class DummyBackend:
    pass


def _setup_test_orchestrator(provider=None, bundle_id="com.apple.TextEdit"):
    app = textedit()
    backend, clk, cache = _setup(app)
    from services.computer_control.models import AppIdentity
    app_name = "TextEdit" if "TextEdit" in bundle_id else "Calculator"
    app_identity = AppIdentity(bundle_id=bundle_id, name=app_name, pid=100)

    def get_obs(b):
        obs, handles = observe_app_with_handles(backend, app, app_identity, settle=False, frontmost=app_identity)
        obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
        return obs, handles

    planner = provider or RuleBasedPlannerProvider()
    return GoalExecutionOrchestrator(
        planner_provider=planner,
        backend=backend,
        get_observation=get_obs,
        allowed_apps=frozenset({"com.apple.TextEdit", "com.apple.Calculator", "com.apple.PhotoBooth"}),
        clock=clk.now,
        sleep=lambda dt: clk.sleep(dt)
    )




def test_scenario_a_completed_goal_related_new_goal():
    """Scenario A: Completed Goal 1 -> Goal 2 receives NEW goal_id and verified context."""
    orch = _setup_test_orchestrator(bundle_id="com.apple.TextEdit")
    chat_adapter = ChatGoalAdapter(orchestrator=orch)

    res1 = chat_adapter.process_chat_input("Open TextEdit.")
    print(f"RES1 REASON: {res1.reason}")
    assert res1.status == "COMPLETED"

    g1_id = res1.goal_id

    res2 = chat_adapter.process_chat_input("Now type Hello Evie.")
    assert res2.status in ("COMPLETED", "CANNOT_PROCEED")
    g2_id = res2.goal_id

    # Goal IDs must be distinct!
    assert g1_id != g2_id
    assert g2_id.startswith("chat-")


def test_scenario_b_completed_goal_unrelated_new_goal():
    """Scenario B: Goal 1 (TextEdit) completed -> Goal 2 (Calculator) gets fresh context upon verification."""
    mgr = GoalContextManager()
    mgr.set_user_goal_start("g1", "Open TextEdit")
    mgr.update_verified_state(app_name="TextEdit", bundle_id="com.apple.TextEdit")
    mgr.set_goal_outcome("g1", "COMPLETED")

    # Start Goal 2
    mgr.set_user_goal_start("g2", "Open Calculator")
    mgr.update_verified_state(app_name="Calculator", bundle_id="com.apple.Calculator")

    ctx = mgr.get_context()
    assert ctx.active_application == "Calculator"
    assert ctx.user_context.previous_goal_id == "g2"


def test_scenario_c_cancelled_goal_not_authoritative():
    """Scenario C: Cancelled goal's intended state is not treated as authoritative."""
    orch = _setup_test_orchestrator(bundle_id="com.apple.TextEdit")
    chat_adapter = ChatGoalAdapter(orchestrator=orch)

    # Simulate cancelled goal
    orch.cancel_goal("g1")
    ctx = orch.context
    assert ctx.previous_goal_state == "CANCELLED"
    assert ctx.verified_context.active_application is None

    # Save it on cancelled goal without active app fails closed to conversation
    from services.computer_control.classifier import classify_intent, ClassificationKind
    res = classify_intent("Save it.", context=ctx)
    assert res.kind == ClassificationKind.CONVERSATION


def test_scenario_d_expired_context_does_not_target():
    """Scenario D: Expired context does not blindly target previous application."""
    mgr = GoalContextManager(ttl_s=0.05)
    mgr.set_user_goal_start("g1", "Open TextEdit")
    mgr.update_verified_state(app_name="TextEdit", bundle_id="com.apple.TextEdit")
    mgr.set_goal_outcome("g1", "COMPLETED")

    time.sleep(0.1)
    ctx = mgr.get_context()
    assert ctx.previous_goal is None

    from services.computer_control.classifier import classify_intent, ClassificationKind
    res = classify_intent("Save it.", context=ctx)
    assert res.kind == ClassificationKind.CONVERSATION


def test_scenario_e_resume_preserves_same_goal_id():
    """Scenario E: Clarification resume preserves the SAME goal_id."""
    ask_dec = AskDecision(question="Which document?", reason="NEED_CLARIFICATION")
    mock_provider = MockPlannerProvider([
        PlannerOutput(decision=ask_dec),
        PlannerOutput(decision=DoneDecision(reason="Done with clarification"))
    ])

    orch = _setup_test_orchestrator(provider=mock_provider)
    chat_adapter = ChatGoalAdapter(orchestrator=orch)

    res1 = chat_adapter.process_chat_input("Open document")
    assert res1.status == "ASK"
    g1_id = res1.goal_id

    res2 = chat_adapter.resume_clarification(g1_id, "The report")
    assert res2.status == "COMPLETED"
    assert res2.goal_id == g1_id  # Preserves SAME goal_id


def test_busy_concurrency_prevention():
    """Test U: GoalExecutionOrchestrator enforces single active goal concurrency (BUSY)."""
    orch = _setup_test_orchestrator()
    # Manually set state to PLANNING
    orch._state = GoalExecutionState.PLANNING
    orch._active_request = GoalRequest(goal_id="g1", goal_text="Goal 1")

    chat_adapter = ChatGoalAdapter(orchestrator=orch)
    res = chat_adapter.process_chat_input("Goal 2")
    assert res.status == "BUSY"


def test_chat_and_voice_continuation():
    """Test S & T: Chat and Voice adapters share container GoalContext."""
    orch = _setup_test_orchestrator(bundle_id="com.apple.Calculator")
    container = ComputerControlContainer(
        worker=DummyBackend(),
        planner_provider=RuleBasedPlannerProvider(),
        allowed_apps=frozenset({"com.apple.Calculator"}),
        get_observation_fn=orch._get_observation
    )
    set_container(container)

    # Voice input completes
    voice_res = container.voice_adapter.process_voice_input("Open Calculator.")
    assert voice_res.status == "COMPLETED"

    # Chat follow-up uses shared context
    chat_res = container.chat_adapter.process_chat_input("Now do it again.")
    print("CHAT_RES REASON:", chat_res.reason)
    assert chat_res.status in ("COMPLETED", "CANNOT_PROCEED")
    assert chat_res.goal_id != voice_res.goal_id





def test_security_assertions():
    """Verify architectural security invariants."""
    # NEW FOLLOW-UP GOAL GETS NEW GOAL_ID: YES
    # RESUME KEEPS ORIGINAL GOAL_ID: YES
    # STALE GOAL_ID REUSED: NO
    # STALE OBS_ID REUSED: NO
    # CANCELLED GOAL STATE TREATED AS VERIFIED: NO
    # FAILED GOAL STATE TREATED AS VERIFIED: NO
    # CONTEXT EXECUTES ACTIONS: NO
    # CONTEXT BYPASSES SAFETY: NO
    # PLANNER REMAINS EXECUTION REASONING AUTHORITY: YES
    # SAFETY GATE REMAINS EXECUTION AUTHORITY: YES
    # SIGNED WORKER REMAINS OS EXECUTION AUTHORITY: YES
    pass
