"""
Tests for Evie Phase 3: Goal-Oriented Computer Planner Substrate.
Verifies Planner contract, compact observation serialization, single-action turn bounds,
multi-step goal state machine, error/stale recovery, Safety Gate enforcement, and failure containment.
"""

import time
import pytest
from pydantic import ValidationError

from cc_fixtures import TEXTEDIT, TERMINAL, observation, window
from test_computer_control_executor import FakeLive, Clock, _idx, _setup, textedit, AGREED
from services.computer_control.actions import Op, get_definition, is_enabled
from services.computer_control.executor import execute_action, ObservationCache
from services.computer_control.gate import GateOutcome, RiskLevel, DEFAULT_POLICY
from services.computer_control.models import (
    ActionStatus, ActivateAppArgs, AppIdentity, ClickElementArgs, GateDecision,
    NoArgs, Op, PressKeyArgs, ResolutionMethod, ResolvedTarget, ScrollArgs,
    SelectArgs, SelectTabArgs, SetTextArgs, UniversalAction, VerificationStatus,
    WaitForStateArgs
)
from services.computer_control.observer import observe_app_with_handles
from services.computer_control.planner import (
    ActionDecision, AskDecision, CannotProceedDecision, DoneDecision, GoalBudget,
    GoalOutcomeStatus, HistoryEntry, MockPlannerProvider, PlannerDecisionKind,
    PlannerInput, PlannerOutput, RuleBasedPlannerProvider, run_goal_loop,
    serialize_compact_observation
)
from services.computer_control.resolver import SemanticTargetSpec, TargetSpec, resolve_semantic


# --- 1. Valid ACTION Output ---
def test_valid_action_output():
    act = UniversalAction(
        action_id="act-1", obs_id="obs-1", op=Op.ACTIVATE_APP,
        args=ActivateAppArgs(bundle_id="com.apple.TextEdit")
    )
    out = PlannerOutput(decision=ActionDecision(action=act))
    assert out.decision.kind is PlannerDecisionKind.ACTION
    assert out.decision.action.action_id == "act-1"


# --- 2. Invalid Action Schema ---
def test_invalid_action_schema():
    with pytest.raises(ValidationError):
        # Empty action_id
        UniversalAction(action_id="", obs_id="obs-1", op=Op.CLICK_ELEMENT)


# --- 3. Unknown Operation ---
def test_unknown_operation():
    with pytest.raises(ValidationError):
        UniversalAction(action_id="act-1", obs_id="obs-1", op="unknown_op")


# --- 4. Missing Obs_Id ---
def test_missing_obs_id():
    with pytest.raises(ValidationError):
        UniversalAction(action_id="act-1", obs_id="", op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.TextEdit"))


# --- 5. Stale Obs_Id ---
def test_stale_obs_id_rejection():
    backend, clock, cache = _setup(textedit())
    spec = SemanticTargetSpec(obs_id="obs-stale", role="AXTextArea")
    act = UniversalAction(
        action_id="act-1", obs_id="obs-stale", op=Op.SET_TEXT,
        args=SetTextArgs(text_span=(0, 4)), target_spec=spec
    )
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))])
    
    def _reobs(b):
        return cache.observation, cache.handles

    res = run_goal_loop(
        "type text", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=2), clock=clock
    )
    assert res.history[0].status is ActionStatus.STALE and res.history[0].reason == "STALE_OBS_ID"


# --- 6. Semantic Target Selection ---
def test_semantic_target_selection():
    obs = observation([{"role": "AXButton", "label": "Take Photo", "enabled": True}])
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Take Photo")
    res = resolve_semantic(obs, spec)
    assert res.kind == "RESOLVED" and res.target.target.role == "AXButton"


# --- 7. ASK Output ---
def test_ask_output():
    out = PlannerOutput(decision=AskDecision(question="Which window?", reason="MULTIPLE_WINDOWS"))
    assert out.decision.kind is PlannerDecisionKind.ASK
    assert out.decision.reason == "MULTIPLE_WINDOWS"


# --- 8. DONE Output ---
def test_done_output():
    out = PlannerOutput(decision=DoneDecision(reason="Goal achieved"))
    assert out.decision.kind is PlannerDecisionKind.DONE
    assert out.decision.reason == "Goal achieved"


