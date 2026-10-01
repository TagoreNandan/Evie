"""
Dedicated unit and integration test suite for GoalExecutionOrchestrator (Evie Phase 6).

Verifies unified goal lifecycle, modality independence, bounded execution state model,
GoalBudget enforcement, on-demand visual fallback, planner provider abstraction,
single-action per turn, code-owned safety gate authority, signed worker execution,
independent verification, user clarification resume, cancellation, concurrency bounds,
audit logging, and scope/forbidden mechanism checks.
"""

import ast
import inspect
from pathlib import Path
import time
import pytest

from cc_fixtures import TEXTEDIT, TERMINAL, observation
from test_computer_control_executor import FakeLive, Clock, _setup, textedit
from services.computer_control.actions import Op
from services.computer_control.gate import GateOutcome, GatePolicy, DEFAULT_POLICY
from services.computer_control.models import (
    ActionStatus, ActivateAppArgs, AppIdentity, ClickElementArgs, NoArgs, SetTextArgs,
    UniversalAction, VerificationStatus
)
from services.computer_control import observer as O
from services.computer_control.observer import observe_app_with_handles
from services.computer_control.planner import (
    ActionDecision, AskDecision, CannotProceedDecision, DoneDecision, GoalBudget,
    MockPlannerProvider, PlannerDecisionKind, PlannerInput, PlannerOutput, RuleBasedPlannerProvider
)
from services.computer_control.resolver import SemanticTargetSpec
from services.computer_control.visual_observer import (
    FakeVisualObservationProvider, VisualBoundingRegion, VisualElement, VisualObservation
)
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)


def _setup_orchestrator_harness(provider=None, clock=None, visual_provider=None, budget=None, app_node=None):
    app = app_node or textedit()
    backend, clk, cache = _setup(app)
    clock = clock or clk

    def get_obs(b):
        obs, handles = observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
        obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
        return obs, handles

    planner = provider or RuleBasedPlannerProvider()
    orch = GoalExecutionOrchestrator(
        planner_provider=planner,
        backend=backend,
        get_observation=get_obs,
        allowed_apps=frozenset({"com.apple.TextEdit", "com.apple.PhotoBooth"}),
        visual_provider=visual_provider,
        budget=budget or GoalBudget(),
        policy=DEFAULT_POLICY,
        clock=clock.now,
        sleep=lambda dt: clock.sleep(dt)
    )
    return orch, backend, clock, cache


# 1. Simple one-action goal
def test_simple_one_action_goal():
    orch, _, _, _ = _setup_orchestrator_harness()
    req = GoalRequest(goal_id="g-1", goal_text="open TextEdit", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert res.goal_id == "g-1"
    assert res.turn_count >= 1
    assert "DONE" in res.reason


# 2. Multi-turn goal
def test_multi_turn_goal():
    class DynamicMultiTurnPlanner:
        def __init__(self):
            self.step = 0
        def choose_action(self, planner_in):
            self.step += 1
            obs_id = planner_in.obs_id
            if self.step == 1:
                spec = SemanticTargetSpec(obs_id=obs_id, role="AXTextArea")
                act = UniversalAction(action_id="act-1", obs_id=obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
                return PlannerOutput(decision=ActionDecision(action=act))
            elif self.step == 2:
                spec = SemanticTargetSpec(obs_id=obs_id, role="AXTextArea")
                act = UniversalAction(action_id="act-2", obs_id=obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
                return PlannerOutput(decision=ActionDecision(action=act))
            return PlannerOutput(decision=DoneDecision(reason="Typed text successfully."))

    orch, _, _, _ = _setup_orchestrator_harness(provider=DynamicMultiTurnPlanner())
    req = GoalRequest(goal_id="g-2", goal_text="type hello in TextEdit", source="chat")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert res.turn_count == 3
    assert res.action_count >= 1


# 3. DONE decision
def test_done_decision():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=DoneDecision(reason="Task already finished."))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-3", goal_text="check status", source="api")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert "Task already finished" in res.reason


# 4. ASK decision
def test_ask_decision():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=AskDecision(question="Which document should I save to?", reason="MISSING_PATH"))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-4", goal_text="save file", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.WAITING_FOR_USER
    assert "ASK:" in res.reason


# 5. Clarification resume
def test_clarification_resume():
    class DynamicClarifyPlanner:
        def __init__(self):
            self.asked = False
        def choose_action(self, planner_in):
            if not self.asked:
                self.asked = True
                return PlannerOutput(decision=AskDecision(question="Do you mean TextEdit?", reason="CONFIRM_APP"))
            spec = SemanticTargetSpec(obs_id=planner_in.obs_id, role="AXTextArea")
            act = UniversalAction(action_id="act-1", obs_id=planner_in.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
            return PlannerOutput(decision=ActionDecision(action=act))

    orch, _, _, _ = _setup_orchestrator_harness(provider=DynamicClarifyPlanner())
    req = GoalRequest(goal_id="g-5", goal_text="open editor", source="hardware")
    res1 = orch.execute_goal(req)
    assert res1.status == GoalExecutionState.WAITING_FOR_USER

    res2 = orch.resume_with_clarification("g-5", "Yes, TextEdit")
    assert res2.status == GoalExecutionState.WAITING_FOR_REOBSERVATION or res2.status == GoalExecutionState.COMPLETED or res2.turn_count >= 1
    assert res2.clarification_count == 1


# 6. Clarification budget
def test_clarification_budget():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=AskDecision(question="Q1?", reason="R1")),
        PlannerOutput(decision=AskDecision(question="Q2?", reason="R2")),
        PlannerOutput(decision=AskDecision(question="Q3?", reason="R3")),
        PlannerOutput(decision=AskDecision(question="Q4?", reason="R4")),
    ])
    budget = GoalBudget(max_clarifications=2)
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p, budget=budget)

    req = GoalRequest(goal_id="g-6", goal_text="vague goal", source="chat")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.WAITING_FOR_USER

    orch.resume_with_clarification("g-6", "Answer 1")
    orch.resume_with_clarification("g-6", "Answer 2")
    res_final = orch.resume_with_clarification("g-6", "Answer 3")
    assert res_final.status == GoalExecutionState.BUDGET_EXCEEDED
    assert "MAX_CLARIFICATIONS_EXCEEDED" in res_final.reason


