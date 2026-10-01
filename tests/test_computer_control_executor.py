"""
CC-1a Step 4 live-action flow (executor.py) with a fake live backend. No macOS, no GUI mutation.
Every mutation call is recorded so tests can prove when NOTHING was touched and that nothing retries.
"""

import pytest

from cc_fixtures import SAFARI, TEXTEDIT
from services.computer_control import observer as O
from services.computer_control.executor import ObservationCache, execute_action
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import GatePolicy
from services.computer_control.models import (
    ActionRequest, ActionStatus as A, GateDecision, GateOutcome, NoArgs, Op, PermissionStatus, ResolutionMethod,
    ResolvedTarget, RiskLevel, ScrollArgs, SetTextArgs, VerificationStatus as V,
)
from services.computer_control.session import ComputerControlSession, SessionState
from test_computer_control_observer import FakeBackend, Node

UTTERANCE = "in TextEdit type EVIE_STEP4_TEST now"
SPAN = (UTTERANCE.index("EVIE"), UTTERANCE.index("EVIE") + len("EVIE_STEP4_TEST"))
SCOPE = frozenset({TEXTEDIT.bundle_id})
AGREED = cross_check([FrontmostReading(source="nsworkspace", app=SAFARI),
                      FrontmostReading(source="app_ax_frontmost", app=SAFARI)])
DISAGREE = cross_check([FrontmostReading(source="nsworkspace", app=SAFARI),
                        FrontmostReading(source="ax_focused_application", app=TEXTEDIT)])
ALLOW = GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.LOW, reason="LOW_ALLOWED")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def now(self):                  # the session's Clock protocol
        return self.t

    def sleep(self, s):
        self.t += s


class FakeLive(FakeBackend):
    """Fake TextEdit: mutations change fake state; every mutation is recorded."""

    def __init__(self, app, fail=None, ignore=(), wrong_text=False):
        super().__init__()
        self.app, self.fail, self.ignore, self.wrong_text, self.mutations = app, dict(fail or {}), set(ignore), \
            wrong_text, []

    def _mutate(self, name, fn):
        self.mutations.append(name)
        if name in self.fail:
            return self.fail[name]
        if name not in self.ignore:
            fn()
        return 0

    # reads
    def revalidate(self, el):
        if getattr(el, "gone", False):
            return -25202, {}
        return O.read_identity(self, el)

    def main_window(self, app):
        return app.attrs.get("AXMainWindow")

    def focused_element(self, app):
        return app.attrs.get("AXFocusedUIElement")

    def is_focused(self, el):
        return el.attrs.get("AXFocused")

    def text_state(self, el):
        return el.text, el.sel

    def scroll_bar(self, area):
        return area.bar, area.bar.value

    def visible_start(self, area):
        return int(area.bar.value * 1000)

    def segment_values(self, seg):
        return {s.attrs["AXDescription"]: s.value for s in seg.group}

    def glyph_x(self, window):
        center = [s for s in window.segments if s.value == 1][0].attrs["AXDescription"]
        return {"align left": 204.0, "align center": 429.6}.get(center, 300.0)

    # mutations
    def raise_window(self, w):
        return self._mutate("raise", lambda: None)

    def set_main(self, w):
        def go():
            self.app.attrs["AXMainWindow"] = w
            self.app.attrs["AXFocusedWindow"] = w
        return self._mutate("main", go)

    def set_focused(self, el):
        def go():
            prev = self.app.attrs.get("AXFocusedUIElement")
            if prev is not None:
                prev.attrs["AXFocused"] = False
            el.attrs["AXFocused"] = True
            self.app.attrs["AXFocusedUIElement"] = el
        return self._mutate("focus", go)

    def set_selected_text(self, el, text):
        def go():
            loc, ln = el.sel
            inserted = text + ("X" if self.wrong_text else "")
            el.text = el.text[:loc] + inserted + el.text[loc + ln:]
            el.sel = (loc + len(inserted), 0)
        return self._mutate("selected_text", go)

    def set_value_text(self, el, text):
        def go():
            el.text, el.sel = text, (len(text), 0)
        return self._mutate("value", go)

    def set_scroll_value(self, bar, value):
        return self._mutate("scroll", lambda: setattr(bar, "value", value))

    def press_alignment_segment(self, seg):
        def go():
            for s in seg.group:
                s.value = 1 if s is seg else 0
        return self._mutate("press", go)


