"""
CC-1a Step 11: deterministic end-to-end session integration, with a fake world (no macOS, no network, no LLM).

    utterance -> session -> OBSERVE -> fast path -> VALIDATE -> HARDEN -> RESOLVE -> GATE -> FRESHNESS
              -> EXECUTE (one fake worker call) -> VERIFY -> OBSERVE -> ... -> DONE / ASK / BLOCKED / CANCELLED

The world log records every port call; `spy` also records the pure pipeline stages, so ordering and
"nothing executes without ..." are asserted on what actually ran, not on what was reported.
"""

import inspect

import pytest

from cc_fixtures import (
    ALIGN_CENTER, ALIGN_LEFT, CHROME, PLAIN_BUTTON, SAFARI, SCROLL_AREA, TERMINAL, TEXT_AREA, TEXTEDIT, FakeClock,
    observation,
)
from services.computer_control import chooser_boundary, session as session_module
from services.computer_control.actions import get_definition
from services.computer_control.chooser import CHOOSER_OPS
from services.computer_control.chooser_boundary import FastPathChooser, choose
from services.computer_control.gate import derive_allowed_apps
from services.computer_control.models import (
    ActionResult, ActionStatus as A, AppIdentity, ExecutionStatus as E, Op, PermissionStatus, VerificationStatus as V,
)
from services.computer_control.observer import ObserveResult
from services.computer_control.orchestrator import ORCHESTRATABLE, Orchestrator, run_commands
from services.computer_control.session import (
    ActiveSessionSlot, ComputerControlSession, SessionConflict, SessionLimits, SessionState as S,
)
from services.computer_control.verification import AppFrontmostGoal

FINDER = AppIdentity(bundle_id="com.apple.finder", pid=629, name="Finder")
SAFARI_TWIN = AppIdentity(bundle_id="com.example.safari-twin", pid=901, name="Safari")
RUNNING = (TEXTEDIT, CHROME, SAFARI, TERMINAL, FINDER)
BY_ID = {a.bundle_id: a for a in RUNNING + (SAFARI_TWIN,)}
STANDARD = (TEXT_AREA, SCROLL_AREA, ALIGN_LEFT, ALIGN_CENTER, PLAIN_BUTTON)
TWO_CENTERS = (TEXT_AREA, SCROLL_AREA, dict(ALIGN_CENTER), dict(ALIGN_CENTER))
SECURE_ONLY = (dict(TEXT_AREA, subrole="AXSecureTextField", role="AXTextField"),)


class World:
    """Fake observe/act ports. Outcomes are scripted per act call; a successful activation moves the front."""

    def __init__(self, front=TEXTEDIT, outcomes=(), targets=STANDARD, running=RUNNING, takeover_at=None,
                 clock=None, act_cost_s=0.0, fail_observe_at=None):
        self.front, self.outcomes, self.targets, self.running = front, list(outcomes), targets, running
        self.takeover_at, self.clock, self.act_cost_s, self.fail_observe_at = takeover_at, clock, act_cost_s, \
            fail_observe_at
        self.n, self.log = 0, []

    def observe(self, bundle_id):
        self.n += 1
        oid = f"obs-{self.n}"
        if self.fail_observe_at == self.n:
            self.log.append(("observe-failed", oid))
            return ObserveResult(status="ERROR", reason="AX_TIMEOUT")
        if self.takeover_at == self.n:
            self.front = CHROME                                     # the user switched apps
        self.log.append(("observe", oid))
        obs = observation(self.targets, obs_id=oid, taken_at=float(self.n), app=BY_ID[bundle_id],
                          frontmost=self.front, running=self.running, fingerprint=f"fp-{self.n}")
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

    def observes(self):
        return [e for e in self.log if e[0] == "observe"]


def new_session(utterance, world, *, clock=None, limits=SessionLimits(), goal=None, sid="sess"):
    allowed = derive_allowed_apps(utterance, world.front, world.running) or frozenset({world.front.bundle_id})
    s = ComputerControlSession(sid, utterance, allowed, clock=clock or FakeClock(), limits=limits, goal_check=goal)
    s.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    return s


