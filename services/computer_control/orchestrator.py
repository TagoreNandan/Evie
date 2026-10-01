"""
Deterministic multi-step orchestration (docs/07_Computer_Control.md Sections 4 and 6; CC-1a Step 6).

Pure sequencing - no OS access, no LLM. It drives the Step 1 ComputerControlSession (the authority
for resolution, the code-owned gate, limits, cancellation and DONE) and two ports supplied by the
caller: `observe(bundle_id)` and `act(request, utterance, allowed_apps)` (the Step 2-5 worker).

Per iteration, exactly ONE action:
  checkpoint -> OBSERVE (fresh; failure stops) -> context check -> CHOOSE next intent -> RESOLVE
  against THIS observation -> GATE (session.propose) -> FRESHNESS (request is for this observation)
  -> checkpoint -> authorize -> EXECUTE (one worker call) -> VERIFY (ActionResult) -> checkpoint
Only SUCCESS continues. FAILED / STALE / TIMEOUT stop (no retry); NOT_VERIFIABLE stops unless the
intent explicitly opts into continuing. When the plan is exhausted the goal is checked on a FRESH
observation: DONE_VERIFIED, DONE_UNVERIFIED (no goal), or BLOCKED (goal not met).

The plan is data: closed intents (operation + target selector + closed arguments). It cannot contain
callbacks, code, coordinates or AX objects, and it is never executed blindly.

CC-1a Step 11 adds `run_command`: the same loop driven by a CHOOSER instead of a plan. Per iteration:
  OBSERVE (fresh) -> CHOOSE (fast path) -> VALIDATE -> HARDEN (chooser_boundary) -> RESOLVE + GATE
  (session.propose) -> FRESHNESS -> EXECUTE (one worker call) -> VERIFY -> next iteration (OBSERVE)
ASK / BLOCKED / STOP never reach the resolver, gate or worker; DONE is a claim the session checks.
Both loops share one execution helper, so there is still exactly one worker call site.
"""

from typing import Any, Callable, Dict, FrozenSet, List, Literal, Optional, Tuple

from pydantic import Field, model_validator

from services.computer_control.actions import get_definition
from services.computer_control.audit import DecisionSource, record_for_decision, record_for_result
from services.computer_control.chooser import ValidatedAction
from services.computer_control.chooser_boundary import ChooserOutcome, FastPathChooser
from services.computer_control.models import (
    BUNDLE_ID_PATTERN,
    EvidenceValue,
    ActionRequest,
    AppIdentity,
    ActionResult,
    ActionStatus,
    Observation,
    Op,
    _Strict,
)
from services.computer_control.observer import ObserveResult
from services.computer_control.resolver import ResolutionOutcome, TargetSpec, resolve
from services.computer_control.session import ActiveSessionSlot, ComputerControlSession, SessionState
from services.computer_control.verification import AppFrontmostGoal, AppNotRunningGoal
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)

# Live-validated primitives that may be orchestrated in CC-1a. `focus` is implemented but has no live
# evidence (docs/07 Section 25), so it is deliberately excluded.
ORCHESTRATABLE = frozenset({Op.ACTIVATE_APP, Op.SET_TEXT, Op.SCROLL, Op.PRESS, Op.PRESS_KEY, Op.CLOSE_WINDOW, Op.QUIT_APP,
                            Op.SWITCH_APP, Op.SWITCH_WINDOW, Op.CLICK_ELEMENT, Op.COPY_SELECTION, Op.PASTE_CLIPBOARD,
                            Op.SELECT_TEXT})
QUIT_GOAL_OBSERVE_BUNDLE = "com.apple.finder"   # always running; never the app that was just quit
MAX_OBSERVE_ATTEMPTS = 3            # re-observation while the UI settles is not an action


class TargetSelector(_Strict):
    identifier: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=200)

    @model_validator(mode="after")
    def _has_key(self):
        if self.identifier is None and (self.role is None or (self.label is None and self.role != "AXTextArea"
                                                              and self.role != "AXScrollArea")):
            raise ValueError("selector needs an identifier, or role + label (text/scroll areas: role alone)")
        return self