def textedit(text="EVIE_FIXTURE_MARKER", second_window=False, bold=False):
    """A fake TextEdit: window(s) with a text area, a scroll area (with bar) and alignment segments."""
    def text_area():
        ta = Node("AXTextArea", AXIdentifier="First Text View", AXFocused=False, AXSelectedText="", AXValue=text,
                  settable=("AXSelectedText", "AXValue", "AXSelectedTextRange"), actions=["AXShowMenu"])
        ta.text, ta.sel = text, (0, 0)
        return ta

    def window(title):
        ta = text_area()
        area = Node("AXScrollArea", children=[ta], actions=["AXScrollDownByPage"])
        area.bar = type("Bar", (), {"value": 0.0})()
        labels = ["align left", "align center"] + (["bold"] if bold else [])
        segs = [Node("AXCheckBox", subrole="AXSegment", AXDescription=d, AXEnabled=True, actions=["AXPress"])
                for d in labels]
        for s in segs:
            s.value = 1 if s.attrs["AXDescription"] == "align left" else 0
            s.group = segs
        w = Node("AXWindow", "AXStandardWindow", children=[area, Node("AXGroup", children=segs)], AXTitle=title,
                 AXDocument=f"file:///tmp/{title}")
        w.segments = segs
        return w, ta

    w1, ta1 = window("fixture")
    ta1.attrs["AXFocused"] = True
    windows = [w1]
    if second_window:
        w2, _ = window("other")
        windows.append(w2)
    app = Node("AXApplication", AXFocusedWindow=w1, AXMainWindow=w1, AXWindows=windows, AXFocusedUIElement=ta1)
    return app


def _setup(app, **backend_kw):
    backend = FakeLive(app, **backend_kw)
    clock = Clock()
    obs, handles = O.observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=SAFARI)
    obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
    cache = ObservationCache(obs, handles, clock())
    return backend, clock, cache


def _idx(obs, role, label=None, window=0):
    return next(t.index for t in obs.targets if t.role == role and t.window_index == window
                and (label is None or t.label == label))


def _request(cache, op, index, args, step=1):
    t = cache.observation.targets[index]
    return ActionRequest(session_id="s", step=step, op=op, args=args, obs_id=cache.observation.obs_id, gate=ALLOW,
                         target=ResolvedTarget(target=t, method=ResolutionMethod.ORDINAL, obs_id=t.obs_id),
                         cancel_epoch=0, timeout_s=10)


def _run(backend, clock, cache, request, frontmost=AGREED, scope=SCOPE, utterance=UTTERANCE):
    def reobserve(pid=None):
        o, h = O.observe_app_with_handles(backend, backend.app, TEXTEDIT, settle=False, frontmost=SAFARI)
        return o.model_copy(update={"settle": o.settle.model_copy(update={"status": O.SettleStatus.SETTLED})}), h
    return execute_action(request, utterance=utterance, allowed_apps=scope, cache=cache, backend=backend,
                          frontmost=frontmost, reobserve=reobserve, clock=clock, sleep=clock.sleep,
                          now=lambda: 1_790_000_000.0)


# --- set_text ---

def test_set_text_inserts_exact_utterance_span_and_verifies():
    backend, clock, cache = _setup(textedit())
    ta = _idx(cache.observation, "AXTextArea")
    result, new_cache = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, ta, SetTextArgs(text_span=SPAN)))
    assert result.status is A.SUCCESS and result.verification is V.VERIFIED and backend.mutations == ["selected_text"]
    assert cache.handles.targets[ta].text == "EVIE_STEP4_TESTEVIE_FIXTURE_MARKER"
    assert result.evidence["exact_text"] is True and result.evidence["chars_after"] == 34
    assert new_cache.observation.obs_id != cache.observation.obs_id == result.evidence["obs_before"]
    assert result.evidence["obs_after"] == new_cache.observation.obs_id and result.mechanism == "ax.set_selected_text"
    assert "EVIE_STEP4" not in result.model_dump_json()          # text itself is never echoed back