def run(utterance, world=None, observe=TEXTEDIT.bundle_id, chooser=None, checkpoint=None, **kw):
    world = world or World()
    clock = kw.pop("clock", None) or FakeClock()
    if world.clock is None:
        world.clock = clock
    s = new_session(utterance, world, clock=clock, **kw)
    hook = (lambda name: checkpoint(name, s)) if checkpoint else (lambda name: None)
    result = Orchestrator(s, world.observe, world.act, checkpoint=hook).run_command(chooser or FastPathChooser(),
                                                                                  observe)
    return result, world, s


def kinds(result):
    return [e.kind for e in result.events]


@pytest.fixture
def spy(monkeypatch):
    """Record the pure pipeline stages as they really run (same module globals the code looks up)."""
    calls = []

    def wrap(module, name, label):
        real = getattr(module, name)

        def recorder(*a, **k):
            calls.append(label)
            return real(*a, **k)
        monkeypatch.setattr(module, name, recorder)
    wrap(chooser_boundary, "fast_path", "CHOOSE")
    wrap(chooser_boundary, "validate_output", "VALIDATE")
    wrap(chooser_boundary, "harden", "HARDEN")
    wrap(session_module, "resolve", "RESOLVE")
    wrap(session_module, "evaluate", "GATE")
    return calls


class ProviderChooser:
    """TEST ONLY: a scripted 'provider' behind the real boundary (validation + hardening). No network."""

    def __init__(self, raw):
        self.raw = raw

    def choose(self, utterance, obs, *, recent=(), step=1, allowed_apps=()):
        if any(r.status == "SUCCESS" for r in recent):
            return FastPathChooser().choose(utterance, obs, recent=recent, step=step, allowed_apps=allowed_apps)
        raw = self.raw(obs) if callable(self.raw) else self.raw

        class P:
            def choose(self, ci):
                return raw
        return choose(utterance, obs, allowed_apps=allowed_apps, recent=recent, step=step, provider=P())


# --- A. end-to-end ordering ---

def test_exact_order_for_one_text_action(spy):
    result, world, s = run("type Hello, Evie!")
    assert kinds(result)[:10] == ["OBSERVE", "CHOOSE", "VALIDATE", "HARDEN", "RESOLVE", "GATE", "FRESHNESS",
                                  "EXECUTE", "VERIFY", "OBSERVE"]
    assert kinds(result)[10:] == ["CHOOSE", "VALIDATE", "HARDEN", "GOAL", "STOP"]   # DONE claim, then terminal
    assert spy[:5] == ["CHOOSE", "VALIDATE", "HARDEN", "RESOLVE", "GATE"]       # what really ran, in order
    assert world.log == [("observe", "obs-1"), ("act", "set_text", "obs-1"), ("observe", "obs-2")]
    assert result.final_state is S.DONE_UNVERIFIED and result.stop_reason == "NO_GOAL_VERIFIER"
    step = result.steps[0]
    assert (step.op, step.status, step.verification, step.chooser, step.assessment) == (
        Op.SET_TEXT, A.SUCCESS, "VERIFIED", "fast_path/type", "VALID")


def test_executed_text_is_the_exact_span():
    captured = []
    world = World()
    real = world.act
    world.act = lambda req, utt, apps: (captured.append(utt[req.args.text_span[0]:req.args.text_span[1]]),
                                        real(req, utt, apps))[1]
    run("type Hello, Evie! ", world)
    assert captured == ["Hello, Evie! "]


@pytest.mark.parametrize("utterance, op", [("scroll down", Op.SCROLL), ("scroll up", Op.SCROLL),
                                           ("align center", Op.PRESS)])
def test_textedit_primitives_run_once_then_done(utterance, op):
    result, world, _ = run(utterance)
    assert [a[1] for a in world.acts()] == [op.value] and result.final_state is S.DONE_UNVERIFIED


