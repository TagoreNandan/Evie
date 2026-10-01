"""
CC-1a Step 6: deterministic orchestration (orchestrator.py) over the Step 1 session, with a fake world.
No macOS. The world log records every port call so ordering and "no execution without ..." are provable.
"""

import pytest
from pydantic import ValidationError

from cc_fixtures import CHROME, SAFARI, TERMINAL, TEXTEDIT, FakeClock, observation
from services.computer_control.models import (
    ActionResult, ActionStatus as A, ExecutionStatus as E, Op, PermissionStatus, VerificationStatus as V,
)
from services.computer_control.observer import ObserveResult
from services.computer_control.orchestrator import (
    ActionIntent, Goal, Orchestrator, Plan, TargetSelector, goal_check_for,
)
from services.computer_control.session import ComputerControlSession, SessionLimits, SessionState as S

FINDER = type(TEXTEDIT)(bundle_id="com.apple.finder", pid=629, name="Finder")
RUNNING = (TEXTEDIT, CHROME, SAFARI, TERMINAL, FINDER)
BY_ID = {a.bundle_id: a for a in RUNNING}
UTTERANCE = "switch to Finder and then Safari, in TextEdit type EVIE_STEP6 then restore EVIE_MARK"
TYPE_SPAN = [UTTERANCE.index("EVIE_STEP6"), UTTERANCE.index("EVIE_STEP6") + len("EVIE_STEP6")]
SCOPE = frozenset({FINDER.bundle_id, SAFARI.bundle_id, TEXTEDIT.bundle_id})


class World:
    """Fake observe/act ports. Outcomes are scripted per act call; activation changes the frontmost app."""

    def __init__(self, front=SAFARI, outcomes=(), fail_observe_at=None, takeover_at=None, reuse_id_at=None,
                 clock=None, act_cost_s=0.0):
        self.front, self.outcomes, self.n, self.log = front, list(outcomes), 0, []
        self.fail_observe_at, self.takeover_at, self.reuse_id_at = fail_observe_at, takeover_at, reuse_id_at
        self.clock, self.act_cost_s = clock, act_cost_s

    def observe(self, bundle_id):
        self.n += 1
        oid = f"obs-{self.n - 1}" if self.reuse_id_at == self.n else f"obs-{self.n}"
        if self.fail_observe_at == self.n:
            self.log.append(("observe-failed", oid))
            return ObserveResult(status="ERROR", reason="AX_TIMEOUT")
        if self.takeover_at == self.n:
            self.front = CHROME                                     # the user switched apps
        self.log.append(("observe", oid))
        obs = observation(obs_id=oid, taken_at=float(self.n), app=BY_ID[bundle_id], frontmost=self.front,
                          running=RUNNING, fingerprint=f"fp-{self.n}")
        return ObserveResult(status="OK", observation=obs)

    def act(self, request, utterance, allowed_apps):
        self.log.append(("act", request.op.value, request.obs_id))
        if self.clock:
            self.clock.advance(self.act_cost_s)
        outcome = self.outcomes.pop(0) if self.outcomes else "SUCCESS"
        if outcome == "RAISE":
            raise TimeoutError("no action result")
        base = dict(session_id=request.session_id, step=request.step, op=request.op, requested_at=1.0)
        if outcome == "SUCCESS":
            if request.op is Op.ACTIVATE_APP:
                self.front = BY_ID[request.args.bundle_id]
            return ActionResult.executed(execution=E.EXECUTED, verification=V.VERIFIED, fingerprint_changed=True,
                                         evidence={"obs_after": "worker-obs"}, **base)
        if outcome == "FAILED":
            return ActionResult.executed(execution=E.EXECUTED, verification=V.VERIFIED_NO_CHANGE,
                                         fingerprint_changed=False, **base)
        if outcome == "NOT_VERIFIABLE_NOOP":
            return ActionResult.executed(execution=E.EXECUTED, verification=V.CHANGED_UNSPECIFIED,
                                         fingerprint_changed=False, **base)
        if outcome == "NOT_VERIFIABLE":
            return ActionResult.executed(execution=E.EXECUTED, verification=V.INCONCLUSIVE,
                                         fingerprint_changed=True, **base)
        if outcome == "TIMEOUT":
            return ActionResult.executed(execution=E.TIMEOUT, verification=V.INCONCLUSIVE,
                                         fingerprint_changed=False, **base)
        status = A(outcome)                                          # STALE / BLOCKED / CANCELLED
        return ActionResult(status=status, execution=E.NOT_EXECUTED, verification=V.NOT_APPLICABLE,
                            retry_permitted=status is A.STALE, reason="WORKER_" + outcome, **base)

    def acts(self):
        return [e for e in self.log if e[0] == "act"]