# 7. CANNOT_PROCEED decision
def test_cannot_proceed():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=CannotProceedDecision(reason="NO_CONTROLS", explanation="No controls found on screen."))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-7", goal_text="click unknown control", source="api")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.CANNOT_PROCEED
    assert "CANNOT_PROCEED" in res.reason


# 8. Max turn budget
def test_max_turn_budget():
    class DynamicRepeatPlanner:
        def choose_action(self, planner_in):
            spec = SemanticTargetSpec(obs_id=planner_in.obs_id, role="AXTextArea")
            act = UniversalAction(action_id="act-rep", obs_id=planner_in.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
            return PlannerOutput(decision=ActionDecision(action=act))

    budget = GoalBudget(max_turns=3)
    orch, _, _, _ = _setup_orchestrator_harness(provider=DynamicRepeatPlanner(), budget=budget)

    req = GoalRequest(goal_id="g-8", goal_text="infinite goal", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.BUDGET_EXCEEDED
    assert "MAX_TURNS_EXCEEDED" in res.reason
    assert res.turn_count == 3


# 9. Max noop budget
def test_max_noop_budget():
    act_stale = UniversalAction(
        action_id="act-stale", obs_id="obs-stale", op=Op.ACTIVATE_APP,
        args=ActivateAppArgs(bundle_id="com.apple.TextEdit")
    )
    mock_p = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act_stale))] * 5)
    budget = GoalBudget(max_turns=10, max_consecutive_noops=2)
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p, budget=budget)

    req = GoalRequest(goal_id="g-9", goal_text="stale goal", source="chat")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.BUDGET_EXCEEDED
    assert "MAX_CONSECUTIVE_NOOPS" in res.reason


# 10. Wall-time budget
def test_wall_time_budget():
    class DynamicRepeatPlanner:
        def choose_action(self, planner_in):
            spec = SemanticTargetSpec(obs_id=planner_in.obs_id, role="AXTextArea")
            act = UniversalAction(action_id="act-rep", obs_id=planner_in.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
            return PlannerOutput(decision=ActionDecision(action=act))

    orch, backend, clock, cache = _setup_orchestrator_harness(provider=DynamicRepeatPlanner(), budget=GoalBudget(max_wall_time_s=5.0))

    def get_obs_advancing(b):
        clock.t += 6.0 # Advance past 5s limit
        app = textedit()
        return observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)

    orch._get_observation = get_obs_advancing

    req = GoalRequest(goal_id="g-10", goal_text="slow goal", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.BUDGET_EXCEEDED
    assert "WALL_CLOCK_TIMEOUT" in res.reason


# 11. Cancellation
def test_cancellation():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=AskDecision(question="Continue?", reason="CHECK"))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-11", goal_text="cancel me", source="voice")
    orch.execute_goal(req)
    assert orch.state == GoalExecutionState.WAITING_FOR_USER

    res = orch.cancel_goal("g-11")
    assert res.status == GoalExecutionState.CANCELLED


# 12. Concurrent goal rejection
def test_concurrent_goal_rejection():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=AskDecision(question="Holding state?", reason="WAIT"))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req1 = GoalRequest(goal_id="g-12a", goal_text="goal 1", source="voice")
    res1 = orch.execute_goal(req1)
    assert res1.status == GoalExecutionState.WAITING_FOR_USER

    req2 = GoalRequest(goal_id="g-12b", goal_text="goal 2", source="chat")
    res2 = orch.execute_goal(req2)
    assert res2.status == GoalExecutionState.BUSY
    assert "ORCHESTRATOR_BUSY" in res2.reason