@pytest.mark.parametrize("utterance, target", [("switch to Safari", SAFARI), ("switch to Finder", FINDER)])
def test_activation_is_goal_verified_on_a_fresh_observation(utterance, target):
    result, world, _ = run(utterance, World(front=TEXTEDIT), observe=FINDER.bundle_id)
    assert world.log == [("observe", "obs-1"), ("act", "activate_app", "obs-1"), ("observe", "obs-2")]
    assert result.final_state is S.DONE_VERIFIED and result.stop_reason == "GOAL_VERIFIED"
    assert world.front == target


# --- B. ASK: nothing is resolved, gated or executed ---

@pytest.mark.parametrize("utterance, reason", [("click that", "UNRESOLVED_REFERENCE"), ("scroll", "DIRECTION_REQUIRED"),
                                               ("switch to Photoshop", "UNKNOWN_APP"),
                                               ("please do something", "UNRECOGNIZED_REQUEST")])
def test_ask_never_resolves_gates_or_executes(spy, utterance, reason):
    result, world, s = run(utterance)
    assert result.final_state is S.ASKING and result.ask_reason == reason
    assert "RESOLVE" not in spy and "GATE" not in spy and world.acts() == []
    assert "RESOLVE" not in kinds(result) and "GATE" not in kinds(result)


def test_ask_keeps_candidates():
    result, world, _ = run("align center", World(targets=TWO_CENTERS))
    assert (result.final_state, result.ask_reason, result.candidates) == (S.ASKING, "AMBIGUOUS_TARGET", (2, 3))
    assert world.acts() == []


# --- C. BLOCKED: nothing executes ---

@pytest.mark.parametrize("utterance, reason", [("run ls", "COMMANDS_NOT_ENABLED"),
                                               ("delete this file", "DESTRUCTIVE_NOT_ENABLED"),
                                               ("click the second thing", "CONTROL_NOT_FOUND"),
                                               ("scroll 900 pixels down", "UNSUPPORTED_SCROLL_AMOUNT")])
def test_blocked_commands_never_execute(spy, utterance, reason):
    world = World(running=(TEXTEDIT, SAFARI, FINDER))                           # Chrome not running
    result, world, _ = run(utterance, world)
    assert result.final_state is S.BLOCKED and world.acts() == [] and "GATE" not in spy
    assert reason is None or reason in result.stop_reason


def test_gate_still_blocks_a_secure_field(spy):
    result, world, _ = run("type hunter2", World(targets=SECURE_ONLY))
    assert result.final_state in (S.BLOCKED, S.ASKING) and world.acts() == []


def test_gate_still_blocks_activation_outside_the_allowed_scope():
    """The utterance never names Safari, so Safari is not in scope even if a chooser proposes it (gate: ASK)."""
    raw = lambda obs: {"status": "ACTION", "op": "activate_app", "args": {"bundle_id": SAFARI.bundle_id}}
    result, world, _ = run("bring up my browser thing", chooser=ProviderChooser(raw))
    assert (result.final_state, result.ask_reason) == (S.ASKING, "OUTSIDE_ALLOWED_APPS")
    assert "GATE" in kinds(result) and world.acts() == []


def test_gate_still_blocks_restricted_apps():
    world = World(running=RUNNING + (AppIdentity(bundle_id="com.apple.keychainaccess", pid=5, name="Keychain Access"),))
    result, world, _ = run("switch to Keychain Access", world)
    assert result.final_state is S.BLOCKED and world.acts() == []


# --- D. DONE is only a claim ---

def test_done_without_goal_is_unverified_and_executes_nothing():
    result, world, _ = run("done")
    assert result.final_state is S.DONE_UNVERIFIED and world.acts() == []


def test_done_with_unmet_goal_is_not_trusted():
    result, world, _ = run("done", World(front=TEXTEDIT), goal=AppFrontmostGoal(SAFARI.bundle_id))
    assert result.final_state is S.BLOCKED and result.stop_reason == "GOAL_NOT_MET" and world.acts() == []