def test_replace_all_restores_original_from_the_utterance():
    backend, clock, cache = _setup(textedit(text="EVIE_STEP4_TESTEVIE_FIXTURE_MARKER"))
    utterance = "restore EVIE_FIXTURE_MARKER"
    span = (8, 8 + len("EVIE_FIXTURE_MARKER"))
    req = _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"), SetTextArgs(text_span=span,
                                                                                         mode="replace_all"))
    result, _ = _run(backend, clock, cache, req, utterance=utterance)
    assert result.status is A.SUCCESS and backend.mutations == ["value"] and result.mechanism == "ax.set_value"


def test_wrong_resulting_text_is_not_success():
    backend, clock, cache = _setup(textedit(), wrong_text=True)
    result, _ = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, 0 + _idx(cache.observation, "AXTextArea"),
                                                     SetTextArgs(text_span=SPAN)))
    assert result.status is A.NOT_VERIFIABLE and result.evidence["exact_text"] is False


def test_no_change_is_failed_and_error_is_failed():
    backend, clock, cache = _setup(textedit(), ignore={"selected_text"})
    result, _ = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"),
                                                     SetTextArgs(text_span=SPAN)))
    assert result.status is A.FAILED and result.verification is V.VERIFIED_NO_CHANGE and result.noop
    backend, clock, cache = _setup(textedit(), fail={"selected_text": -25200})
    result, _ = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"),
                                                     SetTextArgs(text_span=SPAN)))
    assert result.status is A.FAILED and result.ax_error == -25200 and result.reason == "AX_ERROR:-25200"


def test_span_outside_utterance_is_blocked_before_touching_anything():
    backend, clock, cache = _setup(textedit())
    req = _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"), SetTextArgs(text_span=(5, 999)))
    result, _ = _run(backend, clock, cache, req)
    assert result.status is A.BLOCKED and result.reason == "TEXT_SPAN_OUTSIDE_UTTERANCE" and backend.mutations == []


# --- focus ---

def test_focus_is_not_live_and_touches_nothing():
    """Step 16A: focus is disabled; the executor refuses it before any AX call."""
    backend, clock, cache = _setup(textedit(second_window=True))
    target = _idx(cache.observation, "AXTextArea", window=1)
    result, _ = _run(backend, clock, cache, _request(cache, Op.FOCUS, target, NoArgs()))
    assert result.execution is not None


def _focus_runner(second_window, window_index):
    from services.computer_control.executor import _focus
    backend, clock, cache = _setup(textedit(second_window=second_window))
    ti = _idx(cache.observation, "AXTextArea", window=window_index)
    req = _request(cache, Op.FOCUS, ti, NoArgs())
    evidence = {}
    parts = _focus(req, cache.handles.targets[ti], cache.handles.windows[window_index], cache.handles, None,
                   backend, evidence)
    return backend, parts, evidence


def test_preserved_focus_runner_still_moves_focus_for_future_review():
    """The unreachable _focus implementation is kept intact (not deleted) for a future validation."""
    backend, (before, perform, after, verify, mechanism), evidence = _focus_runner(True, 1)
    assert perform() == 0 and backend.mutations == ["raise", "main", "focus"] and mechanism == "ax.raise_main_focus"
    assert verify(before, after()).status is V.VERIFIED and evidence["focused_after"] is True


def test_preserved_focus_runner_reports_no_change_on_an_already_focused_target():
    backend, (before, perform, after, verify, _), _ = _focus_runner(False, 0)
    perform()
    assert verify(before, after()).status is V.VERIFIED_NO_CHANGE


def test_focus_only_for_text_areas():
    backend, clock, cache = _setup(textedit())
    result, _ = _run(backend, clock, cache, _request(cache, Op.FOCUS, _idx(cache.observation, "AXScrollArea"),
                                                     NoArgs()))
    assert result.status is A.BLOCKED and backend.mutations == []


# --- scroll ---

def test_scroll_down_then_up_verified_by_movement():
    backend, clock, cache = _setup(textedit())
    area = _idx(cache.observation, "AXScrollArea")
    down, cache2 = _run(backend, clock, cache, _request(cache, Op.SCROLL, area, ScrollArgs(direction="down")))
    assert down.status is A.SUCCESS and down.evidence["visible_after"] == 250
    area2 = _idx(cache2.observation, "AXScrollArea")
    up, _ = _run(backend, clock, cache2, _request(cache2, Op.SCROLL, area2, ScrollArgs(direction="up"), step=2))
    assert up.status is A.SUCCESS and up.evidence["visible_after"] == 0


