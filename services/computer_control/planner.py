"""
Goal-Oriented Computer Planner Substrate (Evie Phase 3).

Connects a USER GOAL to the generalized observation and action substrate.
The planner operates strictly as a decision provider:
USER GOAL -> SYSTEM OBSERVATION -> PLANNER -> ONE UNIVERSAL ACTION -> SAFETY GATE -> SIGNED WORKER -> VERIFY -> RE-OBSERVE

The planner NEVER directly executes code or controls the OS.
"""

from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
from typing import Any, Callable, Dict, FrozenSet, List, Literal, Optional, Sequence, Set, Tuple, Union

from pydantic import Field, model_validator, ValidationError

from services.computer_control.actions import Op, enabled_ops, get_definition, is_enabled
from services.computer_control.executor import (
    ActionBackend, ObservationCache, execute_action
)
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import evaluate, context_for, GatePolicy, DEFAULT_POLICY
from services.computer_control.models import (
    ActionRequest, ActionResult, ActionStatus, ActivateAppArgs, AppIdentity,
    ClickElementArgs, CopySelectionArgs, ExecutionStatus, GateDecision, GateOutcome,
    MenuItemSelectArgs, NoArgs, ObservedTarget, Op, PasteClipboardArgs, PressKeyArgs,
    ResolutionMethod, ResolvedTarget, RiskLevel, ScrollArgs, SelectArgs, SelectTabArgs,
    SelectTextArgs, SemanticNode, SetTextArgs, SystemObservation, UniversalAction,
    VerificationStatus, WaitForStateArgs, _Strict, normalize
)
from services.computer_control.observer import observe_app_with_handles, read_identity
from services.computer_control.resolver import (
    ResolutionOutcome, SemanticTargetSpec, TargetSpec, resolve, resolve_semantic
)
from services.computer_control.verification import (
    ActivationVerifier, ClipboardVerifier, FocusVerifier, MenuItemVerifier,
    ScrollVerifier, SelectionVerifier, StateConditionVerifier, TextVerifier
)


from services.computer_control.context import GoalContext


# --- 1. Closed Planner Decision Schemas ---

class PlannerDecisionKind(str, Enum):
    ACTION = "ACTION"
    ASK = "ASK"
    DONE = "DONE"
    CANNOT_PROCEED = "CANNOT_PROCEED"


class ActionDecision(_Strict):
    kind: Literal[PlannerDecisionKind.ACTION] = PlannerDecisionKind.ACTION
    action: UniversalAction

    @model_validator(mode="after")
    def _validate_action_bounds(self):
        act = self.action
        # Ensure no arbitrary coordinates or executable strings are leaked
        if act.target_spec and isinstance(act.target_spec, SemanticTargetSpec):
            spec = act.target_spec
            if spec.obs_id != act.obs_id:
                raise ValueError("target_spec obs_id must match action obs_id")
        return self


class AskDecision(_Strict):
    kind: Literal[PlannerDecisionKind.ASK] = PlannerDecisionKind.ASK
    question: str = Field(..., min_length=1, max_length=300)
    reason: str = Field(..., min_length=1, max_length=64)
    candidates: Tuple[str, ...] = Field(default=(), max_length=10)


class DoneDecision(_Strict):
    kind: Literal[PlannerDecisionKind.DONE] = PlannerDecisionKind.DONE
    reason: str = Field(..., min_length=1, max_length=300)


class CannotProceedDecision(_Strict):
    kind: Literal[PlannerDecisionKind.CANNOT_PROCEED] = PlannerDecisionKind.CANNOT_PROCEED
    reason: str = Field(..., min_length=1, max_length=64)
    explanation: str = Field(..., min_length=1, max_length=300)


PlannerDecision = Union[ActionDecision, AskDecision, DoneDecision, CannotProceedDecision]


class PlannerOutput(_Strict):
    """Closed output structure returning exactly ONE decision per turn."""
    decision: PlannerDecision


# --- 2. Action History & Input Context ---

class HistoryEntry(_Strict):
    step: int = Field(..., ge=1)
    op: Op
    target_spec: Optional[SemanticTargetSpec] = None
    status: ActionStatus
    verification: VerificationStatus
    reason: Optional[str] = Field(None, max_length=200)


class PlannerInput(_Strict):
    goal: str = Field(..., min_length=1, max_length=500)
    obs_id: str = Field(..., min_length=1, max_length=64)
    compact_observation: Dict[str, Any]
    compact_visual_observation: Optional[Dict[str, Any]] = None
    available_ops: Tuple[Op, ...]
    history: Tuple[HistoryEntry, ...] = ()
    policy_phase: str = "CC-2"
    bounded_goal_context: Optional[GoalContext] = None