def test_done_with_met_goal_is_verified():
    result, _, _ = run("done", World(front=SAFARI), goal=AppFrontmostGoal(SAFARI.bundle_id))
    assert result.final_state is S.DONE_VERIFIED


def test_activation_not_actually_frontmost_is_not_done():
    """The worker says SUCCESS but a fresh observation disagrees: user takeover / not done, never DONE."""
    world = World(front=TEXTEDIT)
    real = world.act

    def act(req, utt, apps):
        r = real(req, utt, apps)
        world.front = TEXTEDIT                              # nothing actually changed
        return r
    world.act = act
    result, _, _ = run("switch to Safari", world, observe=FINDER.bundle_id)
    assert result.final_state is S.ASKING and result.ask_reason == "USER_TAKEOVER"


# --- E. cancellation ---

@pytest.mark.parametrize("where", ["before_next_iteration", "before_observe", "before_choose", "after_choose",
                                   "before_gate", "before_execute"])
def test_cancellation_before_execution_means_zero_executor_calls(where):
    def checkpoint(name, s):
        if name == where:
            s.cancel()
    result, world, _ = run("type hello", checkpoint=checkpoint)
    assert result.final_state is S.CANCELLED and world.acts() == []


def test_cancellation_after_execution_prevents_the_next_iteration():
    def checkpoint(name, s):
        if name == "after_execute":
            s.cancel()
    result, world, _ = run("type hello", checkpoint=checkpoint)
    assert result.final_state is S.CANCELLED and len(world.acts()) == 1 and len(world.observes()) == 1


def test_cancellation_before_next_iteration_stops_after_one_action():
    seen = []

    def checkpoint(name, s):
        if name == "before_next_iteration":
            seen.append(name)
            if len(seen) == 2:
                s.cancel()
    result, world, _ = run("type hello", checkpoint=checkpoint)
    assert result.final_state is S.CANCELLED and len(world.acts()) == 1 and len(world.observes()) == 1


def test_stop_phrase_cancels_without_acting():
    result, world, _ = run("stop")
    assert result.final_state is S.CANCELLED and world.acts() == []


# --- F. stale targets cannot execute ---

def test_decision_for_an_old_observation_is_blocked():
    raw = {"status": "ACTION", "op": "press", "target_index": 3, "obs_id": "obs-0", "args": {}}
    result, world, _ = run("make the paragraph centered", chooser=ProviderChooser(raw))
    assert result.final_state is S.BLOCKED and "WRONG_OBSERVATION" in result.stop_reason and world.acts() == []


def test_worker_stale_is_not_retried():
    result, world, _ = run("type hello", World(outcomes=["STALE"]))
    assert len(world.acts()) == 1 and result.final_state is S.BLOCKED
    assert result.stop_reason == "STOPPED_AFTER_STALE"


def test_user_takeover_between_iterations_is_re_observed_not_continued():
    result, world, _ = run("type hello", World(takeover_at=2))
    assert result.final_state is S.ASKING and result.ask_reason == "USER_TAKEOVER" and len(world.acts()) == 1


# --- G. ambiguity: hardening turns a tie-break into ASK ---

def test_hardening_turns_duplicate_alignment_into_ask():
    raw = lambda obs: {"status": "ACTION", "op": "press", "target_index": 2, "obs_id": obs.obs_id, "args": {}}
    result, world, _ = run("make the paragraph centered", World(targets=TWO_CENTERS), chooser=ProviderChooser(raw))
    assert (result.final_state, result.ask_reason, result.candidates) == (S.ASKING, "AMBIGUOUS_TARGET", (2, 3))
    assert world.acts() == [] and any(e.kind == "HARDEN" and "MODEL_OUTPUT_AMBIGUOUS" in e.detail
                                      for e in result.events)


def test_hardening_turns_duplicate_safari_into_ask():
    raw = {"status": "ACTION", "op": "activate_app", "args": {"bundle_id": SAFARI.bundle_id}}
    world = World(running=RUNNING + (SAFARI_TWIN,))
    result, world, _ = run("bring safari forward please now", world, chooser=ProviderChooser(raw))
    assert (result.final_state, result.ask_reason) == (S.ASKING, "AMBIGUOUS_APP") and world.acts() == []