class ActionIntent(_Strict):
    op: Op
    observe_bundle: str = Field(..., pattern=BUNDLE_ID_PATTERN)   # app to observe before choosing this intent
    target: Optional[TargetSelector] = None
    args: Dict[str, Any] = Field(default_factory=dict)             # validated against the op's closed schema
    continue_if_not_verifiable: bool = False                       # explicit, per-intent recovery policy

    @model_validator(mode="after")
    def _closed_args(self):
        model = get_definition(self.op).params_model
        if model is not None:
            model.model_validate(self.args)                         # unknown fields rejected
        elif self.args:
            raise ValueError("operation has no schema; arguments not allowed")
        return self


class Goal(_Strict):
    kind: Literal["frontmost_app"]
    bundle_id: str = Field(..., pattern=BUNDLE_ID_PATTERN)


class Plan(_Strict):
    utterance: str = Field(..., min_length=1, max_length=2000)    # set_text spans point into this
    intents: Tuple[ActionIntent, ...] = Field(..., min_length=1, max_length=40)
    final_observe_bundle: str = Field(..., pattern=BUNDLE_ID_PATTERN)
    goal: Optional[Goal] = None


def goal_check_for(plan: Plan):
    if plan.goal is None:
        return None
    return AppFrontmostGoal(plan.goal.bundle_id)


class Event(_Strict):
    kind: Literal["OBSERVE", "CHOOSE", "VALIDATE", "HARDEN", "RESOLVE", "GATE", "FRESHNESS", "EXECUTE", "VERIFY",
                  "GOAL", "STOP"]
    detail: str = Field("", max_length=200)
    step: Optional[int] = None


class StepReport(_Strict):
    intent: int
    op: Op
    obs_before: str
    request_step: Optional[int] = None
    gate: Optional[str] = Field(None, max_length=100)
    status: Optional[ActionStatus] = None
    verification: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=200)
    obs_after_worker: Optional[str] = None
    evidence: Dict[str, EvidenceValue] = Field(default_factory=dict, max_length=20)   # the ActionResult's evidence
    chooser: Optional[str] = Field(None, max_length=100)          # run_command: source/rule of the decision
    assessment: Optional[str] = Field(None, max_length=32)        # run_command: hardening's verdict
    findings: Tuple[str, ...] = Field(default=(), max_length=8)


class OrchestrationResult(_Strict):
    final_state: SessionState
    stop_reason: Optional[str] = Field(None, max_length=200)
    question: Optional[str] = Field(None, max_length=200)
    session_id: Optional[str] = Field(None, max_length=64)
    ask_reason: Optional[str] = Field(None, max_length=64)
    candidates: Tuple[int, ...] = ()
    steps: Tuple[StepReport, ...] = ()
    observations: Tuple[str, ...] = ()
    events: Tuple[Event, ...] = ()


Observe = Callable[[str], ObserveResult]
Act = Callable[[ActionRequest, str, FrozenSet[str]], ActionResult]


