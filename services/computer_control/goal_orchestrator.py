"""
Unified Goal Execution Orchestrator for Evie (Phase 6).

Unifies observation, visual understanding fallback, LLM planning, target resolution,
code-owned safety gate evaluation, signed worker execution, independent verification,
and re-observation into ONE reusable goal-execution engine.

Input modalities (voice, chat, API, hardware) invoke this single orchestrator
via normalized GoalRequest instances.
"""

from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
from typing import Any, Callable, Dict, FrozenSet, List, Literal, Optional, Sequence, Set, Tuple, Union

from pydantic import Field, model_validator

from services.computer_control.actions import enabled_ops, get_definition, is_enabled
from services.computer_control.audit import record_for_decision, record_for_result
from services.computer_control.context import GoalContext, GoalContextManager
from services.computer_control.executor import ActionBackend, ObservationCache, execute_action
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy, context_for, evaluate
from services.computer_control.goal_memory import GoalMemoryManager
from services.computer_control.models import (
    ActionRequest, ActionResult, ActionStatus, AppIdentity, GateDecision, GateOutcome,
    ObservedTarget, Op, ResolvedTarget, SystemObservation, UniversalAction, VerificationStatus, _Strict
)
from services.computer_control.observer import observe_app_with_handles
from services.computer_control.planner import (
    ActionDecision, AskDecision, CannotProceedDecision, DoneDecision, GoalBudget, HistoryEntry,
    PlannerDecision, PlannerDecisionKind, PlannerInput, PlannerOutput, PlannerProvider,
    serialize_compact_observation
)
from services.computer_control.resolver import SemanticTargetSpec, resolve_semantic
from services.computer_control.visual_observer import (
    VisualObservationProvider, VisualObservationRequest, serialize_compact_visual_observation
)


class GoalExecutionState(str, Enum):
    IDLE = "IDLE"
    OBSERVING = "OBSERVING"
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    WAITING_FOR_REOBSERVATION = "WAITING_FOR_REOBSERVATION"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    CANNOT_PROCEED = "CANNOT_PROCEED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    BUSY = "BUSY"


class GoalRequest(_Strict):
    """Normalized goal request structure for all input modalities."""
    goal_id: str = Field(..., min_length=1, max_length=64)
    goal_text: str = Field(..., min_length=1, max_length=500)
    source: str = Field("system", max_length=64)
    session_id: str = Field("default_session", max_length=64)
    created_at: float = Field(default_factory=time.time)
    metadata: Optional[Dict[str, Any]] = None


class GoalExecutionResult(_Strict):
    """Bounded, serializable goal execution summary containing no raw secrets or screenshots."""
    goal_id: str = Field(..., min_length=1, max_length=64)
    status: GoalExecutionState
    turn_count: int = Field(0, ge=0)
    clarification_count: int = Field(0, ge=0)
    elapsed_time_s: float = Field(0.0, ge=0.0)
    reason: str = Field(..., max_length=500)
    final_decision: Optional[PlannerDecision] = None
    history: Tuple[HistoryEntry, ...] = ()
    verification_summary: Optional[str] = Field(None, max_length=500)
    action_count: int = Field(0, ge=0)


ACTIVE_STATES = frozenset({
    GoalExecutionState.OBSERVING,
    GoalExecutionState.PLANNING,
    GoalExecutionState.VALIDATING,
    GoalExecutionState.EXECUTING,
    GoalExecutionState.VERIFYING,
    GoalExecutionState.WAITING_FOR_REOBSERVATION,
    GoalExecutionState.WAITING_FOR_USER,
})