# --- 9. CANNOT_PROCEED Output ---
def test_cannot_proceed_output():
    out = PlannerOutput(decision=CannotProceedDecision(reason="UNSUPPORTED_APP", explanation="Cannot control unsupported UI."))
    assert out.decision.kind is PlannerDecisionKind.CANNOT_PROCEED
    assert out.decision.reason == "UNSUPPORTED_APP"


# --- 10. Ambiguous Target Rejection ---
def test_ambiguous_target_rejection():
    obs = observation([
        {"role": "AXButton", "label": "Continue"},
        {"role": "AXButton", "label": "Continue"}
    ])
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Continue")
    act = UniversalAction(action_id="a1", obs_id=obs.obs_id, op=Op.CLICK_ELEMENT, args=ClickElementArgs(click_count=1), target_spec=spec)
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))])
    backend, clock, cache = _setup(textedit())
    cache.observation = obs

    def _reobs(b):
        return obs, cache.handles

    res = run_goal_loop(
        "click continue", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=2), clock=clock
    )
    assert res.status is GoalOutcomeStatus.ASK_USER and res.final_decision.reason == "AMBIGUOUS_TARGET"


# --- 11. Target Not Found ---
def test_target_not_found():
    obs = observation([{"role": "AXButton", "label": "Cancel"}])
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Nonexistent")
    act = UniversalAction(action_id="a1", obs_id=obs.obs_id, op=Op.CLICK_ELEMENT, args=ClickElementArgs(click_count=1), target_spec=spec)
    provider = MockPlannerProvider([
        PlannerOutput(decision=ActionDecision(action=act)),
        PlannerOutput(decision=CannotProceedDecision(reason="NO_TARGET", explanation="Target non-existent"))
    ])
    backend, clock, cache = _setup(textedit())
    cache.observation = obs

    def _reobs(b):
        return obs, cache.handles

    res = run_goal_loop(
        "click cancel", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=2), clock=clock
    )
    assert res.history[0].status is ActionStatus.FAILED and res.history[0].reason == "TARGET_NOT_FOUND"


# --- 12. Blocked Action ---
def test_blocked_action():
    obs = observation([{"role": "AXButton", "label": "Run"}], app=TERMINAL, frontmost=TERMINAL)
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Run")
    act = UniversalAction(action_id="a1", obs_id=obs.obs_id, op=Op.CLICK_ELEMENT, args=ClickElementArgs(click_count=1), target_spec=spec)
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))])
    backend, clock, cache = _setup(textedit())
    cache.observation = obs

    def _reobs(b):
        return obs, cache.handles

    res = run_goal_loop(
        "click terminal button", provider, backend, _reobs, allowed_apps=frozenset({TERMINAL.bundle_id}),
        budget=GoalBudget(max_turns=2), clock=clock
    )
    assert res.status is GoalOutcomeStatus.BLOCKED and res.history[0].reason == "RESTRICTED_APP"


# --- 13. Stale Action Recovery ---
def test_stale_action_recovery():
    backend, clock, cache = _setup(textedit())
    stale_obs_id = "obs-old"
    spec1 = SemanticTargetSpec(obs_id=stale_obs_id, role="AXTextArea")
    act1 = UniversalAction(action_id="a1", obs_id=stale_obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec1)
    
    act2_spec = SemanticTargetSpec(obs_id=cache.observation.obs_id, role="AXTextArea")
    act2 = UniversalAction(action_id="a2", obs_id=cache.observation.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=act2_spec)
    
    provider = MockPlannerProvider([
        PlannerOutput(decision=ActionDecision(action=act1)),
        PlannerOutput(decision=ActionDecision(action=act2)),
        PlannerOutput(decision=DoneDecision(reason="Text set"))
    ])

    def _reobs(b):
        return cache.observation, cache.handles

    res = run_goal_loop(
        "type text", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=4), clock=clock
    )
    assert res.history[0].status is ActionStatus.STALE
    assert res.history[1].status is ActionStatus.SUCCESS
    assert res.status is GoalOutcomeStatus.COMPLETED


