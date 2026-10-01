"""
Step 15C production press_key: RIGHT_ARROW / LEFT_ARROW only. Schema, gate, executor, verifier and the pinned
backend mechanism - with fakes and a MOCKED Quartz. No real keyboard event is ever generated here.
"""

import pytest
from pydantic import ValidationError

from cc_fixtures import SAFARI, TEXTEDIT, observation
from services.computer_control import observer as O
from services.computer_control.actions import get_definition, is_enabled
from services.computer_control.chooser import CHOOSER_OPS, validate_decision
from services.computer_control.executor import ObservationCache, execute_action
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import context_for, evaluate
from services.computer_control.models import (
    ActionStatus as A, AppIdentity, Op, PressKeyArgs, ResolutionMethod, ResolvedTarget, VerificationStatus as V,
)
from services.computer_control.orchestrator import ORCHESTRATABLE
from services.computer_control.verification import CaretVerifier
from test_computer_control_executor import Clock, FakeLive, _idx, _request, textedit

FRONT_TE = cross_check([FrontmostReading(source="nsworkspace", app=TEXTEDIT),
                        FrontmostReading(source="app_ax_frontmost", app=TEXTEDIT)])
FRONT_SAFARI = cross_check([FrontmostReading(source="nsworkspace", app=SAFARI),
                            FrontmostReading(source="app_ax_frontmost", app=SAFARI)])
SCOPE = frozenset({TEXTEDIT.bundle_id})


# --- 1. action / schema ---

@pytest.mark.parametrize("key", ["RIGHT_ARROW", "LEFT_ARROW"])
def test_supported_keys_are_accepted(key):
    assert PressKeyArgs(key=key).key == key


@pytest.mark.parametrize("bad", [
    {"key": "RETURN"}, {"key": "ESCAPE"}, {"key": "TAB"}, {"key": "DELETE"}, {"key": "UP_ARROW"}, {"key": "a"},
    {"key": "right_arrow"}, {"key": 124}, {"keycode": 124}, {"key": "RIGHT_ARROW", "modifiers": ["cmd"]},
    {"key": "RIGHT_ARROW", "flags": 1048576}, {"key": "cmd+q"}, {"key": "RIGHT_ARROW", "text": "x"}, {},
])
def test_everything_else_is_rejected(bad):
    with pytest.raises(ValidationError):
        PressKeyArgs(**bad)


def test_registry_enables_press_key_for_text_areas_at_low_risk():
    d = get_definition(Op.PRESS_KEY)
    assert is_enabled(Op.PRESS_KEY) and d.params_model is PressKeyArgs and d.risk.value == "LOW"
    assert d.validated_targets == frozenset({("AXTextArea", None)})
    assert Op.PRESS_KEY in CHOOSER_OPS and Op.PRESS_KEY in ORCHESTRATABLE


def test_chooser_decision_with_an_unsupported_key_is_rejected():
    obs = observation(obs_id="o1")
    ok = validate_decision({"status": "ACTION", "op": "press_key", "target_index": 0, "obs_id": "o1",
                            "args": {"key": "RIGHT_ARROW"}}, utterance="move right", observation=obs)
    bad = validate_decision({"status": "ACTION", "op": "press_key", "target_index": 0, "obs_id": "o1",
                             "args": {"key": "RETURN"}}, utterance="press return", observation=obs)
    assert ok.ok and not bad.ok and bad.error.startswith("ARGS")


# --- 2. gate ---

def _gate(targets_kw=None, app=TEXTEDIT, key="RIGHT_ARROW", window="default", scope=SCOPE):
    from cc_fixtures import TEXT_AREA
    target = dict(TEXT_AREA, **(targets_kw or {}))
    obs = observation((target,), obs_id="g1", app=app, win=window)
    t = ResolvedTarget(target=obs.targets[0], method=ResolutionMethod.ORDINAL, obs_id="g1")
    return evaluate(context_for(Op.PRESS_KEY, PressKeyArgs(key=key), t, obs, scope))


@pytest.mark.parametrize("key", ["RIGHT_ARROW", "LEFT_ARROW"])
def test_supported_key_is_allowed_when_every_condition_holds(key):
    g = _gate(key=key)
    assert (g.outcome.value, g.reason) == ("ALLOW", "LOW_ALLOWED")