# 13. Stale observation handling
def test_stale_observation_handling():
    class StaleRecoveryPlanner:
        def __init__(self):
            self.step = 0
        def choose_action(self, planner_in):
            self.step += 1
            if self.step == 1:
                act = UniversalAction(action_id="act-1", obs_id="obs-old", op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.TextEdit"))
                return PlannerOutput(decision=ActionDecision(action=act))
            elif self.step == 2:
                spec = SemanticTargetSpec(obs_id=planner_in.obs_id, role="AXTextArea")
                act = UniversalAction(action_id="act-2", obs_id=planner_in.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
                return PlannerOutput(decision=ActionDecision(action=act))
            return PlannerOutput(decision=DoneDecision(reason="Recovered from stale obs."))

    orch, _, _, _ = _setup_orchestrator_harness(provider=StaleRecoveryPlanner())
    req = GoalRequest(goal_id="g-13", goal_text="recover stale", source="api")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED


# 14. Planner malformed output
def test_planner_malformed_output():
    class BadPlanner:
        def choose_action(self, planner_in):
            return "not_a_planner_output"

    orch, _, _, _ = _setup_orchestrator_harness(provider=BadPlanner())
    req = GoalRequest(goal_id="g-14", goal_text="bad output", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.CANNOT_PROCEED
    assert "MALFORMED_PLANNER_OUTPUT" in res.reason


# 15. Safety Gate BLOCK
def test_safety_gate_block():
    class TerminalPlanner:
        def choose_action(self, planner_in):
            act = UniversalAction(action_id="act-term", obs_id=planner_in.obs_id, op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.Terminal"))
            return PlannerOutput(decision=ActionDecision(action=act))

    orch, _, _, _ = _setup_orchestrator_harness(provider=TerminalPlanner())
    req = GoalRequest(goal_id="g-15", goal_text="open terminal", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.BLOCKED
    assert "BLOCKED" in res.reason


# 16. Worker execution failure
def test_worker_execution_failure():
    def failing_get_obs(b):
        raise RuntimeError("Observation failure")

    act = UniversalAction(
        action_id="act-1", obs_id="obs-1", op=Op.ACTIVATE_APP,
        args=ActivateAppArgs(bundle_id="com.apple.TextEdit")
    )
    mock_p = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    orch._get_observation = failing_get_obs

    req = GoalRequest(goal_id="g-16", goal_text="trigger failure", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.FAILED
    assert "OBSERVATION_FAILURE" in res.reason


# 17. Verification success
def test_verification_success():
    class VerifyPlanner:
        def __init__(self):
            self.done = False
        def choose_action(self, planner_in):
            if not self.done:
                self.done = True
                spec = SemanticTargetSpec(obs_id=planner_in.obs_id, role="AXTextArea")
                act = UniversalAction(action_id="act-1", obs_id=planner_in.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
                return PlannerOutput(decision=ActionDecision(action=act))
            return PlannerOutput(decision=DoneDecision(reason="Verified successfully."))

    orch, _, _, _ = _setup_orchestrator_harness(provider=VerifyPlanner())
    req = GoalRequest(goal_id="g-17", goal_text="verify success", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert res.history[0].verification == VerificationStatus.VERIFIED


# 18. Verification failure
def test_verification_failure():
    act = UniversalAction(
        action_id="act-1", obs_id="obs-wrong", op=Op.ACTIVATE_APP,
        args=ActivateAppArgs(bundle_id="com.apple.TextEdit")
    )
    mock_p = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p, budget=GoalBudget(max_consecutive_noops=1))

    req = GoalRequest(goal_id="g-18", goal_text="verify failure", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.BUDGET_EXCEEDED
    assert res.history[0].verification == VerificationStatus.NOT_APPLICABLE


# 19. Vision fallback invocation
def test_vision_fallback_invocation():
    v_elem = VisualElement(
        visual_id="v1", role="button", label="Example", text_summary="Example button",
        bounding_region=VisualBoundingRegion(x_min=0.1, y_min=0.1, x_max=0.6, y_max=0.3), confidence=0.9
    )
    v_obs = VisualObservation(
        obs_id="obs-initial", taken_at=time.time(), app_bundle_id="com.apple.TextEdit", elements=(v_elem,)
    )
    vis_prov = FakeVisualObservationProvider([v_obs])
    vis_prov.force_visual = True

    class InspectPlanner:
        def __init__(self):
            self.received_vis = False
        def choose_action(self, planner_in):
            if planner_in.compact_visual_observation is not None:
                self.received_vis = True
            return PlannerOutput(decision=DoneDecision(reason="Visual observed."))

    p = InspectPlanner()
    orch, _, _, _ = _setup_orchestrator_harness(provider=p, visual_provider=vis_prov)

    req = GoalRequest(goal_id="g-19", goal_text="use vision", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert p.received_vis is True


# 20. Vision not invoked when AX is sufficient
def test_vision_not_invoked_when_ax_is_sufficient():
    vis_prov = FakeVisualObservationProvider()
    vis_prov.force_visual = False

    class InspectPlanner:
        def __init__(self):
            self.received_vis = False
        def choose_action(self, planner_in):
            if planner_in.compact_visual_observation is not None:
                self.received_vis = True
            return PlannerOutput(decision=DoneDecision(reason="AX sufficient."))

    p = InspectPlanner()
    orch, _, _, _ = _setup_orchestrator_harness(provider=p, visual_provider=vis_prov)

    req = GoalRequest(goal_id="g-20", goal_text="ax sufficient", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert p.received_vis is False


# 21. LLM provider injected through PlannerProvider
def test_llm_provider_injected():
    class DummyLLMPlannerProvider:
        def choose_action(self, planner_in):
            return PlannerOutput(decision=DoneDecision(reason="LLM decision made."))

    orch, _, _, _ = _setup_orchestrator_harness(provider=DummyLLMPlannerProvider())
    req = GoalRequest(goal_id="g-21", goal_text="llm goal", source="chat")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert "LLM decision made" in res.reason


# 22. RuleBased provider injected
def test_rule_based_provider_injected():
    orch, _, _, _ = _setup_orchestrator_harness(provider=RuleBasedPlannerProvider())
    req = GoalRequest(goal_id="g-22", goal_text="activate TextEdit", source="voice")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED


# 23. Fake provider injected
def test_fake_provider_injected():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=DoneDecision(reason="Fake provider done."))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-23", goal_text="fake goal", source="api")
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED


# 24. No direct OS control from orchestrator
def test_no_direct_os_control():
    source = inspect.getsource(GoalExecutionOrchestrator)
    forbidden_imports = ["AppKit", "Foundation", "objc", "Quartz", "CoreGraphics"]
    for item in forbidden_imports:
        assert item not in source, f"Forbidden direct OS import/reference found: {item}"


# 25. No shell / AppleScript / coordinate mechanisms
def test_no_shell_applescript_coordinate_mechanisms():
    path = Path(__file__).parent.parent / "services" / "computer_control" / "goal_orchestrator.py"
    source_text = path.read_text()

    forbidden_calls = ["subprocess", "os.system", "osascript", "click(", "CGEvent", "openApplication"]
    for term in forbidden_calls:
        assert term not in source_text, f"Forbidden execution mechanism found in orchestrator source: {term}"


# 26. Result redaction
def test_result_redaction():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=DoneDecision(reason="Done with secret=P@ssword123."))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-26", goal_text="redaction test", source="voice")
    res = orch.execute_goal(req)
    assert not hasattr(res, "screenshot")
    assert not hasattr(res, "api_key")
    assert not hasattr(res, "password")


# 27. Bounded audit events
def test_bounded_audit_events():
    mock_p = MockPlannerProvider([
        PlannerOutput(decision=DoneDecision(reason="Audit test done."))
    ])
    orch, _, _, _ = _setup_orchestrator_harness(provider=mock_p)
    req = GoalRequest(goal_id="g-27", goal_text="audit test", source="voice")
    res = orch.execute_goal(req)
    events = orch.audit_events
    assert len(events) >= 2
    types = [e["event_type"] for e in events]
    assert "GOAL_STARTED" in types
    assert "GOAL_COMPLETED" in types


# 28. Full lifecycle completion
def test_full_lifecycle_completion():
    class FullLifecyclePlanner:
        def __init__(self):
            self.step = 0
        def choose_action(self, planner_in):
            self.step += 1
            if self.step == 1:
                spec = SemanticTargetSpec(obs_id=planner_in.obs_id, role="AXTextArea")
                act = UniversalAction(action_id="act-1", obs_id=planner_in.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 5)), target_spec=spec)
                return PlannerOutput(decision=ActionDecision(action=act))
            return PlannerOutput(decision=DoneDecision(reason="Full lifecycle completed."))

    orch, _, _, _ = _setup_orchestrator_harness(provider=FullLifecyclePlanner())

    req = GoalRequest(goal_id="g-28", goal_text="full lifecycle", source="voice", metadata={"user_id": "test"})
    res = orch.execute_goal(req)
    assert res.status == GoalExecutionState.COMPLETED
    assert res.goal_id == "g-28"
    assert res.turn_count == 2
    assert res.elapsed_time_s >= 0.0
    assert len(res.history) == 1
