"""
Final computer-control completion: generic semantic click, generally verifiable scroll, safe clipboard,
running+installed app de-duplication (the Safari AMBIGUOUS_APP bug) and post-launch pid resolution
(the Photo Booth TARGET_PROCESS_EXITED bug). Fakes only: no macOS, no real events, no real pasteboard.
"""

import pytest

from cc_fixtures import PLAIN_BUTTON, SAFARI, TEXT_AREA, TEXTEDIT, observation
from services.computer_control import observer as O
from services.computer_control.chooser import AppRef, AskDecision, BlockedDecision, ValidatedAction, build_input
from services.computer_control.chooser_boundary import choose
from services.computer_control.executor import ObservationCache, execute_action
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.hardening import harden
from services.computer_control.models import (
    ActionRequest, ActionStatus as A, ActivateAppArgs, AppIdentity, ClickElementArgs, GateDecision, GateOutcome,
    NoArgs, Op, ResolutionMethod, ResolvedTarget, RiskLevel, ScrollArgs, VerificationStatus as V,
)
from services.computer_control.verification import ClickVerifier, ClipboardVerifier, PasteVerifier
from test_computer_control_executor import Clock, FakeLive, _idx, _request, textedit
from test_computer_control_observer import Node

FRONT_TE = cross_check([FrontmostReading(source="nsworkspace", app=TEXTEDIT),
                        FrontmostReading(source="app_ax_frontmost", app=TEXTEDIT)])
SCOPE = frozenset({TEXTEDIT.bundle_id})
ALLOW = GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.LOW, reason="LOW_ALLOWED")
CLIP = "zq9clip"


class Live(FakeLive):
    """FakeLive + the process/pasteboard/press surface the completion ops use."""

    def __init__(self, app, clip=CLIP, concealed=False, deliver=True, press_effect=True, control_state=False,
                 **kw):
        super().__init__(app, **kw)
        self.clip, self.concealed, self.deliver, self.press_effect = clip, concealed, deliver, press_effect
        self.change_count, self.posted = 7, []
        if control_state:
            self.control_state = lambda el: (el.attrs.get("AXValue"),)

    def running_pid(self, bundle_id):
        return TEXTEDIT.pid

    def is_active(self, pid):
        return True

    def app_reports_frontmost(self, pid):
        return True

    def is_secure_element(self, el):
        return el.attrs.get("AXSubrole") == "AXSecureTextField"

    def selected_text_length(self, el):
        loc, ln = el.sel
        return ln

    def pasteboard_change_count(self):
        return self.change_count

    def pasteboard_text(self):
        return (True, None) if self.concealed else (False, self.clip)

    def post_paste_keys(self, pid):
        self.posted.append((pid, "paste"))
        self.mutations.append("paste")
        el = self.app.attrs["AXFocusedUIElement"]
        if self.deliver:
            loc, ln = el.sel
            el.text = el.text[:loc] + self.clip + el.text[loc + ln:]
            el.sel = (loc + len(self.clip), 0)
        return 0

    def post_copy_keys(self, pid):
        self.posted.append((pid, "copy"))
        self.mutations.append("copy")
        el = self.app.attrs["AXFocusedUIElement"]
        if self.deliver:
            loc, ln = el.sel
            self.clip, self.change_count = el.text[loc:loc + ln], self.change_count + 1
        return 0

    def perform_press(self, el):
        self.mutations.append("press")
        if self.press_effect and getattr(el, "group", None):
            for s in el.group:
                s.value = 1 if s is el else 0
                s.attrs["AXValue"] = s.value
        return 0


def _setup(app=None, sel=(0, 0), **kw):
    app = app or textedit()
    backend = Live(app, **kw)
    ta = app.attrs["AXFocusedUIElement"]
    ta.sel = sel
    for seg in app.attrs["AXMainWindow"].segments:
        seg.attrs["AXValue"] = seg.value
    clock = Clock()
    obs, handles = O.observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
    obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
    return backend, clock, ObservationCache(obs, handles, clock())