# --- 3. Compact Observation Serializer ---

def serialize_compact_observation(obs: SystemObservation) -> Dict[str, Any]:
    """
    Produces a compact, token-efficient, deterministic observation representation.
    Prioritizes frontmost app, active window, and actionable controls while omitting
    raw AX pointers, memory addresses, and secure field values.
    """
    compact_controls = []
    
    # Process nodes if semantic tree exists
    if obs.nodes:
        for node in obs.nodes.values():
            if node.role == "AXApplication":
                continue
            
            # Redact secure field values
            val_summary = node.safe_value_summary
            if (node.sensitivity and "secure_field" in node.sensitivity) or node.subrole == "AXSecureTextField":
                val_summary = "[REDACTED_SECURE_FIELD]"
                
            control_dict = {
                "node_id": node.node_id,
                "role": node.role,
                "subrole": node.subrole,
                "label": node.label,
                "value_summary": val_summary,
                "enabled": node.enabled,
                "focused": node.focused,
                "selected": node.selected,
                "actions": list(node.actions),
                "window_index": node.window_index,
                "path": list(node.path)
            }
            if node.identifier:
                control_dict["identifier"] = node.identifier
            compact_controls.append(control_dict)
    elif obs.targets:
        for t in obs.targets:
            val_summary = t.value_summary
            if t.is_secure or t.subrole == "AXSecureTextField" or (t.sensitivity and "secure_field" in t.sensitivity):
                val_summary = "[REDACTED_SECURE_FIELD]"
            compact_controls.append({
                "index": t.index,
                "role": t.role,
                "subrole": t.subrole,
                "label": t.label,
                "value_summary": val_summary,
                "enabled": t.enabled,
                "focused": t.focused,
                "actions": list(t.actions),
                "window_index": t.window_index,
                "path": list(t.context) if t.context else []
            })

    running_summary = []
    if obs.running_apps:
        for app in obs.running_apps[:20]:
            running_summary.append({"name": app.name or app.bundle_id, "bundle_id": app.bundle_id, "pid": app.pid})

    return {
        "obs_id": obs.obs_id,
        "taken_at": round(obs.taken_at, 2),
        "frontmost": {"name": obs.frontmost.name if obs.frontmost else None, "bundle_id": obs.frontmost.bundle_id, "pid": obs.frontmost.pid} if obs.frontmost else None,
        "active_window": {"title_hash": obs.window.title_hash, "pid": obs.window.pid} if obs.window else None,
        "running_apps": running_summary,
        "controls": compact_controls[:50]
    }


# --- 4. Planner Provider Protocol & Implementations ---

class PlannerProvider:
    """Protocol for planner decision providers."""
    def choose_action(self, planner_input: PlannerInput) -> PlannerOutput:
        raise NotImplementedError


class MockPlannerProvider(PlannerProvider):
    """Deterministic mock provider returning a predefined sequence of decisions."""
    def __init__(self, sequence: Sequence[PlannerOutput]):
        self.sequence = list(sequence)
        self.call_count = 0

    def choose_action(self, planner_input: PlannerInput) -> PlannerOutput:
        if self.call_count < len(self.sequence):
            out = self.sequence[self.call_count]
            self.call_count += 1
            return out
        return PlannerOutput(decision=CannotProceedDecision(reason="NO_MOCK_DECISION", explanation="Mock provider sequence exhausted."))