def activate(bundle, observe=FINDER.bundle_id, **kw):
    return ActionIntent(op=Op.ACTIVATE_APP, observe_bundle=observe, args={"bundle_id": bundle}, **kw)


def te(op, target, args=None, **kw):
    return ActionIntent(op=op, observe_bundle=TEXTEDIT.bundle_id, target=target, args=args or {}, **kw)


TEXT = TargetSelector(role="AXTextArea")
SCROLLER = TargetSelector(role="AXScrollArea")
CENTER = TargetSelector(role="AXCheckBox", subrole="AXSegment", label="align center")
LEFT = TargetSelector(role="AXCheckBox", subrole="AXSegment", label="align left")


def run(plan, world=None, scope=SCOPE, limits=SessionLimits(), checkpoint=lambda name, session: None, clock=None):
    clock = clock or FakeClock()
    world = world or World(clock=clock)
    if world.clock is None:
        world.clock = clock
    session = ComputerControlSession("sess", plan.utterance, scope, clock=clock, limits=limits,
                                     goal_check=goal_check_for(plan))
    session.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    orch = Orchestrator(session, world.observe, world.act, checkpoint=lambda n: checkpoint(n, session))
    return orch.run(plan), world, session


def plan(*intents, goal=None, final=FINDER.bundle_id):
    return Plan(utterance=UTTERANCE, intents=intents, final_observe_bundle=final,
                goal=Goal(kind="frontmost_app", bundle_id=goal) if goal else None)


def kinds(result):
    return [e.kind for e in result.events]


# --- happy paths ---

def test_one_action_success_exact_event_order():
    result, world, _ = run(plan(activate(FINDER.bundle_id), goal=FINDER.bundle_id))
    assert result.final_state is S.DONE_VERIFIED
    assert kinds(result) == ["OBSERVE", "CHOOSE", "RESOLVE", "GATE", "FRESHNESS", "EXECUTE", "VERIFY", "OBSERVE", "GOAL"]
    assert world.log == [("observe", "obs-1"), ("act", "activate_app", "obs-1"), ("observe", "obs-2")]


def test_two_actions_each_on_its_own_fresh_observation():
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id), goal=SAFARI.bundle_id))
    assert result.final_state is S.DONE_VERIFIED and result.observations == ("obs-1", "obs-2", "obs-3")
    assert world.log == [("observe", "obs-1"), ("act", "activate_app", "obs-1"), ("observe", "obs-2"),
                         ("act", "activate_app", "obs-2"), ("observe", "obs-3")]
    assert [s.obs_before for s in result.steps] == ["obs-1", "obs-2"]


@pytest.mark.parametrize("first, second", [
    (te(Op.SET_TEXT, TEXT, {"text_span": TYPE_SPAN}),
     te(Op.SET_TEXT, TEXT, {"text_span": [UTTERANCE.index("EVIE_MARK"), len(UTTERANCE)], "mode": "replace_all"})),
    (te(Op.SCROLL, SCROLLER, {"direction": "down"}), te(Op.SCROLL, SCROLLER, {"direction": "up"})),
    (te(Op.PRESS, CENTER), te(Op.PRESS, LEFT)),
])
def test_element_action_and_reversal_plans(first, second):
    result, world, _ = run(plan(first, second, final=TEXTEDIT.bundle_id))
    assert result.final_state is S.DONE_UNVERIFIED                      # no goal declared: never "verified"
    assert [e[2] for e in world.acts()] == ["obs-1", "obs-2"]
    resolves = [e.detail for e in result.events if e.kind == "RESOLVE"]
    assert "obs=obs-1" in resolves[0] and "obs=obs-2" in resolves[1]