def _exec(backend, clock, cache, req, frontmost=FRONT_TE, utterance="do it"):
    def reobserve(pid=None):
        o, h = O.observe_app_with_handles(backend, backend.app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
        return o.model_copy(update={"settle": o.settle.model_copy(update={"status": O.SettleStatus.SETTLED})}), h
    return execute_action(req, utterance=utterance, allowed_apps=SCOPE, cache=cache, backend=backend,
                          frontmost=frontmost, reobserve=reobserve, clock=clock, sleep=clock.sleep,
                          now=lambda: 1_790_000_000.0)


def _untargeted(cache, op):
    return ActionRequest(session_id="s", step=1, op=op, args=NoArgs(), obs_id=cache.observation.obs_id, gate=ALLOW,
                         target=None, cancel_epoch=0, timeout_s=10)


# --- 1. generic semantic click: grammar ---

def _pick(utterance, targets=(TEXT_AREA, PLAIN_BUTTON)):
    return choose(utterance, observation(targets, obs_id="c1")).decision


@pytest.mark.parametrize("utterance", ["click OK", "press OK", "tap the OK button", "click on OK", "Click ok."])
def test_click_resolves_the_one_control_with_that_visible_label(utterance):
    d = _pick(utterance)
    assert isinstance(d, ValidatedAction) and d.op is Op.CLICK_ELEMENT and d.target_index == 1


def test_click_never_guesses():
    second = dict(PLAIN_BUTTON)
    assert isinstance(_pick("click OK", (TEXT_AREA, PLAIN_BUTTON, second)), AskDecision)            # two "OK"s
    assert _pick("click Submit").reason == "CONTROL_NOT_FOUND"                                     # no such label
    assert _pick("click that").reason == "UNRESOLVED_REFERENCE"
    assert _pick("double-click OK").reason == "DOUBLE_CLICK_NOT_ENABLED"
    disabled = dict(PLAIN_BUTTON, enabled=False)
    assert _pick("click OK", (TEXT_AREA, disabled)).reason == "CONTROL_DISABLED"
    secure = {"role": "AXTextField", "subrole": "AXSecureTextField", "label": "Password"}
    assert _pick("click Password", (TEXT_AREA, secure)).reason == "CONTROL_NOT_FOUND"            # never secure


def test_destructive_label_click_is_blocked_by_the_gate_not_the_grammar():
    delete = dict(PLAIN_BUTTON, label="Delete")
    d = _pick("click Delete", (TEXT_AREA, delete))
    assert isinstance(d, ValidatedAction)                                     # grammar resolves; the gate decides
    from services.computer_control.gate import context_for, evaluate
    obs = observation((TEXT_AREA, delete), obs_id="c1")
    t = ResolvedTarget(target=obs.targets[1], method=ResolutionMethod.ORDINAL, obs_id="c1")
    assert evaluate(context_for(Op.CLICK_ELEMENT, ClickElementArgs(), t, obs, SCOPE)).reason == "DESTRUCTIVE_LABEL"


# --- 2. generic click: executor verification ---

def _click(backend, clock, cache, label="align center"):
    idx = _idx(cache.observation, "AXCheckBox", label)
    return _exec(backend, clock, cache, _request(cache, Op.CLICK_ELEMENT, idx, ClickElementArgs()))


def test_click_verified_by_the_reobserved_ui():
    backend, clock, cache = _setup()
    r, _ = _click(backend, clock, cache)
    assert r.status is A.SUCCESS and r.reason is None and backend.mutations == ["press"]
    assert r.evidence["verifier"] == "UI_STATE_CHANGED" and r.mechanism == "ax.perform_press"


def test_click_verified_by_the_control_state():
    backend, clock, cache = _setup(control_state=True)
    r, _ = _click(backend, clock, cache)
    assert r.status is A.SUCCESS and r.evidence["verifier"] == "CONTROL_STATE_CHANGED"


def test_click_without_any_observable_change_is_not_success():
    backend, clock, cache = _setup(press_effect=False, control_state=True)
    r, _ = _click(backend, clock, cache)
    assert r.status is A.NOT_VERIFIABLE and r.reason == "NO_OBSERVABLE_CHANGE" and r.noop


def test_click_verifier_contract():
    assert ClickVerifier().verify(False, False).status is V.INCONCLUSIVE
    assert ClickVerifier().verify(False, True).status is V.VERIFIED


# --- 3. scroll beyond text views ---

def _list_app(offset_moves=True):
    """A scroll area holding a list (no text view): the content's AXPosition moves when the bar moves."""
    rows = Node("AXList", AXPosition=(0.0, 100.0))
    area = Node("AXScrollArea", children=[rows], actions=["AXScrollDownByPage"], AXPosition=(0.0, 100.0))
    area.bar = type("Bar", (), {"value": 0.0})()
    ta = Node("AXTextArea", AXFocused=True, AXValue="", settable=("AXSelectedText",))
    ta.text, ta.sel = "", (0, 0)
    w = Node("AXWindow", "AXStandardWindow", children=[area, ta], AXTitle="list", AXDocument="file:///tmp/list")
    w.segments = []
    app = Node("AXApplication", AXFocusedWindow=w, AXMainWindow=w, AXWindows=[w], AXFocusedUIElement=ta)
    return app, area, rows, offset_moves


class ListLive(Live):
    def __init__(self, app, rows, moves, **kw):
        super().__init__(app, **kw)
        self.rows, self.moves = rows, moves

    def visible_start(self, area):
        return None                                                        # no text view in this scroll area

    def scroll_content_offset(self, area):
        return int(area.attrs["AXPosition"][1] - self.rows.attrs["AXPosition"][1])

    def set_scroll_value(self, bar, value):
        def go():
            bar.value = value
            if self.moves:
                self.rows.attrs["AXPosition"] = (0.0, 100.0 - 2000 * value)
        return self._mutate("scroll", go)


def _scroll_list(moves=True, direction="down"):
    app, area, rows, moves = _list_app(moves)
    backend = ListLive(app, rows, moves)
    clock = Clock()
    obs, handles = O.observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
    obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
    cache = ObservationCache(obs, handles, clock())
    req = _request(cache, Op.SCROLL, _idx(cache.observation, "AXScrollArea"), ScrollArgs(direction=direction,
                                                                                         amount=0.25))
    return _exec(backend, clock, cache, req), backend


def test_list_scroll_is_verified_by_the_content_position():
    (r, _), backend = _scroll_list()
    assert r.status is A.SUCCESS and r.evidence["scroll_signal"] == "content_offset"
    assert (r.evidence["visible_before"], r.evidence["visible_after"]) == (0, 500)


def test_list_scroll_without_movement_is_failed_not_success():
    (r, _), _ = _scroll_list(moves=False)
    assert r.status is A.FAILED and r.verification is V.VERIFIED_NO_CHANGE


def test_text_scroll_still_uses_the_visible_character_range():
    backend, clock, cache = _setup()
    req = _request(cache, Op.SCROLL, _idx(cache.observation, "AXScrollArea"), ScrollArgs(direction="down",
                                                                                         amount=0.25))
    r, _ = _exec(backend, clock, cache, req)
    assert r.status is A.SUCCESS and r.evidence["scroll_signal"] == "visible_text"


# --- 4. clipboard ---

def test_copy_is_verified_by_pasteboard_metadata_only():
    backend, clock, cache = _setup(sel=(0, 4))
    r, _ = _exec(backend, clock, cache, _untargeted(cache, Op.COPY_SELECTION))
    assert r.status is A.SUCCESS and r.evidence["verifier"] == "CLIPBOARD_HOLDS_SELECTION"
    assert r.evidence["clipboard_changed"] is True and r.evidence["selection_chars"] == 4
    assert "EVIE" not in r.model_dump_json()                         # the copied text is never in the result


@pytest.mark.parametrize("kw, sel, reason", [
    ({}, (0, 0), "NO_TEXT_SELECTED"),
    ({"deliver": False}, (0, 4), "CLIPBOARD_UNCHANGED"),
])
def test_copy_needs_a_selection_and_a_real_pasteboard_change(kw, sel, reason):
    backend, clock, cache = _setup(sel=sel, **kw)
    r, _ = _exec(backend, clock, cache, _untargeted(cache, Op.COPY_SELECTION))
    assert reason in (r.reason or "") and r.status is not A.SUCCESS


def test_copy_from_a_secure_field_is_blocked_before_any_event():
    backend, clock, cache = _setup(sel=(0, 4))
    backend.app.attrs["AXFocusedUIElement"].attrs["AXSubrole"] = "AXSecureTextField"
    r, _ = _exec(backend, clock, cache, _untargeted(cache, Op.COPY_SELECTION))
    assert r.reason == "SECURE_FIELD" and backend.posted == []


def test_copy_requires_the_observed_app_to_be_frontmost():
    backend, clock, cache = _setup(sel=(0, 4))
    safari = cross_check([FrontmostReading(source="nsworkspace", app=SAFARI),
                          FrontmostReading(source="app_ax_frontmost", app=SAFARI)])
    r, _ = _exec(backend, clock, cache, _untargeted(cache, Op.COPY_SELECTION), frontmost=safari)
    assert r.reason == "TARGET_APP_NOT_FRONTMOST" and backend.posted == []


def _paste(backend, clock, cache):
    req = _request(cache, Op.PASTE_CLIPBOARD, _idx(cache.observation, "AXTextArea"), NoArgs())
    return _exec(backend, clock, cache, req)


def test_paste_is_verified_by_the_exact_resulting_text():
    backend, clock, cache = _setup(sel=(0, 0))
    r, _ = _paste(backend, clock, cache)
    assert r.status is A.SUCCESS and r.evidence["exact_text"] is True and r.evidence["clipboard_chars"] == len(CLIP)
    assert backend.posted == [(TEXTEDIT.pid, "paste")] and CLIP not in r.model_dump_json()


@pytest.mark.parametrize("kw, reason", [({"concealed": True}, "CLIPBOARD_CONCEALED"),
                                        ({"clip": ""}, "CLIPBOARD_HAS_NO_TEXT")])
def test_paste_refuses_concealed_or_empty_clipboards(kw, reason):
    backend, clock, cache = _setup(**kw)
    r, _ = _paste(backend, clock, cache)
    assert r.reason == reason and backend.posted == []


def test_paste_with_no_effect_is_failed():
    backend, clock, cache = _setup(deliver=False)
    r, _ = _paste(backend, clock, cache)
    assert r.status is A.FAILED and r.verification is V.VERIFIED_NO_CHANGE


def test_paste_needs_the_target_focused_and_frontmost():
    backend, clock, cache = _setup()
    backend.app.attrs["AXFocusedUIElement"] = None
    r, _ = _paste(backend, clock, cache)
    assert r.reason == "TEXT_AREA_NOT_FOCUSED_LIVE" and backend.posted == []


def test_clipboard_grammar():
    obs = observation((TEXT_AREA, PLAIN_BUTTON), obs_id="p1")
    assert choose("copy", obs).decision.op is Op.COPY_SELECTION
    assert choose("copy the selected text", obs).decision.op is Op.COPY_SELECTION
    d = choose("paste it here", obs).decision
    assert d.op is Op.PASTE_CLIPBOARD and d.target_index == 0
    assert choose("copy my password to notes", obs).decision.reason == "UNSUPPORTED_COPY_FORM"


def test_clipboard_verifiers():
    assert ClipboardVerifier().verify(False, False).status is V.VERIFIED_NO_CHANGE
    assert ClipboardVerifier().verify(True, False).status is V.INCONCLUSIVE
    assert PasteVerifier().verify("a", "aX", "aX").status is V.VERIFIED
    assert PasteVerifier().verify("a", "aY", "aX").status is V.INCONCLUSIVE


# --- 5. Safari AMBIGUOUS_APP: one bundle listed as running AND installed is not two apps ---

def test_running_and_installed_copy_of_one_app_is_not_ambiguous():
    safari = AppIdentity(bundle_id="com.apple.Safari", pid=300, name="Safari")
    obs = observation((TEXT_AREA,), obs_id="s1", running=(TEXTEDIT, safari))
    installed = (AppRef(bundle_id="com.apple.Safari", name="Safari"),)
    ci = build_input("switch to Safari", obs)
    decision = ValidatedAction(op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.Safari"), obs_id="s1")
    h = harden(ci, decision, installed_apps=installed)
    assert h.outcome == "SAFE_NORMALIZED_ACTION", h.findings


def test_two_different_bundles_with_one_name_are_still_ambiguous():
    obs = observation((TEXT_AREA,), obs_id="s2", running=(TEXTEDIT, SAFARI))
    installed = (AppRef(bundle_id="com.example.Safari", name="Safari"),)
    ci = build_input("switch to Safari", obs)
    decision = ValidatedAction(op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.Safari"), obs_id="s2")
    assert harden(ci, decision, installed_apps=installed).outcome == "ASK"


# --- 6. Photo Booth TARGET_PROCESS_EXITED: a launched app's pid is resolved after the launch ---

class Launch:
    """Not running at request time; launch_app starts it with a NEW pid that appears after a delay."""

    def __init__(self, clock, appear_after=3.0):
        self.clock, self.appear_after, self.launched_at, self.pid = clock, appear_after, None, 4242
        self.app = AppIdentity(bundle_id="com.apple.PhotoBooth", pid=self.pid, name="Photo Booth")

    def _up(self):
        return self.launched_at is not None and self.clock() - self.launched_at >= self.appear_after

    def launch_app(self, bundle_id):
        self.launched_at = self.clock()
        return True

    def running_pid(self, bundle_id):
        return self.pid if self._up() else None

    def is_running(self, pid):
        return self._up() and pid == self.pid

    def is_active(self, pid):
        return self._up()

    def app_reports_frontmost(self, pid):
        return self._up()

    def check(self):
        front = self.app if self._up() else TEXTEDIT
        return cross_check([FrontmostReading(source="nsworkspace", app=front),
                            FrontmostReading(source="app_ax_frontmost", app=front)])


def test_slow_launch_resolves_the_new_pid_and_verifies():
    clock = Clock()
    p = Launch(clock)
    obs = observation((TEXT_AREA,), obs_id="l1", running=(TEXTEDIT,))
    cache = ObservationCache(obs, O.ObservationHandles(), clock())
    req = ActionRequest(session_id="s", step=1, op=Op.ACTIVATE_APP, obs_id="l1", gate=ALLOW, cancel_epoch=0,
                        timeout_s=10, args=ActivateAppArgs(bundle_id="com.apple.PhotoBooth"))
    r, _ = execute_action(req, utterance="open Photo Booth", allowed_apps=frozenset({"com.apple.PhotoBooth",
                                                                                     TEXTEDIT.bundle_id}),
                          cache=cache, backend=p, frontmost=FRONT_TE, reobserve=lambda pid: None,
                          recheck_frontmost=p.check, clock=clock, sleep=clock.sleep, now=lambda: 1_790_000_000.0)
    assert r.status is A.SUCCESS and r.evidence["launched"] is True and r.evidence["target_pid_after"] == 4242


def test_launch_whose_process_never_appears_is_not_success():
    clock = Clock()
    p = Launch(clock, appear_after=999)
    obs = observation((TEXT_AREA,), obs_id="l2", running=(TEXTEDIT,))
    cache = ObservationCache(obs, O.ObservationHandles(), clock())
    req = ActionRequest(session_id="s", step=1, op=Op.ACTIVATE_APP, obs_id="l2", gate=ALLOW, cancel_epoch=0,
                        timeout_s=10, args=ActivateAppArgs(bundle_id="com.apple.PhotoBooth"))
    r, _ = execute_action(req, utterance="open Photo Booth", allowed_apps=frozenset({"com.apple.PhotoBooth",
                                                                                     TEXTEDIT.bundle_id}),
                          cache=cache, backend=p, frontmost=FRONT_TE, reobserve=lambda pid: None,
                          recheck_frontmost=p.check, clock=clock, sleep=clock.sleep, now=lambda: 1_790_000_000.0)
    assert r.status is A.NOT_VERIFIABLE and r.reason == "TARGET_PROCESS_NOT_FOUND"


# --- 7. select all (selection for copy) ---

class SelLive(Live):
    def set_selected_range(self, el, location, length):
        self.mutations.append("select_range")
        el.sel = (location, length)
        return 0


def test_select_all_selects_the_whole_text_and_verifies_by_read_back():
    from services.computer_control.models import SelectTextArgs
    app = textedit()
    backend = SelLive(app)
    for seg in app.attrs["AXMainWindow"].segments:
        seg.attrs["AXValue"] = seg.value
    clock = Clock()
    obs, handles = O.observe_app_with_handles(backend, app, TEXTEDIT, settle=False, frontmost=TEXTEDIT)
    obs = obs.model_copy(update={"settle": obs.settle.model_copy(update={"status": O.SettleStatus.SETTLED})})
    cache = ObservationCache(obs, handles, clock())
    req = _request(cache, Op.SELECT_TEXT, _idx(cache.observation, "AXTextArea"),
                   SelectTextArgs(location=0, length=100_000))
    r, _ = _exec(backend, clock, cache, req)
    n = len("EVIE_FIXTURE_MARKER")
    assert r.status is A.SUCCESS and r.evidence["range_after"] == f"0,{n}" and r.evidence["text_unchanged"] is True


def test_select_all_grammar():
    d = choose("select all", observation((TEXT_AREA, PLAIN_BUTTON), obs_id="a1")).decision
    assert d.op is Op.SELECT_TEXT and d.target_index == 0 and d.args.location == 0


# --- 8. live findings: candidates outside the main window; unlabelled controls named by identifier ---

def _calc_obs():
    names = ["AllClear", "Seven", "Add", "Delete", "Equals"]
    buttons = tuple({"role": "AXButton", "identifier": n, "enabled": True, "actions": ("AXPress",)} for n in names)
    return observation(buttons, obs_id="k1")


@pytest.mark.parametrize("utterance, identifier", [("click 7", "Seven"), ("press seven", "Seven"),
                                                   ("click all clear", "AllClear"), ("tap equals", "Equals"),
                                                   ("Click AllClear.", "AllClear"), ("click allclear", "AllClear")])
def test_unlabelled_controls_are_named_by_their_identifier(utterance, identifier):
    obs = _calc_obs()
    d = choose(utterance, obs).decision
    assert isinstance(d, ValidatedAction) and obs.targets[d.target_index].identifier == identifier


def test_identifier_named_destructive_control_is_blocked_by_the_gate():
    from services.computer_control.gate import context_for, evaluate
    obs = _calc_obs()
    d = choose("click delete", obs).decision
    t = ResolvedTarget(target=obs.targets[d.target_index], method=ResolutionMethod.ORDINAL, obs_id="k1")
    assert evaluate(context_for(Op.CLICK_ELEMENT, ClickElementArgs(), t, obs, SCOPE)).reason == "DESTRUCTIVE_LABEL"


def test_a_second_non_main_window_does_not_make_the_main_window_target_ambiguous():
    from cc_fixtures import window
    second = dict(TEXT_AREA, focused=False, window_index=1)
    first = dict(TEXT_AREA, window_index=0)
    obs = observation((first, second), obs_id="w2", windows=(window(main=True, focused=True),
                                                             window(doc=None, main=False, focused=False)))
    d = choose("select all", obs).decision
    assert isinstance(d, ValidatedAction) and d.target_index == 0
    d = choose("type hi", obs).decision
    assert isinstance(d, ValidatedAction) and d.target_index == 0


# --- 9. live finding: every op's request survives the worker's JSON boundary (QUIT_APP did not) ---

def test_shape_sharing_args_round_trip_through_the_worker_protocol():
    from services.computer_control.models import CloseWindowArgs, QuitAppArgs, SelectTextArgs
    from services.computer_control.worker import ActCommand, _TO_WORKER
    cases = [(Op.QUIT_APP, QuitAppArgs(bundle_id="com.apple.calculator"), None),
             (Op.SWITCH_APP, ActivateAppArgs(bundle_id="com.apple.calculator"), None),
             (Op.ACTIVATE_APP, ActivateAppArgs(bundle_id="com.apple.calculator"), None),
             (Op.CLOSE_WINDOW, CloseWindowArgs(), None),
             (Op.COPY_SELECTION, NoArgs(), None)]
    for op, args, target in cases:
        req = ActionRequest(session_id="s", step=1, op=op, args=args, obs_id="o", gate=ALLOW, cancel_epoch=0,
                            timeout_s=10, target=target)
        line = ActCommand(request=req, utterance="u", allowed_apps=["com.apple.calculator"]).model_dump_json()
        back = _TO_WORKER.validate_json(line)
        assert type(back.request.args) is type(args), op


# --- 10. live finding: quitting the frontmost app ends DONE_VERIFIED, checked on a fresh Finder observation ---

def test_quitting_the_observed_app_is_verified_on_a_fresh_observation_of_finder():
    from test_computer_control_session_integration import World, run as run_session
    from services.computer_control.models import ActionResult as R, ExecutionStatus as E
    from services.computer_control.session import SessionState as S

    class QuitWorld(World):
        def observe(self, bundle_id):
            if all(a.bundle_id != bundle_id for a in self.running):
                return O.ObserveResult(status="UNAVAILABLE", reason="APP_NOT_RUNNING")
            return super().observe(bundle_id)

        def act(self, request, utterance, allowed_apps):
            self.log.append(("act", request.op.value, request.obs_id))
            self.running = tuple(a for a in self.running if a.bundle_id != request.args.bundle_id)
            self.front = next(a for a in self.running if a.bundle_id == "com.apple.finder")
            return R.executed(execution=E.EXECUTED, verification=V.VERIFIED, fingerprint_changed=True,
                              session_id=request.session_id, step=request.step, op=request.op, requested_at=1.0)

    world = QuitWorld()
    result, world, _ = run_session("quit TextEdit", world)
    assert result.final_state is S.DONE_VERIFIED and [a[1] for a in world.acts()] == ["quit_app"]