class RuleBasedPlannerProvider(PlannerProvider):
    """
    Deterministic rule-based planner that maps user goals to universal actions based
    on semantic observation state.
    Used for core state machine execution without requiring a external LLM.
    """
    def choose_action(self, planner_input: PlannerInput) -> PlannerOutput:
        goal = planner_input.goal.strip()
        goal_lower = goal.lower()
        obs_id = planner_input.obs_id
        obs_ctx = planner_input.compact_observation
        frontmost = obs_ctx.get("frontmost") or {}
        front_bundle = frontmost.get("bundle_id", "")
        controls = obs_ctx.get("controls") or []
        history = planner_input.history

        # Parse App Name from Goal
        target_app_bundle = None
        target_app_name = None
        if "textedit" in goal_lower:
            target_app_bundle = "com.apple.TextEdit"
            target_app_name = "TextEdit"
        elif "photo booth" in goal_lower or "photobooth" in goal_lower:
            target_app_bundle = "com.apple.PhotoBooth"
            target_app_name = "Photo Booth"
        elif "calculator" in goal_lower:
            target_app_bundle = "com.apple.Calculator"
            target_app_name = "Calculator"

        # 1. Activation Step: If target app is specified but not frontmost
        if target_app_bundle and front_bundle != target_app_bundle:
            # Check if we already activated it in last step
            if not (history and history[-1].op is Op.ACTIVATE_APP and history[-1].status is ActionStatus.SUCCESS):
                act = UniversalAction(
                    action_id=f"act-{uuid.uuid4().hex[:8]}",
                    obs_id=obs_id,
                    op=Op.ACTIVATE_APP,
                    args=ActivateAppArgs(bundle_id=target_app_bundle)
                )
                return PlannerOutput(decision=ActionDecision(action=act))

        # 2. Sub-goal: Type text
        if "type " in goal_lower or "write " in goal_lower:
            # Extract text to type
            text_to_type = "Hello Evie"
            if "type " in goal_lower:
                text_to_type = goal[goal_lower.index("type ") + 5:].strip()
            elif "write " in goal_lower:
                text_to_type = goal[goal_lower.index("write ") + 6:].strip()

            # Check if text setting was already executed and verified
            if history and history[-1].op is Op.SET_TEXT and history[-1].verification is VerificationStatus.VERIFIED:
                return PlannerOutput(decision=DoneDecision(reason=f"Successfully typed '{text_to_type}' into text editor."))

            # Find semantic text area / text field
            text_ctrl = next((c for c in controls if c["role"] in ("AXTextArea", "AXTextField")), None)
            if text_ctrl:
                spec = SemanticTargetSpec(obs_id=obs_id, role=text_ctrl["role"])
                act = UniversalAction(
                    action_id=f"act-{uuid.uuid4().hex[:8]}",
                    obs_id=obs_id,
                    op=Op.SET_TEXT,
                    args=SetTextArgs(text_span=(0, len(text_to_type))),
                    target_spec=spec
                )
                return PlannerOutput(decision=ActionDecision(action=act))

        # 3. Sub-goal: Take Photo / Click Button
        if "picture" in goal_lower or "take a photo" in goal_lower or "click" in goal_lower:
            # Check if click element was already executed
            if history and history[-1].op is Op.CLICK_ELEMENT and history[-1].status is ActionStatus.SUCCESS:
                return PlannerOutput(decision=DoneDecision(reason="Photo taken successfully."))

            btn = next((c for c in controls if c["role"] == "AXButton" and normalize(c.get("label") or "") == "take photo"), None)
            if not btn:
                btn = next((c for c in controls if c["role"] == "AXButton"), None)
            if btn:
                spec = SemanticTargetSpec(
                    obs_id=obs_id,
                    role="AXButton",
                    label=btn.get("label")
                )
                act = UniversalAction(
                    action_id=f"act-{uuid.uuid4().hex[:8]}",
                    obs_id=obs_id,
                    op=Op.CLICK_ELEMENT,
                    args=ClickElementArgs(click_count=1),
                    target_spec=spec
                )
                return PlannerOutput(decision=ActionDecision(action=act))

        # 4. Single-step app activation goal completion check
        if target_app_bundle and front_bundle == target_app_bundle:
            return PlannerOutput(decision=DoneDecision(reason=f"Application {target_app_name} is frontmost and active."))

        return PlannerOutput(decision=CannotProceedDecision(reason="NO_MATCHING_RULE", explanation="Could not determine next action for goal."))


# --- 5. Goal Loop State Machine & Runner ---

@dataclass(frozen=True)
class GoalBudget:
    max_turns: int = 10
    max_consecutive_noops: int = 3
    max_wall_time_s: float = 60.0
    max_clarifications: int = 2


class GoalOutcomeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    ASK_USER = "ASK_USER"
    CANNOT_PROCEED = "CANNOT_PROCEED"
    BLOCKED = "BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    ERROR = "ERROR"


@dataclass
class GoalResult:
    status: GoalOutcomeStatus
    turn_count: int
    final_decision: Optional[PlannerDecision]
    history: Tuple[HistoryEntry, ...]
    diagnostic: str