def test_gate_blocks():
    from cc_fixtures import window
    assert _gate({"subrole": "AXSecureTextField", "role": "AXTextField"}).reason == "SECURE_FIELD"
    keychain = AppIdentity(bundle_id="com.apple.keychainaccess", pid=5, name="Keychain Access")
    assert _gate(app=keychain, scope=frozenset({keychain.bundle_id})).outcome.value == "BLOCK"
    assert _gate({"focused": False}).reason == "TEXT_AREA_NOT_FOCUSED"
    assert _gate(window=window(main=False)).reason == "NOT_MAIN_WINDOW"
    assert _gate({"role": "AXButton", "label": "OK"}).reason == "TARGET_CLASS_NOT_VALIDATED"
    assert _gate(scope=frozenset({SAFARI.bundle_id})).reason == "OUTSIDE_ALLOWED_APPS"


# --- 3. executor (fake live backend, mocked posting) ---

class KeyLive(FakeLive):
    def __init__(self, app, pid=TEXTEDIT.pid, active=True, deliver=True, move=None, change_text=False, **kw):
        super().__init__(app, **kw)
        self.pid, self.active, self.deliver, self.move, self.change_text = pid, active, deliver, move, change_text
        self.posted = []

    def running_pid(self, bundle_id):
        return self.pid

    def is_active(self, pid):
        return self.active

    def app_reports_frontmost(self, pid):
        return self.active

    def post_arrow_key(self, pid, key):
        self.posted.append((pid, key))
        self.mutations.append(f"key:{key}")
        el = self.app.attrs["AXFocusedUIElement"]
        if self.deliver:
            delta = self.move if self.move is not None else {"RIGHT_ARROW": 1, "LEFT_ARROW": -1}[key]
            el.sel = (el.sel[0] + delta, 0)
        if self.change_text:
            el.text += "x"
        return 0


def _setup(app, frontmost_app=TEXTEDIT, caret=0, **kw):
    backend = KeyLive(app, **kw)
    backend.app.attrs["AXFocusedUIElement"].sel = (caret, 0)
    clock = Clock()
    obs, handles = O.observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=frontmost_app)
    obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
    return backend, clock, ObservationCache(obs, handles, clock())