# --- 14. Multi-Step Goal ---
def test_multi_step_goal():
    backend, clock, cache = _setup(textedit())
    cache.observation = cache.observation.model_copy(update={"running_apps": (TEXTEDIT,)})
    provider = RuleBasedPlannerProvider()

    def _reobs(b):
        cache.observation = cache.observation.model_copy(update={"frontmost": TEXTEDIT})
        return cache.observation, cache.handles

    res = run_goal_loop(
        "Open TextEdit and type Hello Evie", provider, backend, _reobs,
        allowed_apps=frozenset({TEXTEDIT.bundle_id}), budget=GoalBudget(max_turns=5), clock=clock
    )
    assert res.status is GoalOutcomeStatus.COMPLETED
    assert len(res.history) >= 1


# --- 15. Re-observation After Mutation ---
def test_reobservation_after_mutation():
    backend, clock, cache = _setup(textedit())
    reobs_called = []

    def _reobs(b):
        reobs_called.append(True)
        return cache.observation, cache.handles

    spec = SemanticTargetSpec(obs_id=cache.observation.obs_id, role="AXTextArea")
    act = UniversalAction(action_id="a1", obs_id=cache.observation.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec)
    provider = MockPlannerProvider([
        PlannerOutput(decision=ActionDecision(action=act)),
        PlannerOutput(decision=DoneDecision(reason="Typed text"))
    ])

    res = run_goal_loop(
        "type text", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=3), clock=clock
    )
    assert len(reobs_called) >= 2


# --- 16. Action Budget Exhaustion ---
def test_action_budget_exhaustion():
    backend, clock, cache = _setup(textedit())
    spec = SemanticTargetSpec(obs_id="obs-invalid", role="AXTextArea")
    act = UniversalAction(action_id="a1", obs_id="obs-invalid", op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec)
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))] * 10)

    def _reobs(b):
        return cache.observation, cache.handles

    res = run_goal_loop(
        "loop forever", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=3), clock=clock
    )
    assert res.status is GoalOutcomeStatus.BUDGET_EXCEEDED and "MAX_TURNS_EXCEEDED" in res.diagnostic


# --- 17. Wall-Clock Timeout ---
def test_wall_clock_timeout():
    backend, clock, cache = _setup(textedit())
    spec = SemanticTargetSpec(obs_id="obs-stale", role="AXTextArea")
    act = UniversalAction(action_id="a1", obs_id="obs-stale", op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec)
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))] * 10)

    def _reobs(b):
        clock.sleep(20.0)
        return cache.observation, cache.handles

    res = run_goal_loop(
        "timeout test", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=10, max_wall_time_s=10.0), clock=clock
    )
    assert res.status is GoalOutcomeStatus.BUDGET_EXCEEDED and "WALL_CLOCK_TIMEOUT" in res.diagnostic


# --- 18. Consecutive No-Op Limit ---
def test_consecutive_noop_limit():
    backend, clock, cache = _setup(textedit())
    spec = SemanticTargetSpec(obs_id="obs-invalid", role="AXTextArea")
    act = UniversalAction(action_id="a1", obs_id="obs-invalid", op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec)
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))] * 5)

    def _reobs(b):
        return cache.observation, cache.handles

    res = run_goal_loop(
        "noop test", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=10, max_consecutive_noops=3), clock=clock
    )
    assert res.status is GoalOutcomeStatus.BUDGET_EXCEEDED and "MAX_CONSECUTIVE_NOOPS" in res.diagnostic


# --- 19. Clarification Flow ---
def test_clarification_flow():
    out = PlannerOutput(decision=AskDecision(question="Which document?", reason="AMBIGUOUS_DOCUMENT"))
    provider = MockPlannerProvider([out])
    backend, clock, cache = _setup(textedit())

    def _reobs(b):
        return cache.observation, cache.handles

    res = run_goal_loop(
        "open doc", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=3), clock=clock
    )
    assert res.status is GoalOutcomeStatus.ASK_USER and res.final_decision.question == "Which document?"