def test_scroll_at_limit_is_blocked_and_no_movement_fails():
    backend, clock, cache = _setup(textedit())
    area = _idx(cache.observation, "AXScrollArea")
    r, _ = _run(backend, clock, cache, _request(cache, Op.SCROLL, area, ScrollArgs(direction="up")))
    assert r.status is A.BLOCKED and r.reason == "AT_SCROLL_LIMIT" and backend.mutations == []
    backend, clock, cache = _setup(textedit(), ignore={"scroll"})
    r, _ = _run(backend, clock, cache, _request(cache, Op.SCROLL, area, ScrollArgs(direction="down")))
    assert r.status is A.FAILED


# --- press (alignment segments only) ---

def test_press_alignment_segment_expected_effect():
    backend, clock, cache = _setup(textedit())
    center = _idx(cache.observation, "AXCheckBox", "align center")
    r, cache2 = _run(backend, clock, cache, _request(cache, Op.PRESS, center, NoArgs()))
    assert r.status is A.SUCCESS and r.evidence["glyph_before"] == 204.0 and r.evidence["glyph_after"] == 429.6
    left = _idx(cache2.observation, "AXCheckBox", "align left")
    r2, _ = _run(backend, clock, cache2, _request(cache2, Op.PRESS, left, NoArgs(), step=2))
    assert r2.status is A.SUCCESS and backend.mutations == ["press", "press"]


def test_press_refuses_non_alignment_and_already_set():
    backend, clock, cache = _setup(textedit(bold=True))
    bold = _idx(cache.observation, "AXCheckBox", "bold")
    r, _ = _run(backend, clock, cache, _request(cache, Op.PRESS, bold, NoArgs()))
    assert r.status is A.BLOCKED and r.reason == "PRESS_ONLY_FOR_ALIGNMENT_SEGMENTS"
    backend, clock, cache = _setup(textedit())
    left = _idx(cache.observation, "AXCheckBox", "align left")
    r, _ = _run(backend, clock, cache, _request(cache, Op.PRESS, left, NoArgs()))
    assert r.status is A.BLOCKED and r.reason == "ALREADY_IN_REQUESTED_STATE" and backend.mutations == []


def test_press_state_mismatch_is_not_success():
    backend, clock, cache = _setup(textedit())
    center = _idx(cache.observation, "AXCheckBox", "align center")
    backend.press_alignment_segment = lambda seg: (backend.mutations.append("press"), 0)[1]   # no effect
    r, _ = _run(backend, clock, cache, _request(cache, Op.PRESS, center, NoArgs()))
    assert r.status is A.FAILED and r.verification is V.VERIFIED_NO_CHANGE


# --- freshness, gate, frontmost: nothing is touched ---

def test_frontmost_disagreement_fails_closed():
    backend, clock, cache = _setup(textedit())
    r, _ = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, 0 + _idx(cache.observation, "AXTextArea"),
                                                SetTextArgs(text_span=SPAN)), frontmost=DISAGREE)
    assert r.status is A.BLOCKED and r.reason.startswith("FRONTMOST_UNRESOLVED: DISAGREE") and backend.mutations == []


def test_stale_observation_and_targets():
    backend, clock, cache = _setup(textedit())
    ta = _idx(cache.observation, "AXTextArea")
    req = _request(cache, Op.SET_TEXT, ta, SetTextArgs(text_span=SPAN))
    newer = ObservationCache(cache.observation.model_copy(update={"obs_id": "obs-newer"}), cache.handles, clock())
    assert _run(backend, clock, newer, req)[0].reason == "OBSERVATION_NOT_LATEST"
    old = ObservationCache(cache.observation, cache.handles, clock() - 61)
    assert _run(backend, clock, old, req)[0].reason == "OBSERVATION_EXPIRED"
    cache.handles.targets[ta].gone = True
    r, _ = _run(backend, clock, cache, req)
    assert r.status is A.STALE and r.reason == "ELEMENT_GONE" and r.retry_permitted
    cache.handles.targets[ta].gone = False
    cache.handles.targets[ta].attrs["AXIdentifier"] = "Something Else"
    assert _run(backend, clock, cache, req)[0].reason == "TARGET_CHANGED"
    assert backend.mutations == []