# --- THE ordering invariant ---

SCENARIOS = [
    dict(p=plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id), goal=SAFARI.bundle_id)),
    dict(p=plan(te(Op.PRESS, CENTER), te(Op.PRESS, LEFT), final=TEXTEDIT.bundle_id)),
    dict(p=plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), w=dict(outcomes=["FAILED"])),
    dict(p=plan(*[te(Op.SCROLL, SCROLLER, {"direction": "down"}, continue_if_not_verifiable=True)] * 4,
                final=TEXTEDIT.bundle_id), w=dict(outcomes=["NOT_VERIFIABLE"] * 4)),
    dict(p=plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), w=dict(takeover_at=2)),
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_execution_without_observe_resolve_gate_freshness_and_fresh_observation_between(scenario):
    result, world, _ = run(scenario["p"], world=World(**scenario.get("w", {})))
    seen = set()
    last_verified = False
    for e in result.events:
        if e.kind == "OBSERVE":
            seen = {"OBSERVE"}
            last_verified = False
        elif e.kind in ("RESOLVE", "GATE", "FRESHNESS"):
            assert "OBSERVE" in seen
            seen.add(e.kind)
        elif e.kind == "EXECUTE":
            assert seen == {"OBSERVE", "RESOLVE", "GATE", "FRESHNESS"}, (seen, kinds(result))
            assert not last_verified
            seen = set()                                            # a new observation is needed before the next
        elif e.kind == "VERIFY":
            last_verified = True
    # The world agrees: every act used the observation immediately before it, and no two acts are adjacent.
    for i, entry in enumerate(world.log):
        if entry[0] == "act":
            assert world.log[i - 1] == ("observe", entry[2])


# --- gate / resolution / unsupported ---

def test_outside_allowed_scope_asks_and_never_executes():
    result, world, _ = run(plan(activate(CHROME.bundle_id)))
    assert result.final_state is S.ASKING and result.stop_reason == "OUTSIDE_ALLOWED_APPS" and world.acts() == []


def test_sensitive_app_blocked_and_never_executes():
    result, world, _ = run(plan(activate(TERMINAL.bundle_id)), scope=SCOPE | {TERMINAL.bundle_id})
    assert result.final_state is S.BLOCKED and "RESTRICTED_APP" in result.stop_reason and world.acts() == []
    assert kinds(result)[-2:] == ["GATE", "STOP"] and "EXECUTE" not in kinds(result)
    assert result.events[-2].detail.startswith("NOT_ALLOWED")


@pytest.mark.parametrize("intent", [te(Op.FOCUS, TEXT), ActionIntent(op=Op.OPEN_URL, observe_bundle=SAFARI.bundle_id)])
def test_unsupported_operations_are_blocked_not_skipped(intent):
    result, world, _ = run(plan(intent, activate(SAFARI.bundle_id)))
    assert result.final_state is S.BLOCKED and "UNSUPPORTED_OPERATION" in result.stop_reason and world.acts() == []


def test_unresolvable_and_ambiguous_targets():
    result, world, _ = run(plan(te(Op.PRESS, TargetSelector(role="AXCheckBox", subrole="AXSegment",
                                                            label="align right"))))
    assert result.final_state is S.BLOCKED and "NO_MATCH" in result.stop_reason and world.acts() == []
    result, world, _ = run(plan(te(Op.PRESS, TargetSelector(role="AXButton", label="OK"))))
    assert result.final_state is S.BLOCKED and "TARGET_CLASS_NOT_VALIDATED" in result.stop_reason
    assert world.acts() == []


# --- result handling: no retries ---