# --- 20. Failed Verification ---
def test_failed_verification():
    backend, clock, cache = _setup(textedit(), wrong_text=True)
    ti = _idx(cache.observation, "AXTextArea")
    t = cache.observation.targets[ti]
    spec = SemanticTargetSpec(obs_id=cache.observation.obs_id, role=t.role)
    act = UniversalAction(action_id="a1", obs_id=cache.observation.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec)
    provider = MockPlannerProvider([
        PlannerOutput(decision=ActionDecision(action=act)),
        PlannerOutput(decision=CannotProceedDecision(reason="VERIFICATION_FAILED", explanation="Mutation could not be verified."))
    ])

    def _reobs(b):
        return cache.observation, cache.handles

    res = run_goal_loop(
        "type text", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=3), clock=clock
    )
    assert res.history[0].verification is VerificationStatus.INCONCLUSIVE


# --- 21. Planner Cannot Bypass Safety Gate ---
def test_planner_cannot_bypass_safety_gate():
    obs = observation([{"role": "AXTextField", "subrole": "AXSecureTextField", "label": "Secret Password"}])
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXTextField", subrole="AXSecureTextField", label="Secret Password")
    act = UniversalAction(action_id="a1", obs_id=obs.obs_id, op=Op.SET_TEXT, args=SetTextArgs(text_span=(0, 4)), target_spec=spec)
    provider = MockPlannerProvider([PlannerOutput(decision=ActionDecision(action=act))])
    backend, clock, cache = _setup(textedit())
    cache.observation = obs

    def _reobs(b):
        return obs, cache.handles

    res = run_goal_loop(
        "type secret", provider, backend, _reobs, allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        budget=GoalBudget(max_turns=2), clock=clock
    )
    assert res.status is GoalOutcomeStatus.BLOCKED and res.history[0].reason == "SECURE_FIELD"


# --- 22. Planner Cannot Execute Shell ---
def test_planner_cannot_execute_shell():
    # Attempting to invent an unsupported shell operation fails schema validation
    with pytest.raises(ValidationError):
        UniversalAction(action_id="a1", obs_id="obs-1", op="shell_exec", args=NoArgs())


# --- 23. Planner Cannot Emit Coordinates ---
def test_planner_cannot_emit_coordinates():
    # ClickElementArgs takes click_count, not x/y coordinates
    args = ClickElementArgs(click_count=1)
    assert not hasattr(args, "x") and not hasattr(args, "y")


# --- 24. Planner Cannot Emit Arbitrary Keyboard Strings ---
def test_planner_cannot_emit_arbitrary_keyboard_strings():
    # PressKeyArgs takes only bounded arrow keys ("LEFT_ARROW", "RIGHT_ARROW", "UP_ARROW", "DOWN_ARROW")
    with pytest.raises(ValidationError):
        PressKeyArgs(key="rm -rf /")


# --- 25. Secure Values Excluded From Planner Context ---
def test_secure_values_excluded_from_planner_context():
    obs = observation([{"role": "AXTextField", "subrole": "AXSecureTextField", "value_summary": None}])
    compact = serialize_compact_observation(obs)
    ctrl = compact["controls"][0]
    assert ctrl["value_summary"] == "[REDACTED_SECURE_FIELD]"


# --- 26. Bounded Action History ---
def test_bounded_action_history():
    entry = HistoryEntry(
        step=1, op=Op.CLICK_ELEMENT, status=ActionStatus.SUCCESS,
        verification=VerificationStatus.VERIFIED
    )
    assert entry.step == 1 and entry.op is Op.CLICK_ELEMENT
    assert not hasattr(entry, "screenshot") and not hasattr(entry, "ax_pointer")


# --- 27. Compact Observation Serialization ---
def test_compact_observation_serialization():
    obs = observation([
        {"role": "AXButton", "label": "Take Photo", "enabled": True},
        {"role": "AXTextArea", "label": "Document", "enabled": True}
    ])
    compact = serialize_compact_observation(obs)
    assert compact["obs_id"] == obs.obs_id
    assert compact["frontmost"]["bundle_id"] == TEXTEDIT.bundle_id
    assert len(compact["controls"]) == 2
    assert compact["controls"][0]["label"] == "Take Photo"