def test_wrong_window_live():
    backend, clock, cache = _setup(textedit(second_window=True))
    backend.app.attrs["AXMainWindow"] = cache.handles.windows[1]          # user switched documents
    r, _ = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"),
                                                SetTextArgs(text_span=SPAN)))
    assert r.status is A.BLOCKED and r.reason == "NOT_MAIN_WINDOW_LIVE" and backend.mutations == []


def test_wrong_application_scope_and_forged_requests_are_blocked_by_the_workers_gate():
    backend, clock, cache = _setup(textedit())
    req = _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"), SetTextArgs(text_span=SPAN))
    r, _ = _run(backend, clock, cache, req, scope=frozenset({SAFARI.bundle_id}))
    assert r.status is A.BLOCKED and r.reason == "ASK:OUTSIDE_ALLOWED_APPS"
    # A forged "allowed" request for an unvalidated class: the worker's own gate re-check refuses it.
    backend2, clock2, cache2 = _setup(textedit())
    scroll_area = _idx(cache2.observation, "AXScrollArea")
    r, _ = _run(backend2, clock2, cache2, _request(cache2, Op.PRESS, scroll_area, NoArgs()))
    assert r.status is A.BLOCKED and r.reason == "TARGET_CLASS_NOT_VALIDATED"
    assert backend.mutations == [] and backend2.mutations == []


def test_secure_target_and_unknown_evie_status_are_blocked():
    app = textedit()
    secure = Node("AXTextField", subrole="AXSecureTextField", AXTitle="Password")
    app.attrs["AXMainWindow"].children.append(secure)
    backend, clock, cache = _setup(app)
    idx = _idx(cache.observation, "AXTextField")
    r, _ = _run(backend, clock, cache, _request(cache, Op.SET_TEXT, idx, SetTextArgs(text_span=SPAN)))
    assert r.status is A.BLOCKED and r.reason == "SECURE_FIELD"
    unknown = ObservationCache(cache.observation.model_copy(update={"evie_self": O.EvieSelfStatus.UNKNOWN}),
                               cache.handles, clock())
    r, _ = _run(backend, clock, unknown, _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"),
                                                  SetTextArgs(text_span=SPAN)))
    assert r.reason == "EVIE_STATUS_UNKNOWN" and backend.mutations == []


def test_disabled_op_and_unsettled_observation():
    backend, clock, cache = _setup(textedit())
    unsettled = ObservationCache(cache.observation.model_copy(update={"settle": cache.observation.settle.model_copy(
        update={"status": O.SettleStatus.GROWING})}), cache.handles, clock())
    r, _ = _run(backend, clock, unsettled, _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"),
                                                    SetTextArgs(text_span=SPAN)))
    assert r.reason == "OBSERVATION_UNRELIABLE" and backend.mutations == []


def test_no_retry_after_failure():
    backend, clock, cache = _setup(textedit(), fail={"selected_text": -25200})
    req = _request(cache, Op.SET_TEXT, _idx(cache.observation, "AXTextArea"), SetTextArgs(text_span=SPAN))
    first, new_cache = _run(backend, clock, cache, req)
    again, _ = _run(backend, clock, new_cache, req)           # same request again: its observation is spent
    assert first.status is A.FAILED and again.status is A.STALE and backend.mutations == ["selected_text"]


# --- end to end with the pure session (one action) ---

def test_session_to_worker_executor_round_trip():
    backend, clock, cache = _setup(textedit())
    session = ComputerControlSession("sess", UTTERANCE, SCOPE, clock=clock)
    session.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    assert session.observe(cache.observation) is SessionState.CHOOSING
    ta = _idx(cache.observation, "AXTextArea")
    req = session.propose({"status": "ACTION", "op": "set_text", "target_index": ta,
                           "args": {"text_span": list(SPAN)}}, cache.observation.obs_id)
    assert session.authorize(req)
    result, _ = _run(backend, clock, cache, req, scope=session.allowed_apps)
    assert result.status is A.SUCCESS and session.record_result(result) is SessionState.OBSERVING
