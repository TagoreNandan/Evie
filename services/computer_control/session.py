"""
Pure ComputerControlSession state machine (docs/07_Computer_Control.md Section 6).

The session is driven from outside (later: an orchestrator that talks to the worker). It never
touches the OS, sleeps, or starts threads; time comes from an injected clock, so every limit is
deterministic in tests. Per iteration:

    observe(obs)            OBSERVING -> CHOOSING
    propose(decision, id)   CHOOSING -> RESOLVING -> GATING -> EXECUTING (returns an ActionRequest)
    authorize(request)      final pre-execution check (cancellation, limits), called right before dispatch
    record_result(result)   EXECUTING -> VERIFYING -> OBSERVING (or a terminal state)

Terminal states: DONE_VERIFIED, DONE_UNVERIFIED, BLOCKED, ASKING, CANCELLED, LIMIT_REACHED, ERROR.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, FrozenSet, List, Optional, Protocol, Sequence, Set, Tuple

from pydantic import Field

from services.computer_control.actions import get_definition
from services.computer_control.chooser import (
    MAX_RECENT_ACTIONS,
    AskDecision,
    BlockedDecision,
    DoneDecision,
    RecentAction,
    ValidatedAction,
    validate_decision,
)
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy, context_for, evaluate
from services.computer_control.models import (
    MIN_ACTION_TIMEOUT_S,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ExecutionStatus,
    GateOutcome,
    Observation,
    Op,
    PermissionStatus,
    SettleStatus,
    _Strict,
)
from services.computer_control.resolver import TargetSpec, resolve
from services.computer_control.verification import GoalCheck


class SessionState(str, Enum):
    IDLE = "IDLE"
    OBSERVING = "OBSERVING"
    CHOOSING = "CHOOSING"
    RESOLVING = "RESOLVING"
    GATING = "GATING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    DONE_VERIFIED = "DONE_VERIFIED"
    DONE_UNVERIFIED = "DONE_UNVERIFIED"
    BLOCKED = "BLOCKED"
    ASKING = "ASKING"
    CANCELLED = "CANCELLED"
    LIMIT_REACHED = "LIMIT_REACHED"
    ERROR = "ERROR"


TERMINAL_STATES = frozenset({
    SessionState.DONE_VERIFIED, SessionState.DONE_UNVERIFIED, SessionState.BLOCKED, SessionState.ASKING,
    SessionState.CANCELLED, SessionState.LIMIT_REACHED, SessionState.ERROR,
})


class Clock(Protocol):
    def now(self) -> float: ...


@dataclass(frozen=True)
class SessionLimits:
    max_actions: int = 40
    max_consecutive_noops: int = 3
    wall_clock_s: float = 180.0
    per_action_timeout_s: float = 10.0     # value is an open decision (docs/07 Section 21); hook only
    token_budget: int = 0                  # CC-1a makes no model calls
    cost_budget_usd: float = 0.0

    def __post_init__(self):
        if self.per_action_timeout_s <= MIN_ACTION_TIMEOUT_S:
            raise ValueError("per-action timeout must exceed the AX messaging timeout plus the settle bound")
        if self.max_actions < 1 or self.max_consecutive_noops < 1 or self.wall_clock_s <= 0:
            raise ValueError("session limits must be positive")


class StepRecord(_Strict):
    at: float
    event: str = Field(..., max_length=32)
    state: SessionState
    reason: Optional[str] = Field(None, max_length=200)
    op: Optional[Op] = None
    step: Optional[int] = None


class SessionError(RuntimeError):
    """A caller drove the state machine out of order (a programming error, not a user outcome)."""


class SessionConflict(RuntimeError):
    """Another computer-control session is active and preemption was not requested."""


class ComputerControlSession:
    def __init__(self, session_id: str, utterance: str, allowed_apps: FrozenSet[str], *, clock: Clock,
                 limits: SessionLimits = SessionLimits(), policy: GatePolicy = DEFAULT_POLICY,
                 goal_check: Optional[GoalCheck] = None, daily_cap_ok: Callable[[], bool] = lambda: True,
                 installed_apps: Sequence[Any] = ()):
        if not session_id or not utterance or not allowed_apps:
            raise ValueError("session_id, utterance and allowed_apps are required")
        self.session_id = session_id
        self.utterance = utterance
        self.allowed_apps: FrozenSet[str] = frozenset(allowed_apps)   # fixed for the session's lifetime
        self.clock = clock
        self.limits = limits
        self.policy = policy
        self.goal_check = goal_check
        self.daily_cap_ok = daily_cap_ok
        self.installed_apps = installed_apps

        self.state = SessionState.IDLE
        self.stop_reason: Optional[str] = None
        self.question: Optional[str] = None
        self.ask_reason: Optional[str] = None          # stable reason code of an ASK (chooser, resolver or gate)
        self.candidates: Tuple[int, ...] = ()           # target indexes offered with an ASK, when known
        self.recent: List[RecentAction] = []           # bounded action summaries (op/status only; never text)
        self.cancel_epoch = 0
        self.attempts = 0                  # executions authorized + stale attempts; bounded by max_actions
        self.consecutive_noops = 0
        self.tokens_used = 0
        self.cost_used_usd = 0.0
        self.started_at: Optional[float] = None
        self.observation: Optional[Observation] = None
        self.pending: Optional[ActionRequest] = None
        self.history: List[StepRecord] = []
        self._cancelled = False
        self._evie_acted_since_observation = False
        self._failed: Set[Tuple] = set()
        self._failure_candidate: Optional[Tuple] = None

    # --- helpers ---

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def _record(self, event: str, reason: Optional[str] = None, op: Optional[Op] = None,
                step: Optional[int] = None) -> None:
        self.history.append(StepRecord(at=self.clock.now(), event=event, state=self.state, reason=reason,
                                       op=op, step=step))

    def _to(self, state: SessionState, event: str, reason: Optional[str] = None, **kw) -> SessionState:
        self.state = state
        if state in TERMINAL_STATES and self.stop_reason is None:
            self.stop_reason = reason or event
        self._record(event, reason, **kw)
        return state

    def _require(self, *states: SessionState) -> None:
        if self.state not in states:
            raise SessionError(f"invalid in state {self.state.value}; expected {[s.value for s in states]}")

    def _limit_hit(self) -> Optional[str]:
        if self.started_at is not None and self.clock.now() - self.started_at >= self.limits.wall_clock_s:
            return "WALL_CLOCK_LIMIT"
        if self.attempts >= self.limits.max_actions:
            return "ACTION_LIMIT"
        if self.consecutive_noops >= self.limits.max_consecutive_noops:
            return "NOOP_LIMIT"
        return None

    def _check_limits(self) -> bool:
        reason = self._limit_hit()
        if reason:
            self._to(SessionState.LIMIT_REACHED, "limit", reason)
            return True
        return False

    def _noop(self, reason: str, op: Optional[Op] = None) -> None:
        self.consecutive_noops += 1
        self._record("noop", reason, op=op)
        self._check_limits()

    @staticmethod
    def _failure_key(op: Op, target_key: Any, args: Any, fingerprint: str) -> Tuple:
        return (op, target_key, args.model_dump_json(), fingerprint)

    # --- lifecycle ---

    def start(self, permission: PermissionStatus) -> SessionState:
        self._require(SessionState.IDLE)
        self.started_at = self.clock.now()
        if not permission.ok:
            return self._to(SessionState.ERROR, "start", "PERMISSION_UNAVAILABLE")
        return self._to(SessionState.OBSERVING, "start")

    def cancel(self, reason: str = "USER_STOP") -> int:
        """Stop before the next action. Bumps the epoch the worker checks right before its AX call."""
        self._cancelled = True
        self.cancel_epoch += 1
        if not self.is_terminal:
            self._to(SessionState.CANCELLED, "cancel", reason)
        return self.cancel_epoch

    def is_epoch_current(self, epoch: int) -> bool:
        """Worker-side check immediately before the AX call."""
        return not self._cancelled and epoch == self.cancel_epoch and self.state is SessionState.EXECUTING

    def halt(self, reason: str) -> SessionState:
        """Stop a non-terminal session as BLOCKED (e.g. after FAILED/STALE: no automatic retry)."""
        if self.is_terminal:
            return self.state
        self.pending = None
        return self._to(SessionState.BLOCKED, "halt", reason)

    def fail(self, reason: str) -> SessionState:
        """Fail closed (e.g. permission lost, worker unavailable)."""
        if self.is_terminal:
            return self.state
        self.pending = None
        return self._to(SessionState.ERROR, "error", reason)

    # --- budget hooks ---

    def can_call_model(self) -> bool:
        return (not self.is_terminal and self.daily_cap_ok() and self.tokens_used < self.limits.token_budget
                and self.cost_used_usd < self.limits.cost_budget_usd)

    def budget_remaining(self) -> int:
        return max(0, self.limits.token_budget - self.tokens_used)

    def record_model_usage(self, tokens_in: int, tokens_out: int, cost_usd: float) -> SessionState:
        self.tokens_used += max(0, tokens_in) + max(0, tokens_out)
        self.cost_used_usd += max(0.0, cost_usd)
        if not self.is_terminal and (self.tokens_used > self.limits.token_budget
                                     or self.cost_used_usd > self.limits.cost_budget_usd):
            self._to(SessionState.LIMIT_REACHED, "limit", "MODEL_BUDGET")
        return self.state

    # --- loop ---

    def observe(self, observation: Observation) -> SessionState:
        if self.is_terminal:
            return self.state
        self._require(SessionState.OBSERVING, SessionState.CHOOSING)
        if self._check_limits():
            return self.state
        prev = self.observation
        if prev is not None and observation.taken_at < prev.taken_at:
            self._record("observe", "OUT_OF_ORDER_OBSERVATION")
            return self.state
        if prev is not None and not self._evie_acted_since_observation and (
                (observation.frontmost.bundle_id if observation.frontmost else None)
                != (prev.frontmost.bundle_id if prev.frontmost else None)
                or (observation.window.identity() if observation.window else None)
                != (prev.window.identity() if prev.window else None)):
            self.observation = observation
            return self._to(SessionState.ASKING, "observe", "USER_TAKEOVER")
        if observation.settle.status is not SettleStatus.SETTLED:
            self._record("observe", f"NOT_SETTLED:{observation.settle.status.value}")
            return self._to(SessionState.OBSERVING, "observe", "AWAIT_SETTLED_OBSERVATION")
        if observation.degraded:
            self.observation = observation
            return self._to(SessionState.BLOCKED, "observe", "OBSERVATION_DEGRADED")
        self.observation = observation
        self._evie_acted_since_observation = False
        return self._to(SessionState.CHOOSING, "observe")

    def propose(self, raw_decision: Any, obs_id: str) -> Optional[ActionRequest]:
        """Validate one decision (chooser or fast path), resolve, gate. Returns a request only if ALLOWed."""
        if self.is_terminal:             # e.g. cancelled while the chooser was running
            self._record("propose", "IGNORED_AFTER_TERMINAL")
            return None
        self._require(SessionState.CHOOSING)
        if self._check_limits():
            return None
        obs = self.observation
        if obs_id != obs.obs_id:
            self.attempts += 1
            self._record("propose", "DECISION_FOR_STALE_OBSERVATION")
            self._check_limits()
            return None

        checked = validate_decision(raw_decision, utterance=self.utterance, observation=obs, installed_apps=self.installed_apps)
        if not checked.ok:
            self._noop(f"REJECTED_OUTPUT:{checked.error}")
            return None
        decision = checked.decision
        if isinstance(decision, DoneDecision):
            met = self.goal_check.check(obs) if self.goal_check is not None else None
            if met is True:
                return self._done(SessionState.DONE_VERIFIED, "GOAL_VERIFIED")
            if met is None:
                return self._done(SessionState.DONE_UNVERIFIED, "NO_GOAL_VERIFIER")
            self._noop("DONE_CLAIM_NOT_MET")
            return None
        if isinstance(decision, BlockedDecision):
            self._to(SessionState.BLOCKED, "propose", f"CHOOSER_BLOCKED:{decision.reason}")
            return None
        if isinstance(decision, AskDecision):
            self.question, self.ask_reason, self.candidates = decision.question, decision.reason, decision.candidates
            self._to(SessionState.ASKING, "propose", f"CHOOSER_ASK:{decision.reason}" if decision.reason
                     else "CHOOSER_ASK")
            return None
        return self._resolve_and_gate(decision, obs)

    def _done(self, state: SessionState, reason: str) -> None:
        self._to(state, "done", reason)
        return None

    def _resolve_and_gate(self, action: ValidatedAction, obs: Observation) -> Optional[ActionRequest]:
        self._to(SessionState.RESOLVING, "resolve", op=action.op)
        target = None
        if get_definition(action.op).requires_target:
            outcome = resolve(obs, TargetSpec(obs_id=obs.obs_id, index=action.target_index))
            if outcome.kind == "STALE":
                self.attempts += 1
                self._to(SessionState.OBSERVING, "resolve", outcome.reason, op=action.op)
                self._check_limits()
                return None
            if outcome.kind == "ASK":
                self.ask_reason, self.candidates = outcome.reason, outcome.candidates
                self._to(SessionState.ASKING, "resolve", outcome.reason, op=action.op)
                return None
            if outcome.kind == "BLOCKED":
                self._to(SessionState.BLOCKED, "resolve", outcome.reason, op=action.op)
                return None
            target = outcome.target

        key = self._failure_key(action.op, target.target.stable_key() if target else None, action.args,
                                obs.fingerprint)
        if key in self._failed:
            self._to(SessionState.CHOOSING, "resolve", "REPEAT_OF_FAILED_ACTION", op=action.op)
            self._noop("REPEAT_OF_FAILED_ACTION", op=action.op)
            return None

        self._to(SessionState.GATING, "gate", op=action.op)
        gate = evaluate(context_for(action.op, action.args, target, obs, self.allowed_apps), self.policy)
        if gate.outcome is GateOutcome.BLOCK:
            self._to(SessionState.BLOCKED, "gate", f"{gate.risk.value}:{gate.reason}", op=action.op)
            return None
        if gate.outcome is GateOutcome.ASK:
            self.ask_reason = gate.reason[:64] if gate.reason else None
            self._to(SessionState.ASKING, "gate", gate.reason, op=action.op)
            return None

        request = ActionRequest(
            session_id=self.session_id, step=self.attempts + 1, op=action.op, target=target, args=action.args,
            obs_id=obs.obs_id, gate=gate, cancel_epoch=self.cancel_epoch, timeout_s=self.limits.per_action_timeout_s,
        )
        self.pending = request
        self._failure_candidate = key
        self._to(SessionState.EXECUTING, "approved", op=action.op, step=request.step)
        return request

    def authorize(self, request: ActionRequest) -> bool:
        """Final check immediately before dispatch: cancellation, epoch, limits, daily cap."""
        if self.state is not SessionState.EXECUTING or self.pending is not request:
            return False
        if self._cancelled or request.cancel_epoch != self.cancel_epoch:
            return False
        if self._check_limits():
            self.pending = None
            return False
        if not self.daily_cap_ok():
            self.pending = None
            self._to(SessionState.LIMIT_REACHED, "limit", "DAILY_SPEND_CAP")
            return False
        self.attempts += 1
        self._record("dispatch", op=request.op, step=request.step)
        return True

    def record_result(self, result: ActionResult) -> SessionState:
        if self.pending is None or result.step != self.pending.step or result.session_id != self.session_id \
                or result.op is not self.pending.op:
            raise SessionError("result does not match the pending request")
        target = self.pending.target
        self.pending = None
        self._evie_acted_since_observation = result.execution is not ExecutionStatus.NOT_EXECUTED
        self.recent = (self.recent + [RecentAction(
            step=result.step, op=result.op, status=result.status.value,
            summary=f"{result.op.value} target={target.target.index if target else None} "
                    f"verification={result.verification.value}")])[-MAX_RECENT_ACTIONS:]
        if self.is_terminal:                       # cancelled while the action was in flight: log only
            self._record("result", f"AFTER_TERMINAL:{result.status.value}", op=result.op, step=result.step)
            return self.state
        self._to(SessionState.VERIFYING, "result", result.status.value, op=result.op, step=result.step)

        if result.status is ActionStatus.CANCELLED:
            return self._to(SessionState.CANCELLED, "result", "CANCELLED_BY_WORKER")
        if result.status is ActionStatus.STALE:
            self._to(SessionState.OBSERVING, "result", "STALE_REOBSERVE")
            self._check_limits()
            return self.state
        if result.status is ActionStatus.BLOCKED:
            return self._to(SessionState.BLOCKED, "result", result.reason or "BLOCKED_BY_WORKER")

        if result.noop:
            self.consecutive_noops += 1
            self._failed.add(self._failure_candidate)   # never repeat against this unchanged observation
        else:
            self.consecutive_noops = 0
        self._to(SessionState.OBSERVING, "result")
        self._check_limits()
        return self.state


class ActiveSessionSlot:
    """Only one active computer-control session. A new one preempts (cancels) the old one, or is rejected."""

    def __init__(self):
        self.current: Optional[ComputerControlSession] = None

    def claim(self, session: ComputerControlSession, preempt: bool = False) -> None:
        if self.current is not None and self.current is not session and not self.current.is_terminal:
            if not preempt:
                raise SessionConflict(f"session {self.current.session_id} is still active")
            self.current.cancel("PREEMPTED")
        self.current = session

    def release(self, session: ComputerControlSession) -> None:
        if self.current is session:
            self.current = None