class GoalExecutionOrchestrator:
    """
    Unified execution engine for Evie computer-control goals.
    Enforces concurrency bounds, GoalBudget constraints, safety gate authorization,
    signed worker execution, independent verification, and on-demand visual fallback.
    """

    def __init__(
        self,
        planner_provider: PlannerProvider,
        backend: ActionBackend,
        get_observation: Callable[[ActionBackend], Tuple[SystemObservation, Any]],
        allowed_apps: FrozenSet[str],
        visual_provider: Optional[VisualObservationProvider] = None,
        budget: GoalBudget = GoalBudget(),
        policy: GatePolicy = DEFAULT_POLICY,
        context_manager: Optional[GoalContextManager] = None,
        goal_memory: Optional[GoalMemoryManager] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        step_log: Any = None
    ):
        self._planner_provider = planner_provider
        self._backend = backend
        self._get_observation = get_observation
        self._allowed_apps = allowed_apps
        self._visual_provider = visual_provider
        self._budget = budget
        self._policy = policy
        self._context_manager = context_manager or GoalContextManager()
        self._goal_memory = goal_memory or GoalMemoryManager()
        self._clock = clock
        self._sleep = sleep
        self._step_log = step_log

        # State lifecycle
        self._active_request: Optional[GoalRequest] = None
        self._state: GoalExecutionState = GoalExecutionState.IDLE
        self._cancel_requested: bool = False
        self._history: List[HistoryEntry] = []
        self._turn_count: int = 0
        self._clarification_count: int = 0
        self._consecutive_noops: int = 0
        self._t_start: float = 0.0
        self._audit_events: List[Dict[str, Any]] = []

    @property
    def context_manager(self) -> GoalContextManager:
        return self._context_manager

    @property
    def goal_memory(self) -> GoalMemoryManager:
        return self._goal_memory

    @property
    def context(self) -> GoalContext:
        return self._context_manager.get_context()

    @property
    def state(self) -> GoalExecutionState:
        return self._state

    @property
    def active_request(self) -> Optional[GoalRequest]:

        return self._active_request

    @property
    def audit_events(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self._audit_events)

    def execute_goal(self, request: GoalRequest) -> GoalExecutionResult:
        """
        Submits and executes a normalized user goal.
        Fails fast with BUSY if another goal is active on this orchestrator instance.
        """
        if self._state in ACTIVE_STATES and self._active_request is not None:
            return GoalExecutionResult(
                goal_id=request.goal_id,
                status=GoalExecutionState.BUSY,
                turn_count=0,
                clarification_count=0,
                elapsed_time_s=0.0,
                reason="ORCHESTRATOR_BUSY: another goal execution is currently active",
                history=()
            )

        # Initialize session state
        self._active_request = request
        self._state = GoalExecutionState.OBSERVING
        self._cancel_requested = False
        self._history = []
        self._turn_count = 0
        self._clarification_count = 0
        self._consecutive_noops = 0
        self._t_start = self._clock()
        self._audit_events = []

        self._record_audit_event("GOAL_STARTED", {"goal_id": request.goal_id, "source": request.source})
        session_id = getattr(request, "session_id", "default_session") or "default_session"
        self._context_manager.set_user_goal_start(request.goal_id, request.goal_text, session_id=session_id)
        return self._run_loop(request.goal_text)

    def resume_with_clarification(self, goal_id: str, user_response: str) -> GoalExecutionResult:
        """
        Resumes a goal waiting for user clarification.
        """
        if self._active_request is None or self._active_request.goal_id != goal_id:
            return GoalExecutionResult(
                goal_id=goal_id,
                status=GoalExecutionState.CANNOT_PROCEED,
                turn_count=self._turn_count,
                clarification_count=self._clarification_count,
                elapsed_time_s=round(self._clock() - self._t_start, 2),
                reason="GOAL_NOT_FOUND: no matching active goal found for clarification",
                history=tuple(self._history)
            )

        if self._state != GoalExecutionState.WAITING_FOR_USER:
            return GoalExecutionResult(
                goal_id=goal_id,
                status=self._state,
                turn_count=self._turn_count,
                clarification_count=self._clarification_count,
                elapsed_time_s=round(self._clock() - self._t_start, 2),
                reason=f"INVALID_STATE: goal is in state {self._state.value}, not WAITING_FOR_USER",
                history=tuple(self._history)
            )

        self._clarification_count += 1
        if self._clarification_count > self._budget.max_clarifications:
            self._state = GoalExecutionState.BUDGET_EXCEEDED
            return self._make_result(
                GoalExecutionState.BUDGET_EXCEEDED,
                "MAX_CLARIFICATIONS_EXCEEDED: maximum user clarification limit reached"
            )

        self._record_audit_event("GOAL_RESUMED", {"goal_id": goal_id, "user_response": user_response[:100]})
        self._record_audit_event("CLARIFICATION_PROVIDED", {"goal_id": goal_id, "user_response": user_response[:100]})
        updated_goal_text = f"{self._active_request.goal_text}\nClarification: {user_response}"
        return self._run_loop(updated_goal_text)

    def cancel_goal(self, goal_id: str) -> GoalExecutionResult:
        """
        Safely cancels an active or waiting goal execution.
        """
        if self._active_request is not None and self._active_request.goal_id == goal_id:
            self._cancel_requested = True
            if self._state == GoalExecutionState.WAITING_FOR_USER or self._state in ACTIVE_STATES:
                self._state = GoalExecutionState.CANCELLED
                self._record_audit_event("GOAL_CANCELLED", {"goal_id": goal_id})
                return self._make_result(
                    GoalExecutionState.CANCELLED,
                    "GOAL_CANCELLED: goal execution cancelled by user request"
                )

        return self._make_result(
            self._state if self._state != GoalExecutionState.IDLE else GoalExecutionState.CANCELLED,
            "GOAL_CANCELLED: goal execution cancelled"
        )

    def _run_loop(self, current_goal_text: str) -> GoalExecutionResult:
        """
        Core turn loop implementing the goal execution state machine.
        """
        cache: Optional[ObservationCache] = None

        while self._turn_count < self._budget.max_turns:
            # Check cancellation
            if self._cancel_requested:
                self._state = GoalExecutionState.CANCELLED
                self._record_audit_event("GOAL_CANCELLED", {"goal_id": self._active_request.goal_id if self._active_request else ""})
                return self._make_result(
                    GoalExecutionState.CANCELLED,
                    "GOAL_CANCELLED: goal execution cancelled"
                )

            # Check wall time limit
            elapsed = self._clock() - self._t_start
            if elapsed > self._budget.max_wall_time_s:
                self._state = GoalExecutionState.BUDGET_EXCEEDED
                return self._make_result(
                    GoalExecutionState.BUDGET_EXCEEDED,
                    f"WALL_CLOCK_TIMEOUT: {elapsed:.1f}s exceeded wall time limit of {self._budget.max_wall_time_s}s"
                )

            # Check consecutive noops limit
            if self._consecutive_noops >= self._budget.max_consecutive_noops:
                self._state = GoalExecutionState.BUDGET_EXCEEDED
                return self._make_result(
                    GoalExecutionState.BUDGET_EXCEEDED,
                    f"MAX_CONSECUTIVE_NOOPS: {self._consecutive_noops} consecutive no-ops reached"
                )

            # 1. State: OBSERVING (Primary: Accessibility)
            self._state = GoalExecutionState.OBSERVING
            try:
                obs, handles = self._get_observation(self._backend)
            except Exception as e:
                self._state = GoalExecutionState.FAILED
                return self._make_result(
                    GoalExecutionState.FAILED,
                    f"OBSERVATION_FAILURE: failed to capture AX observation: {str(e)}"
                )

            if obs is None:
                self._state = GoalExecutionState.FAILED
                return self._make_result(
                    GoalExecutionState.FAILED,
                    "OBSERVATION_FAILURE: null AX observation returned"
                )

            self._record_audit_event("OBSERVATION_CREATED", {"obs_id": obs.obs_id, "app": obs.app.bundle_id})
            cache = ObservationCache(obs, handles, self._clock())

            # Update authoritative verified system context from observation
            app_name = obs.frontmost.name if obs.frontmost else obs.app.name if obs.app else None
            bundle_id = obs.frontmost.bundle_id if obs.frontmost else obs.app.bundle_id if obs.app else None
            win_title = obs.window.title_hash if obs.window else None
            session_id = getattr(self._active_request, "session_id", "default_session") if self._active_request else "default_session"
            self._context_manager.update_verified_state(
                app_name=app_name,
                bundle_id=bundle_id,
                window_title=win_title,
                session_id=session_id
            )

            # 2. Build Compact Context & Optional Visual Fallback
            compact_obs = serialize_compact_observation(obs)
            compact_vis_obs: Optional[Dict[str, Any]] = None

            # On-demand visual fallback: invoke only if visual provider exists AND AX semantics are lacking or visual is requested
            ax_has_controls = bool(obs.nodes or obs.targets)
            force_vis = getattr(self._visual_provider, "force_visual", False) if self._visual_provider else False
            if self._visual_provider is not None and (not ax_has_controls or force_vis):
                try:
                    has_secure = any(getattr(t, "is_secure", False) or getattr(t, "subrole", "") == "AXSecureTextField" for t in obs.targets)
                    vis_req = VisualObservationRequest(
                        obs_id=obs.obs_id,
                        app=obs.app,
                        frontmost=obs.frontmost,
                        has_secure_fields=has_secure
                    )
                    vis_obs = self._visual_provider.observe(vis_req)
                    if vis_obs:
                        compact_vis_obs = serialize_compact_visual_observation(vis_obs)
                        self._record_audit_event("VISUAL_FALLBACK_CREATED", {"obs_id": obs.obs_id, "element_count": len(vis_obs.elements)})
                except Exception:
                    compact_vis_obs = None

            available = enabled_ops(self._policy.phase)
            planner_in = PlannerInput(
                goal=current_goal_text,
                obs_id=obs.obs_id,
                compact_observation=compact_obs,
                compact_visual_observation=compact_vis_obs,
                available_ops=available,
                history=tuple(self._history),
                policy_phase=self._policy.phase.value,
                bounded_goal_context=self._context_manager.get_context(session_id=session_id)
            )

            # 3. State: PLANNING
            self._state = GoalExecutionState.PLANNING
            self._turn_count += 1

            try:
                planner_out = self._planner_provider.choose_action(planner_in)
            except Exception as e:
                self._state = GoalExecutionState.FAILED
                return self._make_result(
                    GoalExecutionState.FAILED,
                    f"PLANNER_EXCEPTION: provider raised exception: {str(e)}"
                )

            if not isinstance(planner_out, PlannerOutput) or not hasattr(planner_out, "decision"):
                self._state = GoalExecutionState.CANNOT_PROCEED
                return self._make_result(
                    GoalExecutionState.CANNOT_PROCEED,
                    "MALFORMED_PLANNER_OUTPUT: output is not a valid PlannerOutput instance"
                )

            decision = planner_out.decision
            self._record_audit_event("PLANNER_DECISION", {"kind": decision.kind.value, "step": self._turn_count})

            # 4. Handle Terminal & Non-Action Decisions
            if decision.kind == PlannerDecisionKind.DONE:
                self._state = GoalExecutionState.COMPLETED
                self._record_audit_event("GOAL_COMPLETED", {"reason": decision.reason})
                return self._make_result(
                    GoalExecutionState.COMPLETED,
                    f"DONE: {decision.reason}",
                    final_decision=decision
                )

            if decision.kind == PlannerDecisionKind.ASK:
                self._state = GoalExecutionState.WAITING_FOR_USER
                self._record_audit_event("CLARIFICATION_REQUESTED", {"question": decision.question})
                return self._make_result(
                    GoalExecutionState.WAITING_FOR_USER,
                    f"ASK: {decision.question}",
                    final_decision=decision
                )

            if decision.kind == PlannerDecisionKind.CANNOT_PROCEED:
                self._state = GoalExecutionState.CANNOT_PROCEED
                self._record_audit_event("CANNOT_PROCEED", {"reason": decision.reason})
                return self._make_result(
                    GoalExecutionState.CANNOT_PROCEED,
                    f"CANNOT_PROCEED: {decision.reason} - {decision.explanation}",
                    final_decision=decision
                )

            # 5. Handle Action Decision
            if decision.kind != PlannerDecisionKind.ACTION or not hasattr(decision, "action"):
                self._state = GoalExecutionState.CANNOT_PROCEED
                return self._make_result(
                    GoalExecutionState.CANNOT_PROCEED,
                    "INVALID_DECISION: non-action decision without valid decision kind"
                )

            action = decision.action

            # 6. State: VALIDATING
            self._state = GoalExecutionState.VALIDATING

            # Validate obs_id matching
            if action.obs_id != obs.obs_id:
                entry = HistoryEntry(
                    step=self._turn_count,
                    op=action.op,
                    status=ActionStatus.STALE,
                    verification=VerificationStatus.NOT_APPLICABLE,
                    reason="STALE_OBS_ID"
                )
                self._history.append(entry)
                self._consecutive_noops += 1
                continue

            resolved_target: Optional[ResolvedTarget] = None
            definition = get_definition(action.op)

            if definition.requires_target:
                if not action.target_spec:
                    entry = HistoryEntry(
                        step=self._turn_count,
                        op=action.op,
                        status=ActionStatus.BLOCKED,
                        verification=VerificationStatus.NOT_APPLICABLE,
                        reason="MISSING_TARGET_SPEC"
                    )
                    self._history.append(entry)
                    self._consecutive_noops += 1
                    continue

                spec = action.target_spec if isinstance(action.target_spec, SemanticTargetSpec) else SemanticTargetSpec(**action.target_spec.model_dump())
                resolution = resolve_semantic(obs, spec)

                if resolution.kind == "STALE":
                    entry = HistoryEntry(
                        step=self._turn_count,
                        op=action.op,
                        target_spec=spec,
                        status=ActionStatus.STALE,
                        verification=VerificationStatus.NOT_APPLICABLE,
                        reason=resolution.reason
                    )
                    self._history.append(entry)
                    self._consecutive_noops += 1
                    continue

                if resolution.reason == "AMBIGUOUS_TARGET" or resolution.kind == "AMBIGUOUS_TARGET":
                    ask_dec = AskDecision(
                        question=f"Multiple controls match {spec.role}/{spec.label}. Which one?",
                        reason="AMBIGUOUS_TARGET"
                    )
                    self._state = GoalExecutionState.WAITING_FOR_USER
                    self._record_audit_event("CLARIFICATION_REQUESTED", {"reason": "AMBIGUOUS_TARGET"})
                    return self._make_result(
                        GoalExecutionState.WAITING_FOR_USER,
                        "ASK: AMBIGUOUS_TARGET",
                        final_decision=ask_dec
                    )

                if resolution.kind != "RESOLVED" or not resolution.target:
                    entry = HistoryEntry(
                        step=self._turn_count,
                        op=action.op,
                        target_spec=spec,
                        status=ActionStatus.FAILED,
                        verification=VerificationStatus.NOT_APPLICABLE,
                        reason=resolution.reason or "TARGET_NOT_FOUND"
                    )
                    self._history.append(entry)
                    self._consecutive_noops += 1
                    continue

                resolved_target = resolution.target

            self._record_audit_event("ACTION_VALIDATED", {"op": action.op.value, "step": self._turn_count})

            # 7. Evaluate Code-Owned Safety Gate
            gate_ctx = context_for(action.op, action.args, resolved_target, obs, self._allowed_apps)
            gate = evaluate(gate_ctx, self._policy)

            if gate.outcome != GateOutcome.ALLOW:
                entry = HistoryEntry(
                    step=self._turn_count,
                    op=action.op,
                    target_spec=action.target_spec if isinstance(action.target_spec, SemanticTargetSpec) else None,
                    status=ActionStatus.BLOCKED,
                    verification=VerificationStatus.NOT_APPLICABLE,
                    reason=gate.reason
                )
                self._history.append(entry)
                self._consecutive_noops += 1
                self._record_audit_event("ACTION_BLOCKED", {"op": action.op.value, "reason": gate.reason})

                if gate.outcome == GateOutcome.ASK:
                    ask_dec = AskDecision(
                        question=f"Confirmation required for action: {gate.reason}",
                        reason="GATE_CONFIRMATION_REQUIRED"
                    )
                    self._state = GoalExecutionState.WAITING_FOR_USER
                    return self._make_result(
                        GoalExecutionState.WAITING_FOR_USER,
                        f"ASK: {gate.reason}",
                        final_decision=ask_dec
                    )

                self._state = GoalExecutionState.BLOCKED
                return self._make_result(
                    GoalExecutionState.BLOCKED,
                    f"BLOCKED: {gate.reason}"
                )

            # 8. State: EXECUTING (via Signed Worker Substrate)
            self._state = GoalExecutionState.EXECUTING
            req = ActionRequest(
                session_id=f"sess-goal-{self._active_request.goal_id[:8]}-{self._turn_count}",
                step=self._turn_count,
                op=action.op,
                target=resolved_target,
                args=action.args,
                obs_id=obs.obs_id,
                gate=gate,
                cancel_epoch=0,
                timeout_s=10.0
            )

            app_ref = obs.frontmost or obs.app
            fm_readings = (
                FrontmostReading(source="nsworkspace", app=app_ref),
                FrontmostReading(source="app_ax_frontmost", app=app_ref)
            ) if app_ref else ()
            fm_check = cross_check(fm_readings)

            def _recheck_fm():
                latest_obs, _ = self._get_observation(self._backend)
                app_curr = latest_obs.frontmost or latest_obs.app
                readings = (
                    FrontmostReading(source="nsworkspace", app=app_curr),
                    FrontmostReading(source="app_ax_frontmost", app=app_curr)
                ) if app_curr else ()
                return cross_check(readings)

            try:
                res, fresh_obs = execute_action(
                    req,
                    utterance=current_goal_text,
                    allowed_apps=self._allowed_apps,
                    cache=cache,
                    backend=self._backend,
                    clock=self._clock,
                    frontmost=fm_check,
                    reobserve=lambda p: self._get_observation(self._backend),
                    recheck_frontmost=_recheck_fm
                )
            except Exception as e:
                self._state = GoalExecutionState.FAILED
                return self._make_result(
                    GoalExecutionState.FAILED,
                    f"WORKER_EXCEPTION: worker failed during execution: {str(e)}"
                )

            # 9. State: VERIFYING & Record History
            self._state = GoalExecutionState.VERIFYING
            entry = HistoryEntry(
                step=self._turn_count,
                op=action.op,
                target_spec=action.target_spec if isinstance(action.target_spec, SemanticTargetSpec) else None,
                status=res.status,
                verification=res.verification,
                reason=res.reason
            )
            self._history.append(entry)
            self._record_audit_event("ACTION_EXECUTED", {"op": action.op.value, "status": res.status.value, "verification": res.verification.value})

            if res.noop or res.status in (ActionStatus.FAILED, ActionStatus.STALE, ActionStatus.TIMEOUT):
                self._consecutive_noops += 1
            else:
                self._consecutive_noops = 0

            # State: WAITING_FOR_REOBSERVATION before next turn loop iteration
            self._state = GoalExecutionState.WAITING_FOR_REOBSERVATION

        # Budget exceeded turn limit
        self._state = GoalExecutionState.BUDGET_EXCEEDED
        return self._make_result(
            GoalExecutionState.BUDGET_EXCEEDED,
            f"MAX_TURNS_EXCEEDED: {self._turn_count} turns reached limit of {self._budget.max_turns}"
        )

    def _make_result(
        self,
        status: GoalExecutionState,
        reason: str,
        final_decision: Optional[PlannerDecision] = None
    ) -> GoalExecutionResult:
        elapsed = round(self._clock() - self._t_start, 2)
        goal_id = self._active_request.goal_id if self._active_request else "unknown"
        session_id = getattr(self._active_request, "session_id", "default_session") if self._active_request else "default_session"
        action_count = sum(1 for h in self._history if h.status == ActionStatus.SUCCESS)

        self._context_manager.set_goal_outcome(goal_id, status.value, reason, session_id=session_id)

        # Verification summary
        ver_summary = None
        if self._history:
            last = self._history[-1]
            ver_summary = f"last_op={last.op.value}, status={last.status.value}, verification={last.verification.value}"

        # Record terminal goal outcome in bounded Goal Memory
        source = getattr(self._active_request, "source", "system") if self._active_request else "system"
        goal_text = self._active_request.goal_text if self._active_request else ""
        now_wall = time.time()
        self._goal_memory.record_outcome(
            goal_id=goal_id,
            session_id=session_id,
            source=source,
            goal_text=goal_text,
            outcome_state=status.value,
            created_at=now_wall - elapsed,
            completed_at=now_wall,
            result_summary=ver_summary or reason,
            clarification_count=self._clarification_count
        )


        if status == GoalExecutionState.COMPLETED:
            self._record_audit_event("GOAL_COMPLETED", {"goal_id": goal_id})
        elif status in (GoalExecutionState.FAILED, GoalExecutionState.CANNOT_PROCEED, GoalExecutionState.BUDGET_EXCEEDED):
            self._record_audit_event("GOAL_FAILED", {"goal_id": goal_id, "reason": reason[:100]})
        elif status == GoalExecutionState.BLOCKED:
            self._record_audit_event("GOAL_BLOCKED", {"goal_id": goal_id, "reason": reason[:100]})

        return GoalExecutionResult(
            goal_id=goal_id,
            status=status,
            turn_count=self._turn_count,
            clarification_count=self._clarification_count,
            elapsed_time_s=max(0.0, elapsed),
            reason=reason[:500],
            final_decision=final_decision,
            history=tuple(self._history),
            verification_summary=ver_summary,
            action_count=action_count
        )

    def _record_audit_event(self, event_type: str, details: Dict[str, Any]) -> None:
        safe_details = {k: str(v)[:200] for k, v in details.items()}
        self._audit_events.append({
            "timestamp": self._clock(),
            "event_type": event_type,
            "goal_id": self._active_request.goal_id if self._active_request else "",
            "details": safe_details
        })