def test_fast_path_duplicate_safari_is_ask():
    result, world, _ = run("switch to Safari", World(running=RUNNING + (SAFARI_TWIN,)))
    assert (result.final_state, result.ask_reason) == (S.ASKING, "AMBIGUOUS_APP") and world.acts() == []


def test_invalid_text_span_is_invalid_output():
    raw = lambda obs: {"status": "ACTION", "op": "set_text", "target_index": 0, "obs_id": obs.obs_id,
                       "args": {"text_span": [0, 999]}}
    result, world, _ = run("put some words there", chooser=ProviderChooser(raw))
    assert result.final_state is S.BLOCKED and "TEXT_SPAN_OUTSIDE_UTTERANCE" in result.stop_reason
    assert world.acts() == [] and "HARDEN" not in kinds(result)


def test_text_command_executes_the_grammar_span_not_a_provider_span():
    """A provider span off by one is never used: the grammar answers a text command before any provider."""
    captured = []
    world = World()
    real = world.act
    world.act = lambda req, utt, apps: (captured.append(tuple(req.args.text_span)), real(req, utt, apps))[1]
    raw = lambda obs: {"status": "ACTION", "op": "set_text", "target_index": 0, "obs_id": obs.obs_id,
                       "args": {"text_span": [5, 10]}}
    result, _, _ = run("type hello ", world, chooser=ProviderChooser(raw))
    assert captured == [(5, 11)] and result.final_state is S.DONE_UNVERIFIED   # "hello " incl. trailing space


# --- H/I. verification and failure: no retry ---

@pytest.mark.parametrize("outcome, status, final", [
    ("NOT_VERIFIABLE", A.NOT_VERIFIABLE, S.BLOCKED), ("FAILED", A.FAILED, S.BLOCKED),
    ("TIMEOUT", A.TIMEOUT, S.BLOCKED), ("RAISE", None, S.ERROR)])
def test_non_success_stops_without_retry(outcome, status, final):
    result, world, _ = run("switch to Safari", World(outcomes=[outcome]), observe=FINDER.bundle_id)
    assert len(world.acts()) == 1 and len(world.observes()) == 1 and result.final_state is final
    if status is not None:
        step = result.steps[0]
        assert step.status is status and step.status is not A.SUCCESS
        assert result.stop_reason == f"STOPPED_AFTER_{status.value}"


# --- J. multi-step ---

def triple():
    return [("switch to Safari", FINDER.bundle_id), ("switch to Finder", FINDER.bundle_id),
            ("switch to Safari", FINDER.bundle_id)]


def sequence(world, commands, slot=None, clock=None):
    clock = clock or FakeClock()
    counter = iter(range(1, 100))
    return run_commands(commands, lambda u: new_session(u, world, clock=clock, sid=f"s{next(counter)}"),
                        world.observe, world.act, slot=slot or ActiveSessionSlot())


def test_multi_step_fresh_observation_after_every_action():
    world = World(front=TEXTEDIT)
    results = sequence(world, triple())
    assert [r.final_state for r in results] == [S.DONE_VERIFIED] * 3
    assert [e[0] for e in world.log] == ["observe", "act", "observe"] * 3
    acted_on = [e[2] for e in world.acts()]
    assert len(set(acted_on)) == 3 and world.front == SAFARI
    assert [r.session_id for r in results] == ["s1", "s2", "s3"]


def test_multi_step_failure_stops_the_sequence():
    world = World(front=TEXTEDIT, outcomes=["SUCCESS", "FAILED", "SUCCESS"])
    results = sequence(world, triple())
    assert [r.final_state for r in results] == [S.DONE_VERIFIED, S.BLOCKED]
    assert len(world.acts()) == 2 and world.front == SAFARI