class Orchestrator:
    def __init__(self, session: ComputerControlSession, observe: Observe, act: Act, *,
                 checkpoint: Callable[[str], None] = lambda name: None, step_log: Any = None):
        self.session, self._observe, self._act, self._checkpoint_hook = session, observe, act, checkpoint
        # Audit (computer_control_steps): ONE redacted record per attempted decision. Observational only; if the
        # log is unavailable nothing is dispatched, and a failed write halts the session (fail-safe).
        self._step_log, self._log_step, self._source, self.audit_failures = step_log, 0, "plan", 0
        self.events: List[Event] = []
        self.steps: List[StepReport] = []
        self.observations: List[str] = []

    # --- helpers ---

    def _event(self, kind: str, detail: str = "", step: Optional[int] = None) -> None:
        self.events.append(Event(kind=kind, detail=detail[:200], step=step))

    def _stopped(self, where: str) -> bool:
        """Cancellation (or any terminal state) is checked at every checkpoint."""
        self._checkpoint_hook(where)
        if self.session.is_terminal:
            self._event("STOP", f"{self.session.state.value} at {where}: {self.session.stop_reason}")
            return True
        return False

    def _result(self) -> OrchestrationResult:
        s = self.session
        return OrchestrationResult(final_state=s.state, stop_reason=s.stop_reason, question=s.question,
                                   session_id=s.session_id, ask_reason=s.ask_reason if s.state is SessionState.ASKING
                                   else None, candidates=s.candidates if s.state is SessionState.ASKING else (),
                                   steps=tuple(self.steps),
                                   observations=tuple(self.observations), events=tuple(self.events))

    def _fresh_observation(self, bundle_id: str) -> Optional[Observation]:
        """A NEW observation accepted by the session, or None (session then terminal). Never reuses the old one."""
        for _ in range(MAX_OBSERVE_ATTEMPTS):
            if self._stopped("before_observe"):
                return None
            result = self._observe(bundle_id)
            obs = result.observation
            if result.status != "OK" or obs is None:
                self._event("OBSERVE", f"FAILED {result.status}: {result.reason}")
                self.session.fail(f"OBSERVATION_FAILED:{result.status}")
                self._stopped("after_observe")
                return None
            if obs.obs_id in self.observations:
                self.session.fail("OBSERVATION_NOT_FRESH")
                self._stopped("after_observe")
                return None
            self.observations.append(obs.obs_id)
            self._event("OBSERVE", f"{obs.obs_id} app={obs.app.bundle_id} "
                                   f"frontmost={obs.frontmost.bundle_id if obs.frontmost else None}")
            state = self.session.observe(obs)
            if self._stopped("after_observe"):
                return None
            if state is SessionState.CHOOSING:
                return obs
        self.session.fail("OBSERVATION_NOT_SETTLED")
        self._stopped("after_observe")
        return None

    def _ask(self, question: str, obs: Observation, reason: Optional[str] = None,
             candidates: Tuple[int, ...] = ()) -> None:
        raw: Dict[str, Any] = {"status": "ASK", "question": question, "candidates": list(candidates)[:10]}
        if reason:
            raw["reason"] = reason
        self._propose(raw, obs)

    def _block(self, reason: str, obs: Observation) -> None:
        self._propose({"status": "BLOCKED", "reason": reason}, obs)

    # --- audit (computer_control_steps) ---

    def _audit(self, build) -> None:
        if self._step_log is None:
            return
        self._log_step += 1
        try:
            self._step_log.write(build(self._log_step))
        except Exception:
            self.audit_failures += 1
            if not self.session.is_terminal:
                self.session.halt("AUDIT_LOG_WRITE_FAILED")        # no further action without an audit trail

    def _audit_ready(self) -> bool:
        if self._step_log is None:
            return True
        try:
            return bool(self._step_log.ready())
        except Exception:
            return False

    def _audit_decision(self, raw: Any, obs: Observation, op_override: Optional[str] = None) -> None:
        s = self.session
        raw = raw if isinstance(raw, dict) else {}
        op = op_override or (raw.get("op") if raw.get("status") == "ACTION" else None)
        op = op if op in {o.value for o in Op} else (None if op is None else "INVALID")
        idx, args = raw.get("target_index"), raw.get("args") if isinstance(raw.get("args"), dict) else {}
        target = obs.targets[idx] if isinstance(idx, int) and 0 <= idx < len(obs.targets) else None
        app = obs.app
        if op == Op.ACTIVATE_APP.value and isinstance(args.get("bundle_id"), str):
            try:
                app = AppIdentity(bundle_id=args["bundle_id"], pid=0)
            except Exception:
                app = obs.app
        key = args.get("key") if op == Op.PRESS_KEY.value and args.get("key") in ("RIGHT_ARROW", "LEFT_ARROW") \
            else None
        reason = s.stop_reason if s.is_terminal else (s.history[-1].reason if s.history else None)
        self._audit(lambda n: record_for_decision(s.session_id, n, self._source, op, target, app, s.state.value,
                                                  reason, key))

    def _propose(self, raw: Any, obs: Observation):
        """session.propose + exactly one audit record when the decision is NOT dispatched to the worker."""
        request = self.session.propose(raw, obs.obs_id)
        if request is None:
            self._audit_decision(raw, obs)
        return request

    # --- the loop ---

    def run(self, plan: Plan) -> OrchestrationResult:
        s = self.session
        if s.state is not SessionState.OBSERVING:
            raise ValueError(f"session must be started and observing, not {s.state.value}")
        if plan.utterance != s.utterance:
            raise ValueError("plan utterance must be the session's verbatim utterance")
        self._source = "plan"
        expected_front: Optional[str] = None

        for index, intent in enumerate(plan.intents):
            obs = self._fresh_observation(intent.observe_bundle)
            if obs is None:
                return self._result()
            # Context check: after our own action the frontmost app must be what we expect.
            if expected_front is not None:
                actual = obs.frontmost.bundle_id if obs.frontmost else None
                if actual != expected_front:
                    self._event("STOP", f"USER_TAKEOVER expected={expected_front} actual={actual}", index)
                    self._ask("The active app changed unexpectedly. Should I continue?", obs, "USER_TAKEOVER")
                    return self._result()

            if self._stopped("before_choose"):
                return self._result()
            self._event("CHOOSE", f"intent {index}: {intent.op.value}", index)
            if self._stopped("after_choose"):
                return self._result()
            report = StepReport(intent=index, op=intent.op, obs_before=obs.obs_id)

            if intent.op not in ORCHESTRATABLE:
                self._event("GATE", f"BLOCK UNSUPPORTED_OPERATION {intent.op.value}", index)
                self._block(f"UNSUPPORTED_OPERATION:{intent.op.value}", obs)
                self.steps.append(report.model_copy(update={"gate": "BLOCK:UNSUPPORTED_OPERATION"}))
                return self._result()

            decision: Dict[str, Any] = {"status": "ACTION", "op": intent.op.value, "args": dict(intent.args)}
            if get_definition(intent.op).requires_target:
                if intent.target is None:
                    self._block("TARGET_SELECTOR_REQUIRED", obs)
                    return self._result()
                outcome = self._resolve(obs, intent.target)
                self._event("RESOLVE", f"{outcome.kind} {outcome.reason} obs={obs.obs_id} "
                                       f"index={outcome.target.target.index if outcome.target else None}", index)
                if outcome.kind != "RESOLVED":
                    if outcome.kind == "ASK":
                        self._ask("More than one control matches. Which one?", obs, outcome.reason, outcome.candidates)
                    else:
                        self._block(f"RESOLVE_{outcome.kind}:{outcome.reason}", obs)
                    self.steps.append(report.model_copy(update={"reason": outcome.reason}))
                    return self._result()
                decision["target_index"] = outcome.target.target.index
            else:
                self._event("RESOLVE", f"APP {intent.args.get('bundle_id')} obs={obs.obs_id}", index)

            if self._stopped("before_gate"):
                return self._result()
            request = self._propose(decision, obs)             # validates, re-resolves, runs the code-owned gate
            if request is None:
                self._event("GATE", f"NOT_ALLOWED {s.state.value}: {s.stop_reason}", index)
                if not s.is_terminal:
                    s.halt(f"NO_ACTION_APPROVED:{s.history[-1].reason if s.history else ''}")
                self.steps.append(report.model_copy(update={"gate": s.stop_reason}))
                self._stopped("after_gate")
                return self._result()
            result = self._execute(request, obs, index, report)
            if result is None:
                return self._result()

            if result.status is ActionStatus.SUCCESS:
                expected_front = intent.args["bundle_id"] if intent.op is Op.ACTIVATE_APP else \
                    (obs.frontmost.bundle_id if obs.frontmost else None)
                continue
            if result.status is ActionStatus.NOT_VERIFIABLE and intent.continue_if_not_verifiable:
                expected_front = None                           # context unknown: the next observe decides
                continue
            s.halt(f"STOPPED_AFTER_{result.status.value}")      # FAILED / STALE / TIMEOUT / NOT_VERIFIABLE
            self._stopped("after_result")
            return self._result()

        # Plan exhausted: verify the goal on a FRESH observation (never "the last action succeeded").
        obs = self._fresh_observation(plan.final_observe_bundle)
        if obs is None:
            return self._result()
        if expected_front is not None and (obs.frontmost.bundle_id if obs.frontmost else None) != expected_front:
            self._event("STOP", f"USER_TAKEOVER before goal check expected={expected_front}")
            self._ask("The active app changed unexpectedly. Should I continue?", obs)
            return self._result()
        self._propose({"status": "DONE"}, obs)
        self._event("GOAL", f"{s.state.value}: {s.stop_reason if s.is_terminal else 'GOAL_NOT_MET'}")
        if not s.is_terminal:                                   # DONE claim not met: no further steps exist
            s.halt("GOAL_NOT_MET")
            self._stopped("after_goal")
        return self._result()

    def _execute(self, request: ActionRequest, obs: Observation, index: int,
                 report: StepReport) -> Optional[ActionResult]:
        """GATE (already approved) -> FRESHNESS -> checkpoint -> authorize -> EXECUTE (the ONLY worker call)
        -> VERIFY (the ActionResult) -> record -> checkpoint. None = the session stopped; never retried here."""
        s = self.session
        gate = f"{request.gate.outcome.value}:{request.gate.risk.value}:{request.gate.reason}"
        self._event("GATE", gate, index)
        fresh = request.obs_id == obs.obs_id == s.observation.obs_id and (
            request.target is None or request.target.obs_id == obs.obs_id)
        self._event("FRESHNESS", f"{'OK' if fresh else 'FAILED'} obs={request.obs_id}", index)
        target = request.target.target if request.target is not None else None
        key = getattr(request.args, "key", None)
        activated = getattr(request.args, "bundle_id", None)
        app = AppIdentity(bundle_id=activated, pid=0) if activated else obs.app

        def not_dispatched() -> None:
            self._audit(lambda n: record_for_decision(s.session_id, n, self._source, request.op.value, target, app,
                                                      s.state.value, s.stop_reason, key))

        if not fresh:                                       # defensive: the session guarantees this
            s.halt("FRESHNESS_CHECK_FAILED")
            not_dispatched()
            return None
        if self._stopped("before_execute"):
            not_dispatched()
            return None
        if not self._audit_ready():                          # no audit trail -> no action (fail-safe)
            s.halt("AUDIT_LOG_UNAVAILABLE")
            self._event("STOP", "AUDIT_LOG_UNAVAILABLE", index)
            return None
        if not s.authorize(request):                        # final session-side cancel/limit check
            self._event("STOP", f"NOT_AUTHORIZED {s.state.value}: {s.stop_reason}", index)
            not_dispatched()
            return None
        self._event("EXECUTE", f"{request.op.value} request_step={request.step}", index)
        try:
            result = self._act(request, s.utterance, s.allowed_apps)
        except Exception as e:                              # worker exited / timed out: outcome unknown
            detail = getattr(e, "worker_error", None)              # the worker's own exception type, if reported
            s.fail(f"ACT_OUTCOME_UNKNOWN:{type(e).__name__}" + (f":{detail}"[:80] if detail else ""))
            self._audit(lambda n: record_for_decision(s.session_id, n, self._source, request.op.value, target, app,
                                                      s.state.value, s.stop_reason, key).model_copy(
                update={"execution": None, "verification": None}))           # outcome genuinely unknown
            self._stopped("after_execute")
            return None
        self._event("VERIFY", f"{result.status.value} {result.verification.value} "
                              f"{result.reason or ''}".strip(), index)
        self.steps.append(report.model_copy(update={
            "request_step": request.step, "gate": gate, "status": result.status,
            "verification": result.verification.value, "reason": result.reason,
            "obs_after_worker": result.evidence.get("obs_after") if result.evidence else None,
            "evidence": dict(result.evidence)}))
        s.record_result(result)
        self._audit(lambda n: record_for_result(request, result, self._source, n, obs.app))
        if self._stopped("after_execute"):
            return None
        return result

    # --- CC-1a Step 11: chooser-driven session ---

    def run_command(self, chooser: "CommandChooser", observe_bundle: str) -> OrchestrationResult:
        """
        Carry out the session's utterance with a chooser (CC-1a: FastPathChooser only), one action per
        iteration, each on a fresh observation of `observe_bundle`. Only a verified SUCCESS continues, and
        the next choice is made on a new observation; anything else stops without a retry.
        """
        s = self.session
        if s.state is not SessionState.OBSERVING:
            raise ValueError(f"session must be started and observing, not {s.state.value}")
        expected_front: Optional[str] = None
        iteration = 0
        while True:
            if self._stopped("before_next_iteration"):
                return self._result()
            obs = self._fresh_observation(observe_bundle)
            if obs is None:
                return self._result()
            if expected_front is not None and (obs.frontmost.bundle_id if obs.frontmost else None) != expected_front:
                self._event("STOP", f"USER_TAKEOVER expected={expected_front} "
                                    f"actual={obs.frontmost.bundle_id if obs.frontmost else None}", iteration)
                self._ask("The active app changed unexpectedly. Should I continue?", obs, "USER_TAKEOVER")
                return self._result()

            iteration += 1
            if self._stopped("before_choose"):
                return self._result()
            outcome = chooser.choose(s.utterance, obs, recent=tuple(s.recent), step=iteration,
                                     allowed_apps=tuple(sorted(s.allowed_apps)))
            self._chosen_events(outcome, iteration)
            self._source = "chooser" if outcome.source == "provider" else "fast_path"
            if self._stopped("after_choose"):
                return self._result()
            if outcome.stop:
                s.cancel("USER_STOP")
                self._audit_decision({}, obs)                   # STOP: one CANCELLED record, no op
                self._stopped("after_choose")
                return self._result()

            raw = outcome.as_decision()
            if raw["status"] != "ACTION":                       # ASK / BLOCKED / DONE: never resolved, gated or run
                self._propose(raw, obs)
                if raw["status"] == "DONE":
                    self._event("GOAL", f"{s.state.value}: {s.stop_reason if s.is_terminal else 'GOAL_NOT_MET'}")
                if not s.is_terminal:                           # e.g. DONE claim not met: nothing else to do
                    s.halt("GOAL_NOT_MET" if raw["status"] == "DONE" else "NO_ACTION_APPROVED")
                self._stopped("after_decision")
                return self._result()

            op = Op(raw["op"])
            report = StepReport(intent=iteration, op=op, obs_before=obs.obs_id,
                                chooser=f"{outcome.source}/{outcome.rule}", assessment=outcome.assessment,
                                findings=outcome.findings)
            if op not in ORCHESTRATABLE:                        # defensive: the boundary allows CHOOSER_OPS only
                self._event("GATE", f"BLOCK UNSUPPORTED_OPERATION {op.value}", iteration)
                self._block(f"UNSUPPORTED_OPERATION:{op.value}", obs)
                return self._result()
            if op is Op.ACTIVATE_APP and s.goal_check is None:
                # The goal comes from the deterministic grammar's own decision (code, not a model): the named
                # app must be frontmost on a FRESH observation before DONE can be verified.
                s.goal_check = AppFrontmostGoal(raw["args"]["bundle_id"])
            if op is Op.QUIT_APP and s.goal_check is None and raw["args"].get("bundle_id"):
                s.goal_check = AppNotRunningGoal(raw["args"]["bundle_id"])

            if self._stopped("before_gate"):
                return self._result()
            mark = len(s.history)
            request = self._propose(raw, obs)                   # re-validates, resolves, runs the code-owned gate
            new = s.history[mark:]
            resolved = [r for r in new if r.event == "resolve"]
            if resolved:
                self._event("RESOLVE", f"{resolved[-1].reason or 'RESOLVED'} obs={obs.obs_id} "
                                       f"index={raw.get('target_index')}", iteration)
            if request is None:
                if any(r.event == "gate" for r in new):
                    self._event("GATE", f"NOT_ALLOWED {s.state.value}: {s.stop_reason}", iteration)
                if not s.is_terminal:
                    s.halt(f"NO_ACTION_APPROVED:{s.history[-1].reason if s.history else ''}")
                self.steps.append(report.model_copy(update={"gate": s.stop_reason}))
                self._stopped("after_gate")
                return self._result()

            result = self._execute(request, obs, iteration, report)
            if result is None:
                return self._result()
            if result.status is not ActionStatus.SUCCESS:       # FAILED / STALE / TIMEOUT / NOT_VERIFIABLE
                s.halt(f"STOPPED_AFTER_{result.status.value}")  # no retry, no alternative mechanism
                self._stopped("after_result")
                return self._result()
            expected_front = raw["args"]["bundle_id"] if op is Op.ACTIVATE_APP else \
                (obs.frontmost.bundle_id if obs.frontmost else None)
            if op is Op.QUIT_APP:
                # The quit app cannot be observed any more and macOS picks the next frontmost app itself: the
                # goal is checked on a fresh observation of an always-running app instead.
                observe_bundle, expected_front = QUIT_GOAL_OBSERVE_BUNDLE, None

    def _chosen_events(self, outcome: ChooserOutcome, iteration: int) -> None:
        d = outcome.decision
        kind = "STOP" if outcome.stop else ("NONE" if d is None else ("ACTION" if isinstance(d, ValidatedAction)
                                                                       else d.status))
        extra = ""
        if outcome.decision is not None and kind == "ACTION":
            extra = f" {outcome.decision.op.value} index={outcome.decision.target_index}"
        elif outcome.decision is not None and kind in ("ASK", "BLOCKED"):
            extra = f" {outcome.decision.reason}"
        self._event("CHOOSE", f"{outcome.source}/{outcome.rule} {kind}{extra}", iteration)
        if outcome.stop:
            return
        self._event("VALIDATE", "OK" if outcome.error is None else f"INVALID {outcome.error}", iteration)
        if outcome.assessment is not None:
            self._event("HARDEN", f"{outcome.assessment} {','.join(outcome.findings)}".strip(), iteration)

    @staticmethod
    def _resolve(obs: Observation, sel: TargetSelector) -> ResolutionOutcome:
        """Resolve against THIS observation only. Role-only selectors (a text/scroll area) need a unique match."""
        if sel.identifier is not None or sel.label is not None:
            return resolve(obs, TargetSpec(obs_id=obs.obs_id, identifier=sel.identifier, role=sel.role,
                                           subrole=sel.subrole, label=sel.label))
        hits = [t.index for t in obs.targets if t.role == sel.role and t.subrole == sel.subrole]
        if not hits:
            return ResolutionOutcome(kind="BLOCKED", reason="NO_MATCH")
        if len(hits) > 1:
            return ResolutionOutcome(kind="ASK", reason="AMBIGUOUS_TARGET", candidates=tuple(hits))
        return resolve(obs, TargetSpec(obs_id=obs.obs_id, role=sel.role, subrole=sel.subrole, index=hits[0]))