def _press(backend, clock, cache, key, frontmost=FRONT_TE):
    req = _request(cache, Op.PRESS_KEY, _idx(cache.observation, "AXTextArea"), PressKeyArgs(key=key))

    def reobserve(pid=None):
        o, h = O.observe_app_with_handles(backend, backend.app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
        return o.model_copy(update={"settle": o.settle.model_copy(update={"status": O.SettleStatus.SETTLED})}), h
    return execute_action(req, utterance="move the caret", allowed_apps=SCOPE, cache=cache, backend=backend,
                          frontmost=frontmost, reobserve=reobserve, clock=clock, sleep=clock.sleep,
                          now=lambda: 1_790_000_000.0)


def test_right_then_left_move_exactly_and_restore():
    backend, clock, cache = _setup(textedit())
    r1, cache2 = _press(backend, clock, cache, "RIGHT_ARROW")
    assert r1.status is A.SUCCESS and r1.verification is V.VERIFIED and r1.mechanism == "cg.post_to_pid_arrow"
    assert (r1.evidence["range_before"], r1.evidence["range_after"], r1.evidence["text_unchanged"]) == ("0,0", "1,0",
                                                                                                        True)
    r2, _ = _press(backend, clock, cache2, "LEFT_ARROW")
    assert r2.status is A.SUCCESS and r2.evidence["range_after"] == "0,0"
    assert backend.posted == [(TEXTEDIT.pid, "RIGHT_ARROW"), (TEXTEDIT.pid, "LEFT_ARROW")]
    assert backend.mutations == ["key:RIGHT_ARROW", "key:LEFT_ARROW"] and r1.evidence["target_pid"] == TEXTEDIT.pid
    assert "EVIE_FIXTURE" not in r1.model_dump_json()                  # the document text is never echoed


@pytest.mark.parametrize("kw, status, reason", [
    ({"deliver": False}, A.FAILED, "CARET_UNCHANGED"),
    ({"move": 2}, A.NOT_VERIFIABLE, "CARET_MOVED_UNEXPECTEDLY"),
    ({"change_text": True}, A.NOT_VERIFIABLE, "TEXT_CHANGED"),
])
def test_unverified_outcomes_are_never_success_and_never_retried(kw, status, reason):
    backend, clock, cache = _setup(textedit(), **kw)
    r, _ = _press(backend, clock, cache, "RIGHT_ARROW")
    assert r.status is status and r.reason == reason and len(backend.posted) == 1


@pytest.mark.parametrize("setup_kw, frontmost, reason, status", [
    ({}, FRONT_SAFARI, "TARGET_APP_NOT_FRONTMOST", A.BLOCKED),
    ({"pid": 9999}, FRONT_TE, "TARGET_PID_CHANGED", A.STALE),
    ({"active": False}, FRONT_TE, "TARGET_APP_NOT_ACTIVE", A.BLOCKED),
    ({"caret": 19}, FRONT_TE, "CARET_AT_LIMIT", A.BLOCKED),
])
def test_live_prechecks_block_before_any_key(setup_kw, frontmost, reason, status):
    backend, clock, cache = _setup(textedit(), **setup_kw)
    r, _ = _press(backend, clock, cache, "RIGHT_ARROW", frontmost=frontmost)
    assert r.status is status and r.reason == reason and backend.posted == []


def test_unfocused_text_area_is_blocked_live():
    app = textedit()
    backend, clock, cache = _setup(app)
    app.attrs["AXFocusedUIElement"].attrs["AXFocused"] = False          # focus moved after the observation
    r, _ = _press(backend, clock, cache, "RIGHT_ARROW")
    assert r.reason == "TEXT_AREA_NOT_FOCUSED_LIVE" and backend.posted == []


def test_ambiguous_text_area_is_blocked():
    backend, clock, cache = _setup(textedit(second_window=True))
    r, _ = _press(backend, clock, cache, "RIGHT_ARROW")
    assert r.status is A.BLOCKED and backend.posted == []


def test_stale_observation_and_stale_target_are_blocked():
    backend, clock, cache = _setup(textedit())
    req = _request(cache, Op.PRESS_KEY, _idx(cache.observation, "AXTextArea"), PressKeyArgs(key="RIGHT_ARROW"))
    stale = req.model_copy(update={"obs_id": "obs-old"})
    r, _ = execute_action(stale, utterance="u", allowed_apps=SCOPE, cache=cache, backend=backend, frontmost=FRONT_TE,
                          reobserve=lambda pid=None: None, clock=clock, sleep=clock.sleep, now=lambda: 1.0)
    assert r.status is A.STALE and r.reason == "OBSERVATION_NOT_LATEST" and backend.posted == []
    cache.handles.targets[req.target.target.index].gone = True
    r, _ = _press(backend, clock, cache, "RIGHT_ARROW")
    assert r.status is A.STALE and backend.posted == []


# --- 4. verifier ---

@pytest.mark.parametrize("before, after, delta, status, reason", [
    (("abc", (0, 0)), ("abc", (1, 0)), 1, V.VERIFIED, "CARET_MOVED_EXACTLY"),
    (("abc", (1, 0)), ("abc", (0, 0)), -1, V.VERIFIED, "CARET_MOVED_EXACTLY"),
    (("abc", (0, 0)), ("abc", (0, 0)), 1, V.VERIFIED_NO_CHANGE, "CARET_UNCHANGED"),
    (("abc", (0, 0)), ("abc", (2, 0)), 1, V.INCONCLUSIVE, "CARET_MOVED_UNEXPECTEDLY"),
    (("abc", (0, 0)), ("abcd", (1, 0)), 1, V.INCONCLUSIVE, "TEXT_CHANGED"),
    (("abc", (0, 0)), ("abc", (1, 1)), 1, V.INCONCLUSIVE, "CARET_MOVED_UNEXPECTEDLY"),
])
def test_caret_verifier(before, after, delta, status, reason):
    r = CaretVerifier().verify(before, after, delta)
    assert (r.status, r.reason) == (status, reason)


# --- the pinned backend mechanism, with a MOCKED Quartz ---

class MockQuartz:
    def __init__(self):
        self.created, self.posted = [], []

    def CGEventCreateKeyboardEvent(self, source, code, down):
        self.created.append((source, code, down))
        return ("event", code, down)

    def CGEventPostToPid(self, pid, event):
        self.posted.append((pid, event))


@pytest.mark.parametrize("key, code", [("RIGHT_ARROW", 124), ("LEFT_ARROW", 123)])
def test_backend_posts_exactly_one_down_and_one_up_to_the_pid(key, code):
    from services.computer_control.macos_actions import PyObjCActionBackend
    b = object.__new__(PyObjCActionBackend)
    b.quartz = MockQuartz()
    assert b.post_arrow_key(4321, key) == 0
    assert b.quartz.created == [(None, code, True), (None, code, False)]              # no source, no flags
    assert b.quartz.posted == [(4321, ("event", code, True)), (4321, ("event", code, False))]


@pytest.mark.parametrize("key", ["RETURN", "ESCAPE", "UP_ARROW", 124, "cmd+q"])
def test_backend_refuses_any_other_key(key):
    from services.computer_control.macos_actions import PyObjCActionBackend
    b = object.__new__(PyObjCActionBackend)
    b.quartz = MockQuartz()
    with pytest.raises(KeyError):
        b.post_arrow_key(4321, key)
    assert b.quartz.created == [] and b.quartz.posted == []