@pytest.mark.parametrize("outcome, reason", [
    ("FAILED", "STOPPED_AFTER_FAILED"), ("STALE", "STOPPED_AFTER_STALE"),
    ("NOT_VERIFIABLE", "STOPPED_AFTER_NOT_VERIFIABLE"), ("TIMEOUT", "STOPPED_AFTER_TIMEOUT"),
])
def test_non_success_stops_without_retry(outcome, reason):
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(FINDER.bundle_id), activate(SAFARI.bundle_id)),
                           world=World(outcomes=[outcome]))
    assert result.final_state is S.BLOCKED and result.stop_reason == reason
    assert len(world.acts()) == 1 and result.steps[0].status is A(outcome if outcome != "NOT_VERIFIABLE"
                                                                    else "NOT_VERIFIABLE")


def test_worker_blocked_and_cancelled_results_are_terminal():
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), world=World(outcomes=["BLOCKED"]))
    assert result.final_state is S.BLOCKED and len(world.acts()) == 1
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)),
                           world=World(outcomes=["CANCELLED"]))
    assert result.final_state is S.CANCELLED and len(world.acts()) == 1


def test_unknown_act_outcome_is_an_error_not_a_retry():
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), world=World(outcomes=["RAISE"]))
    assert result.final_state is S.ERROR and result.stop_reason == "ACT_OUTCOME_UNKNOWN:TimeoutError"
    assert len(world.acts()) == 1


def test_not_verifiable_continues_only_with_explicit_policy():
    p = plan(activate(FINDER.bundle_id, continue_if_not_verifiable=True), activate(SAFARI.bundle_id),
             goal=SAFARI.bundle_id)
    result, world, _ = run(p, world=World(outcomes=["NOT_VERIFIABLE", "SUCCESS"]))
    assert len(world.acts()) == 2 and result.final_state is S.DONE_VERIFIED


# --- limits ---

def test_three_consecutive_noops_stop():
    intents = [te(Op.SCROLL, SCROLLER, {"direction": "down"}, continue_if_not_verifiable=True)] * 5
    result, world, _ = run(plan(*intents, final=TEXTEDIT.bundle_id), world=World(outcomes=["NOT_VERIFIABLE_NOOP"] * 5))
    assert result.final_state is S.LIMIT_REACHED and result.stop_reason == "NOOP_LIMIT" and len(world.acts()) == 3


def test_action_limit():
    result, world, _ = run(plan(*[activate(FINDER.bundle_id), activate(SAFARI.bundle_id)] * 3),
                           limits=SessionLimits(max_actions=2))
    assert result.final_state is S.LIMIT_REACHED and result.stop_reason == "ACTION_LIMIT" and len(world.acts()) == 2


def test_forty_action_cap_default():
    intents = [activate(FINDER.bundle_id), activate(SAFARI.bundle_id)] * 20
    result, world, _ = run(Plan(utterance=UTTERANCE, intents=intents, final_observe_bundle=FINDER.bundle_id))
    assert len(world.acts()) == 40 and result.final_state is S.LIMIT_REACHED and result.stop_reason == "ACTION_LIMIT"
    with pytest.raises(ValidationError):
        Plan(utterance=UTTERANCE, intents=intents + [activate(FINDER.bundle_id)], final_observe_bundle=FINDER.bundle_id)


def test_session_wall_clock():
    clock = FakeClock()
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id), activate(FINDER.bundle_id)),
                           world=World(clock=clock, act_cost_s=100.0), clock=clock)
    assert result.final_state is S.LIMIT_REACHED and result.stop_reason == "WALL_CLOCK_LIMIT" and len(world.acts()) == 2


# --- cancellation at every checkpoint ---

CHECKPOINTS = ["before_observe", "after_observe", "before_choose", "after_choose", "before_gate", "before_execute"]


@pytest.mark.parametrize("where", CHECKPOINTS)
def test_cancellation_before_execution_prevents_any_mutation(where):
    def checkpoint(name, session):
        if name == where:
            session.cancel()
    result, world, _ = run(plan(activate(FINDER.bundle_id)), checkpoint=checkpoint)
    assert result.final_state is S.CANCELLED and world.acts() == []
    assert kinds(result)[-1] == "STOP"