def test_multi_step_ask_stops_the_sequence():
    world = World(front=TEXTEDIT)
    results = sequence(world, [("switch to Finder", FINDER.bundle_id), ("click that", TEXTEDIT.bundle_id),
                               ("switch to Safari", FINDER.bundle_id)])
    assert [r.final_state for r in results] == [S.DONE_VERIFIED, S.ASKING] and len(world.acts()) == 1


# --- K. limits ---

def test_limits_are_unchanged():
    lim = SessionLimits()
    assert (lim.max_actions, lim.max_consecutive_noops, lim.wall_clock_s) == (40, 3, 180.0)


def test_action_cap():
    result, world, _ = run("type hello", limits=SessionLimits(max_actions=1))
    assert len(world.acts()) == 1 and result.final_state is S.LIMIT_REACHED and result.stop_reason == "ACTION_LIMIT"


def test_time_cap():
    clock = FakeClock()
    result, world, _ = run("type hello", World(clock=clock, act_cost_s=181.0), clock=clock)
    assert len(world.acts()) == 1 and result.stop_reason == "WALL_CLOCK_LIMIT"


def test_noop_cap_counts_rejected_outputs():
    world = World()
    s = new_session("put some words there", world, limits=SessionLimits(max_consecutive_noops=1))
    s.observe(world.observe(TEXTEDIT.bundle_id).observation)
    assert s.propose({"status": "ACTION", "op": "set_text", "target_index": 0, "obs_id": "obs-1",
                      "args": {"text_span": [0, 999]}}, "obs-1") is None
    assert s.state is S.LIMIT_REACHED and s.stop_reason == "NOOP_LIMIT"


def test_one_active_session():
    world = World(front=TEXTEDIT)
    slot = ActiveSessionSlot()
    other = new_session("type x", world, sid="other")
    slot.claim(other)
    with pytest.raises(SessionConflict):
        sequence(world, triple(), slot=slot)
    assert world.log == []


# --- observability and history: bounded, no secrets ---

def test_history_and_events_never_contain_the_typed_text():
    result, _, s = run("type hunter2-SECRET")
    assert s.recent and all("hunter2" not in r.summary for r in s.recent)
    assert all("hunter2" not in e.detail for e in result.events)
    assert "hunter2" not in result.model_dump_json()
    assert s.recent[0].status == "SUCCESS" and s.recent[0].op is Op.SET_TEXT


def test_recent_history_is_bounded():
    world = World()
    s = new_session("type hello", world)
    assert s.recent == []
    from services.computer_control.chooser import MAX_RECENT_ACTIONS
    assert MAX_RECENT_ACTIONS == 10
    assert "[-MAX_RECENT_ACTIONS:]" in inspect.getsource(session_module.ComputerControlSession.record_result)


def test_result_identifies_session_iteration_and_observations():
    result, _, _ = run("scroll down")
    assert result.session_id == "sess" and result.observations == ("obs-1", "obs-2")
    assert {e.step for e in result.events if e.kind in ("CHOOSE", "EXECUTE")} == {1, 2}
    assert result.steps[0].obs_before == "obs-1" and result.steps[0].gate.startswith("ALLOW")


# --- L. scope: no new operation, no provider ---

def test_no_new_operations():
    assert ORCHESTRATABLE == CHOOSER_OPS == {Op.ACTIVATE_APP, Op.SET_TEXT, Op.SCROLL, Op.PRESS, Op.PRESS_KEY, Op.CLOSE_WINDOW, Op.QUIT_APP, Op.SWITCH_APP, Op.SWITCH_WINDOW, Op.CLICK_ELEMENT, Op.COPY_SELECTION, Op.PASTE_CLIPBOARD, Op.SELECT_TEXT}
    assert Op.FOCUS not in ORCHESTRATABLE
    assert not get_definition(Op.ACTIVATE_APP).requires_target


def test_session_chooser_is_the_fast_path_without_a_provider():
    source = inspect.getsource(FastPathChooser)
    assert "provider=None" in source and "http" not in source.lower()