class CommandChooser:
    """What run_command needs from a chooser (FastPathChooser in CC-1a; see chooser_boundary)."""
    def choose(self, utterance: str, observation: Observation, *, recent=(), step: int = 1,
               allowed_apps=()) -> ChooserOutcome: ...


SUCCESSFUL_ENDS = frozenset({SessionState.DONE_VERIFIED, SessionState.DONE_UNVERIFIED})


def run_commands(commands: "List[Tuple[str, str]]", start_session: Callable[[str], ComputerControlSession],
                 observe: Observe, act: Act, *, slot: ActiveSessionSlot, chooser: Optional[CommandChooser] = None,
                 checkpoint: Callable[[str], None] = lambda name: None) -> Tuple[OrchestrationResult, ...]:
    """
    Several commands in order, each (utterance, observe_bundle) in its OWN started session, one active at a
    time (the slot never preempts). The sequence stops at the first command that does not end DONE: no retry,
    no skipping ahead.
    """
    results: List[OrchestrationResult] = []
    for utterance, observe_bundle in commands:
        session = start_session(utterance)
        slot.claim(session)                                     # SessionConflict if another session is active
        try:
            result = Orchestrator(session, observe, act, checkpoint=checkpoint).run_command(
                chooser or FastPathChooser(), observe_bundle)
        finally:
            slot.release(session)
        results.append(result)
        if result.final_state not in SUCCESSFUL_ENDS:
            break
    return tuple(results)