def test_cancellation_after_execution_prevents_the_next_action():
    def checkpoint(name, session):
        if name == "after_execute":
            session.cancel()
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), checkpoint=checkpoint)
    assert result.final_state is S.CANCELLED and len(world.acts()) == 1


def test_cancel_between_gate_and_authorize_is_caught_by_the_session():
    orig = ComputerControlSession.propose

    def propose_then_cancel(self, decision, obs_id):
        request = orig(self, decision, obs_id)
        if request is not None:
            self.cancel()                                           # Stop arrives right after approval
        return request
    ComputerControlSession.propose = propose_then_cancel
    try:
        result, world, _ = run(plan(activate(FINDER.bundle_id)))
    finally:
        ComputerControlSession.propose = orig
    assert result.final_state is S.CANCELLED and world.acts() == []


# --- context, observation, goal ---

def test_user_takeover_between_steps_asks():
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), world=World(takeover_at=2))
    assert result.final_state is S.ASKING and "USER_TAKEOVER" in [e.detail.split()[0] for e in result.events][-1]
    assert len(world.acts()) == 1


def test_observation_failure_stops_and_old_observation_is_not_used():
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), world=World(fail_observe_at=2))
    assert result.final_state is S.ERROR and result.stop_reason == "OBSERVATION_FAILED:ERROR" and len(world.acts()) == 1


def test_reused_observation_id_is_rejected():
    result, world, _ = run(plan(activate(FINDER.bundle_id), activate(SAFARI.bundle_id)), world=World(reuse_id_at=2))
    assert result.final_state is S.ERROR and result.stop_reason == "OBSERVATION_NOT_FRESH" and len(world.acts()) == 1


def test_goal_false_is_never_done():
    result, world, _ = run(plan(activate(FINDER.bundle_id), goal=SAFARI.bundle_id))
    assert result.final_state is S.BLOCKED and result.stop_reason == "GOAL_NOT_MET"
    assert S.DONE_VERIFIED not in {result.final_state} and len(world.acts()) == 1


def test_done_unverified_without_goal():
    result, _, _ = run(plan(activate(FINDER.bundle_id)))
    assert result.final_state is S.DONE_UNVERIFIED and result.stop_reason == "NO_GOAL_VERIFIER"


def test_run_requires_a_live_session_and_the_verbatim_utterance():
    clock = FakeClock()
    session = ComputerControlSession("s", UTTERANCE, SCOPE, clock=clock)
    world = World()
    with pytest.raises(ValueError):
        Orchestrator(session, world.observe, world.act).run(plan(activate(FINDER.bundle_id)))   # not started
    session.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    session.cancel()
    with pytest.raises(ValueError):
        Orchestrator(session, world.observe, world.act).run(plan(activate(FINDER.bundle_id)))   # terminal
    s2 = ComputerControlSession("s2", "something else", SCOPE, clock=clock)
    s2.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    with pytest.raises(ValueError):
        Orchestrator(s2, world.observe, world.act).run(plan(activate(FINDER.bundle_id)))
    assert world.log == []


# --- the plan is closed data ---

@pytest.mark.parametrize("bad", [
    dict(op="activate_app", observe_bundle="com.apple.finder", args={"bundle_id": "com.apple.finder", "x": 1}),
    dict(op="press", observe_bundle="com.apple.TextEdit", args={"selector": "#buy"}),
    dict(op="set_text", observe_bundle="com.apple.TextEdit", args={"text_span": [0, 3], "text": "rm -rf ~"}),
    dict(op="shell", observe_bundle="com.apple.Terminal"),
    dict(op="activate_app", observe_bundle="com.apple.finder", args={"bundle_id": "x"}, callback="print"),
    dict(op="open_url", observe_bundle="com.apple.Safari", args={"url": "https://example.com"}),
    dict(op="activate_app", observe_bundle="bad bundle; rm", args={"bundle_id": "com.apple.finder"}),
])
def test_intents_are_closed(bad):
    with pytest.raises(ValidationError):
        ActionIntent(**bad)