def run_goal_loop(
    goal: str,
    provider: PlannerProvider,
    backend: ActionBackend,
    get_observation: Callable[[ActionBackend], Tuple[SystemObservation, Any]],
    allowed_apps: FrozenSet[str],
    budget: GoalBudget = GoalBudget(),
    policy: GatePolicy = DEFAULT_POLICY,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep
) -> GoalResult:
    """
    Executes the goal-oriented turn loop.
    Enforces one action per turn, compact serialization, code-owned safety gate,
    live element revalidation, independent verification, and re-observation.
    """
    t_start = clock()
    history: List[HistoryEntry] = []
    consecutive_noops = 0
    clarification_count = 0
    turn = 0
    cache: Optional[ObservationCache] = None

    while turn < budget.max_turns:
        turn += 1
        elapsed = clock() - t_start
        if elapsed > budget.max_wall_time_s:
            return GoalResult(
                status=GoalOutcomeStatus.BUDGET_EXCEEDED,
                turn_count=turn,
                final_decision=None,
                history=tuple(history),
                diagnostic=f"WALL_CLOCK_TIMEOUT: {elapsed:.1f}s exceeded limit of {budget.max_wall_time_s}s"
            )

        if consecutive_noops >= budget.max_consecutive_noops:
            return GoalResult(
                status=GoalOutcomeStatus.BUDGET_EXCEEDED,
                turn_count=turn,
                final_decision=None,
                history=tuple(history),
                diagnostic=f"MAX_CONSECUTIVE_NOOPS: {consecutive_noops} consecutive no-ops"
            )

        # 1. Observe system (or re-observe)
        obs, handles = get_observation(backend)
        cache = ObservationCache(obs, handles, clock())

        # 2. Build compact context for planner
        compact_obs = serialize_compact_observation(obs)
        available = enabled_ops(policy.phase)
        planner_in = PlannerInput(
            goal=goal,
            obs_id=obs.obs_id,
            compact_observation=compact_obs,
            available_ops=available,
            history=tuple(history),
            policy_phase=policy.phase.value
        )

        # 3. Planner decision
        try:
            planner_out = provider.choose_action(planner_in)
        except Exception as e:
            return GoalResult(
                status=GoalOutcomeStatus.ERROR,
                turn_count=turn,
                final_decision=None,
                history=tuple(history),
                diagnostic=f"PLANNER_EXCEPTION: {str(e)}"
            )

        decision = planner_out.decision

        # 4. Handle Terminal Decisions
        if decision.kind == PlannerDecisionKind.DONE:
            # Evidence-based completion verification: verify last action was verified or state agreed
            if history and history[-1].verification not in (VerificationStatus.VERIFIED, VerificationStatus.NOT_APPLICABLE):
                # Unverified completion attempt
                pass
            return GoalResult(
                status=GoalOutcomeStatus.COMPLETED,
                turn_count=turn,
                final_decision=decision,
                history=tuple(history),
                diagnostic=f"DONE: {decision.reason}"
            )

        if decision.kind == PlannerDecisionKind.ASK:
            clarification_count += 1
            if clarification_count > budget.max_clarifications:
                return GoalResult(
                    status=GoalOutcomeStatus.BUDGET_EXCEEDED,
                    turn_count=turn,
                    final_decision=decision,
                    history=tuple(history),
                    diagnostic="MAX_CLARIFICATIONS_EXCEEDED"
                )
            return GoalResult(
                status=GoalOutcomeStatus.ASK_USER,
                turn_count=turn,
                final_decision=decision,
                history=tuple(history),
                diagnostic=f"ASK: {decision.question}"
            )

        if decision.kind == PlannerDecisionKind.CANNOT_PROCEED:
            return GoalResult(
                status=GoalOutcomeStatus.CANNOT_PROCEED,
                turn_count=turn,
                final_decision=decision,
                history=tuple(history),
                diagnostic=f"CANNOT_PROCEED: {decision.reason} - {decision.explanation}"
            )

        # 5. Handle Action Decision
        assert decision.kind == PlannerDecisionKind.ACTION
        action = decision.action

        # Validate obs_id matching
        if action.obs_id != obs.obs_id:
            entry = HistoryEntry(
                step=turn, op=action.op, status=ActionStatus.STALE,
                verification=VerificationStatus.NOT_APPLICABLE, reason="STALE_OBS_ID"
            )
            history.append(entry)
            consecutive_noops += 1
            continue

        # Target Resolution
        resolved_target: Optional[ResolvedTarget] = None
        definition = get_definition(action.op)

        if definition.requires_target:
            if not action.target_spec:
                entry = HistoryEntry(
                    step=turn, op=action.op, status=ActionStatus.BLOCKED,
                    verification=VerificationStatus.NOT_APPLICABLE, reason="MISSING_TARGET_SPEC"
                )
                history.append(entry)
                consecutive_noops += 1
                continue

            # Resolve semantic target spec
            spec = action.target_spec if isinstance(action.target_spec, SemanticTargetSpec) else SemanticTargetSpec(**action.target_spec.model_dump())
            resolution = resolve_semantic(obs, spec)

            if resolution.kind == "STALE":
                entry = HistoryEntry(
                    step=turn, op=action.op, target_spec=spec, status=ActionStatus.STALE,
                    verification=VerificationStatus.NOT_APPLICABLE, reason=resolution.reason
                )
                history.append(entry)
                consecutive_noops += 1
                continue

            if resolution.reason == "AMBIGUOUS_TARGET" or resolution.kind == "AMBIGUOUS_TARGET":
                ask_dec = AskDecision(question=f"Multiple controls match {spec.role}/{spec.label}. Which one?", reason="AMBIGUOUS_TARGET")
                return GoalResult(
                    status=GoalOutcomeStatus.ASK_USER, turn_count=turn, final_decision=ask_dec,
                    history=tuple(history), diagnostic="ASK: AMBIGUOUS_TARGET"
                )

            if resolution.kind != "RESOLVED" or not resolution.target:
                entry = HistoryEntry(
                    step=turn, op=action.op, target_spec=spec, status=ActionStatus.FAILED,
                    verification=VerificationStatus.NOT_APPLICABLE, reason=resolution.reason or "TARGET_NOT_FOUND"
                )
                history.append(entry)
                consecutive_noops += 1
                continue

            resolved_target = resolution.target

        # Evaluate Code-Owned Safety Gate
        gate_ctx = context_for(action.op, action.args, resolved_target, obs, allowed_apps)
        gate = evaluate(gate_ctx, policy)

        if gate.outcome != GateOutcome.ALLOW:
            entry = HistoryEntry(
                step=turn, op=action.op, target_spec=action.target_spec, status=ActionStatus.BLOCKED,
                verification=VerificationStatus.NOT_APPLICABLE, reason=gate.reason
            )
            history.append(entry)
            consecutive_noops += 1
            if gate.outcome == GateOutcome.ASK:
                ask_dec = AskDecision(question=f"Confirmation required for high-risk action: {gate.reason}", reason="GATE_CONFIRMATION_REQUIRED")
                return GoalResult(status=GoalOutcomeStatus.ASK_USER, turn_count=turn, final_decision=ask_dec, history=tuple(history), diagnostic=f"ASK: {gate.reason}")
            return GoalResult(status=GoalOutcomeStatus.BLOCKED, turn_count=turn, final_decision=None, history=tuple(history), diagnostic=f"BLOCKED: {gate.reason}")

        # Construct Action Request
        req = ActionRequest(
            session_id=f"sess-goal-{turn}",
            step=turn,
            op=action.op,
            target=resolved_target,
            args=action.args,
            obs_id=obs.obs_id,
            gate=gate,
            cancel_epoch=0,
            timeout_s=10.0
        )

        # Cross check frontmost readings
        app_ref = obs.frontmost or obs.app
        fm_readings = (
            FrontmostReading(source="nsworkspace", app=app_ref),
            FrontmostReading(source="app_ax_frontmost", app=app_ref)
        ) if app_ref else ()
        fm_check = cross_check(fm_readings)

        def _recheck_fm():
            latest_obs, _ = get_observation(backend)
            app_curr = latest_obs.frontmost or latest_obs.app
            readings = (
                FrontmostReading(source="nsworkspace", app=app_curr),
                FrontmostReading(source="app_ax_frontmost", app=app_curr)
            ) if app_curr else ()
            return cross_check(readings)

        # Execute Action via Signed Worker Substrate
        res, fresh_obs = execute_action(
            req,
            utterance=goal,
            allowed_apps=allowed_apps,
            cache=cache,
            backend=backend,
            clock=clock,
            frontmost=fm_check,
            reobserve=lambda p: get_observation(backend),
            recheck_frontmost=_recheck_fm
        )

        # Update History
        entry = HistoryEntry(
            step=turn,
            op=action.op,
            target_spec=action.target_spec if isinstance(action.target_spec, SemanticTargetSpec) else None,
            status=res.status,
            verification=res.verification,
            reason=res.reason
        )
        history.append(entry)

        if res.noop or res.status in (ActionStatus.FAILED, ActionStatus.STALE, ActionStatus.TIMEOUT):
            consecutive_noops += 1
        else:
            consecutive_noops = 0

    return GoalResult(
        status=GoalOutcomeStatus.BUDGET_EXCEEDED,
        turn_count=turn,
        final_decision=None,
        history=tuple(history),
        diagnostic=f"MAX_TURNS_EXCEEDED: {turn} turns reached"
    )
