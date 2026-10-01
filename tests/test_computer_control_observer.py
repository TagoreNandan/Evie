"""
Read-only AX observer (docs/07 Section 7), with fake AX nodes. No macOS, no PyObjC.
The fake backend records every call so tests can prove what was (not) read.
"""

import json
from collections import Counter

import pytest
from pydantic import ValidationError

from cc_fixtures import TEXTEDIT, observation
from services.computer_control import macos_ax, observer as O
from services.computer_control.gate import EVIE_UI_TITLE, GatePolicy, context_for, evaluate
from services.computer_control.models import (
    AXErrorRecord, AppIdentity, EvieSelfStatus, NoArgs, Op, ResolutionMethod, ResolvedTarget, Sensitivity,
    SettleStatus as S,
)

NO_VALUE, UNSUPPORTED, CANNOT_COMPLETE, FAILURE = -25212, -25205, -25204, -25200


class Node:
    def __init__(self, role, subrole=None, children=None, actions=(), settable=(), errors=None, **attrs):
        self.attrs = {"AXRole": role, **({"AXSubrole": subrole} if subrole else {}), **attrs}
        self.children = list(children) if children is not None else []
        self.actions, self.settable, self.errors = list(actions), set(settable), dict(errors or {})

    def __repr__(self):
        return f"<Node {self.attrs.get('AXRole')}>"


class FakeBackend:
    def __init__(self, clock=None):
        self.reads, self.clock = [], clock

    def _tick(self):
        if self.clock:
            self.clock.advance(0.01)

    def copy(self, el, attr):
        self._tick()
        self.reads.append((el.attrs.get("AXRole"), attr))
        if attr in el.errors:
            return el.errors[attr], None
        if attr in el.attrs:
            return 0, el.attrs[attr]
        assert attr in {"AXFocusedWindow", "AXMainWindow", "AXWindows", "AXTitle"}, f"unsupported read {attr}"
        return UNSUPPORTED, None

    def children(self, el, limit):
        if "AXChildren" in el.errors:
            return el.errors["AXChildren"], [], 0
        return 0, el.children[:limit], len(el.children)

    def attribute_names(self, el):
        names = [k for k in list(el.attrs) + list(el.errors) if k != "AXChildren"]
        return 0, names + ["AXChildren"]

    def action_names(self, el):
        return 0, el.actions

    def is_settable(self, el, attr):
        return 0, attr in el.settable

    def key(self, el):
        return id(el)

    def same(self, a, b):
        return a is b


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _window(*children, title="Scratch", doc="file:///tmp/scratch.txt", **kw):
    return Node("AXWindow", "AXStandardWindow", children=children, AXTitle=title, AXDocument=doc, **kw)


def _app(focused=None, main=None, windows=None):
    attrs = {}
    if focused is not None:
        attrs["AXFocusedWindow"] = focused
    if main is not None:
        attrs["AXMainWindow"] = main
    if windows is not None:
        attrs["AXWindows"] = windows
    return Node("AXApplication", **attrs)


def BUTTON(label, **kw):
    attrs = {"AXTitle": label, "AXEnabled": True, "AXPosition": (10.0, 10.0), "AXSize": (20.0, 20.0), **kw}
    return Node("AXButton", actions=["AXPress"], **attrs)


def _observe(app_el, backend=None, app=TEXTEDIT, limits=O.DEFAULT_LIMITS, policy=None, screens=((0, 0, 1440, 900),),
             **kw):
    backend = backend or FakeBackend()
    return O.observe_app(backend, app_el, app, frontmost=app, screens=screens, limits=limits,
                         policy=policy or GatePolicy(), settle=False, **kw), backend


# --- roots ---

def test_focused_window_is_the_primary_root():
    w = _window(BUTTON("OK"))
    obs, _ = _observe(_app(focused=w, main=w, windows=[w]))
    assert len(obs.windows) == 1 and obs.window.is_focused and obs.window.is_main
    assert obs.targets[0].label == "OK" and obs.targets[0].window_index == 0


def test_main_window_fallback_when_nothing_focused():
    w = _window(BUTTON("OK"))
    obs, _ = _observe(_app(main=w))
    assert obs.window is not None and obs.window.is_main and not obs.window.is_focused


def test_axwindows_fallback_and_windows_are_roots_not_the_app():
    w = _window(BUTTON("OK"))
    obs, backend = _observe(_app(windows=[w]))
    assert [t.label for t in obs.targets] == ["OK"]
    assert obs.window is None                          # neither focused nor main: no primary is claimed
    assert ("AXApplication", "AXChildren") not in backend.reads


def test_duplicate_roots_are_removed():
    w1, w2 = _window(BUTTON("A"), title="One"), _window(BUTTON("B"), title="Two")
    obs, _ = _observe(_app(focused=w1, main=w1, windows=[w1, w2, w1]))
    assert len(obs.windows) == 2 and [t.label for t in obs.targets] == ["A", "B"]
    assert [t.window_index for t in obs.targets] == [0, 1]


def test_window_identity_uses_hashes_not_titles_or_paths():
    obs, _ = _observe(_app(focused=_window(title="Quarterly plan", doc="file:///Users/me/plan.txt")))
    blob = obs.model_dump_json()
    assert "Quarterly" not in blob and "plan.txt" not in blob
    assert obs.window.title_hash and obs.window.document_hash and obs.window.pid == TEXTEDIT.pid
    assert "cg_window_id" not in json.loads(blob)["window"]


# --- caps ---

def test_depth_cap_is_recorded_and_incomplete():
    deep = BUTTON("leaf")
    for _ in range(6):
        deep = Node("AXGroup", children=[deep])
    obs, _ = _observe(_app(focused=_window(deep)), limits=O.ObserverLimits(max_depth=3))
    assert "depth" in obs.caps_hit and not obs.complete and obs.degraded


def test_node_cap_is_recorded():
    obs, _ = _observe(_app(focused=_window(*[BUTTON(str(i)) for i in range(20)])),
                      limits=O.ObserverLimits(max_nodes=5))
    assert "nodes" in obs.caps_hit and obs.counts.total_nodes == 5


def test_time_budget_is_recorded():
    clock = Clock()
    backend = FakeBackend(clock=clock)
    obs, _ = _observe(_app(focused=_window(*[BUTTON(str(i)) for i in range(50)])), backend=backend,
                      limits=O.ObserverLimits(time_budget_s=0.2), clock=clock)
    assert "time" in obs.caps_hit and not obs.complete


def test_bounded_children_slice_is_recorded():
    obs, _ = _observe(_app(focused=_window(*[BUTTON(str(i)) for i in range(10)])),
                      limits=O.ObserverLimits(max_children=4))
    assert "children" in obs.caps_hit and obs.counts.total_nodes == 5   # window + 4 children


def test_cycle_protection():
    group = Node("AXGroup")
    group.children = [BUTTON("x"), group]              # refers back to itself
    w = _window(group, group)
    obs, _ = _observe(_app(focused=w))
    assert obs.counts.total_nodes == 3 and obs.complete


# --- errors ---

def test_axchildren_error_is_recorded_with_role_and_code():
    obs, _ = _observe(_app(focused=_window(Node("AXTextField", errors={"AXChildren": FAILURE}))))
    assert AXErrorRecord(attribute="AXChildren", role="AXTextField", code=FAILURE, count=1) in obs.ax_errors
    assert obs.counts.ax_errors == 1


def test_no_value_is_not_an_error_and_unsupported_attributes_are_not_read():
    node = Node("AXStaticText", errors={"AXValue": NO_VALUE})
    obs, backend = _observe(_app(focused=_window(node)))
    assert obs.counts.ax_errors == 0
    assert ("AXStaticText", "AXIdentifier") not in backend.reads       # not in its attribute names


def test_other_attribute_errors_are_counted():
    obs, _ = _observe(_app(focused=_window(Node("AXImage", errors={"AXDescription": CANNOT_COMPLETE}))))
    assert AXErrorRecord(attribute="AXDescription", role="AXImage", code=CANNOT_COMPLETE, count=1) in obs.ax_errors


# --- exclusions ---

def test_secure_field_is_never_read_or_descended():
    secret = Node("AXTextField", subrole="AXSecureTextField", AXTitle="Password", AXValue="hunter2",
                  AXSelectedText="hunter2", children=[Node("AXStaticText", AXValue="leak")])
    obs, backend = _observe(_app(focused=_window(secret)))
    t = obs.targets[0]
    assert t.is_secure and t.value_summary is None and Sensitivity.SECURE_FIELD in t.sensitivity
    assert ("AXTextField", "AXValue") not in backend.reads and ("AXTextField", "AXSelectedText") not in backend.reads
    assert "hunter2" not in obs.model_dump_json() and "leak" not in obs.model_dump_json()
    assert obs.counts.secure_nodes == 1 and obs.excluded["secure"] == 1


def test_document_text_is_never_summarised():
    obs, backend = _observe(_app(focused=_window(Node("AXTextArea", AXValue="private letter", AXSelectedText="",
                                                      settable=("AXSelectedText",)))))
    assert ("AXTextArea", "AXValue") not in backend.reads and obs.targets[0].value_summary is None
    assert obs.targets[0].settable == ("AXSelectedText",)


def test_menus_are_pruned_and_recorded():
    menu = Node("AXMenu", children=[BUTTON("Delete Everything")])
    obs, _ = _observe(_app(focused=_window(menu, BUTTON("OK"))))
    assert [t.label for t in obs.targets] == ["OK"] and obs.menu_bar_pruned and obs.excluded["menu"] == 1


def test_no_observe_app_is_refused_without_reading():
    backend = FakeBackend()
    keychain = AppIdentity(bundle_id="com.apple.keychainaccess", pid=77, name="Keychain Access")
    obs, _ = _observe(_app(focused=_window(BUTTON("x"))), backend=backend, app=keychain)
    assert obs.restricted == "NO_OBSERVE_APP" and obs.targets == () and not obs.complete and backend.reads == []


def test_restricted_app_targets_are_flagged():
    terminal = AppIdentity(bundle_id="com.apple.Terminal", pid=88, name="Terminal")
    obs, _ = _observe(_app(focused=_window(BUTTON("x"))), app=terminal)
    assert all(Sensitivity.SENSITIVE_APP in t.sensitivity for t in obs.targets)


def test_evie_window_is_excluded():
    evie = _window(BUTTON("Confirm"), title=EVIE_UI_TITLE)
    other = _window(BUTTON("OK"), title="Docs")
    obs, _ = _observe(_app(focused=evie, windows=[evie, other]))
    assert obs.evie_self is EvieSelfStatus.EXCLUDED and [t.label for t in obs.targets] == ["OK"]
    assert obs.window is None          # the focused window was Evie's: no primary window is claimed
    assert obs.excluded["evie_self_window"] == 1


def test_evie_status_not_excluded_and_unknown():
    obs, _ = _observe(_app(focused=_window(BUTTON("OK"))))
    assert obs.evie_self is EvieSelfStatus.NOT_EXCLUDED
    broken = Node("AXWindow", "AXStandardWindow", children=[BUTTON("OK")], errors={"AXTitle": CANNOT_COMPLETE})
    obs, _ = _observe(_app(focused=broken))
    assert obs.evie_self is EvieSelfStatus.UNKNOWN


def test_evie_process_is_refused():
    obs, _ = _observe(_app(focused=_window(BUTTON("x"))), policy=GatePolicy(evie_pids=frozenset({TEXTEDIT.pid})))
    assert obs.restricted == "EVIE_SELF" and obs.evie_self is EvieSelfStatus.EXCLUDED and obs.targets == ()


# --- targets and counts ---

def test_actionable_vs_non_actionable_and_on_screen():
    off = BUTTON("far", AXPosition=(5000.0, 5000.0))
    text = Node("AXStaticText", AXValue="Hello", AXPosition=(0.0, 0.0), AXSize=(50.0, 10.0))
    group = Node("AXGroup")                                         # unlabelled container: not a target
    obs, _ = _observe(_app(focused=_window(BUTTON("OK"), off, text, group)))
    c = obs.counts
    assert (c.total_nodes, c.actionable_nodes, c.on_screen_nodes) == (5, 2, 2)
    assert [t.label or t.value_summary for t in obs.targets] == ["OK", "far", "Hello"]
    assert obs.targets[2].actions == () and obs.targets[2].value_summary == "Hello"
    assert obs.targets[0].context == ("AXWindow",)


def test_frame_is_metadata_only_and_identifiers_are_never_invented():
    obs, _ = _observe(_app(focused=_window(BUTTON("OK"))))
    t = obs.targets[0]
    assert t.frame.width == 20.0 and t.identifier is None and t.enabled is True and t.focused is None


def test_bounded_targets_prefer_actionable_and_record_truncation():
    labels = [Node("AXStaticText", AXTitle=f"text {i}") for i in range(4)]
    buttons = [BUTTON(f"b{i}") for i in range(3)]
    obs, _ = _observe(_app(focused=_window(*labels, *buttons)), limits=O.ObserverLimits(max_targets=4))
    assert obs.counts.targets == 4 and obs.counts.targets_truncated == 3
    assert [t.label for t in obs.targets] == ["text 0", "b0", "b1", "b2"]   # all actionable kept, tree order


def test_web_area_is_recorded_normally():
    obs, _ = _observe(_app(focused=_window(Node("AXWebArea", AXTitle="page", children=[BUTTON("Link")]))))
    assert obs.web_area_present and "AXWebArea" in [t.role for t in obs.targets]


def test_observation_ids_are_unique_and_serialisation_has_no_raw_objects():
    w = _window(BUTTON("OK"))
    a, _ = _observe(_app(focused=w))
    b, _ = _observe(_app(focused=w))
    assert a.obs_id != b.obs_id and a.fingerprint == b.fingerprint
    text = a.model_dump_json()
    assert "<Node" not in text and "AXUIElement" not in text
    assert type(a).model_validate_json(text) == a
    assert all(t.obs_id == a.obs_id for t in a.targets)


def test_target_window_index_must_exist():
    obs = observation()
    with pytest.raises(ValidationError):
        type(obs)(**{**obs.model_dump(), "targets": [dict(obs.targets[0].model_dump(), window_index=3)]})


def test_gate_uses_the_targets_own_window():
    main = _window(Node("AXGroup", children=[Node("AXCheckBox", subrole="AXSegment", AXDescription="align center",
                                                  actions=["AXPress"])]), title="Main")
    other = _window(Node("AXGroup", children=[Node("AXCheckBox", subrole="AXSegment", AXDescription="align center",
                                                   actions=["AXPress"])]), title="Other")
    obs, _ = _observe(_app(focused=main, main=main, windows=[main, other]))
    in_other = [t for t in obs.targets if t.window_index == 1][0]
    resolved = ResolvedTarget(target=in_other, method=ResolutionMethod.ORDINAL, obs_id=obs.obs_id)
    d = evaluate(context_for(Op.PRESS, NoArgs(), resolved, obs, frozenset({TEXTEDIT.bundle_id})))
    assert d.reason == "NOT_MAIN_WINDOW"


# --- settle ---

@pytest.mark.parametrize("counts, fps, expected", [
    ([5, 5, 5], ["a", "a", "a"], S.SETTLED),
    ([5, 6, 8], ["a", "b", "c"], S.GROWING),
    ([9, 7, 7], ["a", "b", "c"], S.SHRINKING),
    ([5, 6, 5], ["a", "b", "a"], S.OSCILLATING),
    ([5, 5], ["a", "b"], S.OSCILLATING),
    ([5], ["a"], S.UNABLE_TO_DETERMINE),
])
def test_classify_settle(counts, fps, expected):
    assert O.classify_settle(list(zip(fps, counts)), 3) is expected


def test_wait_for_settle_stops_when_stable():
    clock, polls = Clock(), iter([("a", 5), ("b", 6), ("b", 6), ("b", 6)])
    info = O.wait_for_settle(lambda: next(polls), O.DEFAULT_LIMITS, clock, clock.advance)
    assert info.status is S.SETTLED and info.elapsed_ms == 1500


def test_wait_for_settle_is_bounded():
    clock, n = Clock(), iter(range(1000))
    info = O.wait_for_settle(lambda: (f"fp{next(n)}", next(n)), O.DEFAULT_LIMITS, clock, clock.advance)
    assert info.status is S.GROWING and info.elapsed_ms == 20000


def test_unsettled_observation_is_recorded_not_hidden():
    clock = Clock()
    w = _window(BUTTON("OK"))
    grow = iter(range(1000))

    def moving_skeleton(*a, **k):
        return f"fp{next(grow)}", next(grow)

    original = O.skeleton
    O.skeleton = moving_skeleton
    try:
        obs = O.observe_app(FakeBackend(), _app(focused=w), TEXTEDIT, frontmost=TEXTEDIT, settle=True,
                            clock=clock, sleep=clock.advance)
    finally:
        O.skeleton = original
    assert obs.settle.status is S.GROWING and obs.targets                  # observed, but not "settled"


def test_skeleton_fingerprint_changes_with_structure():
    w = _window(BUTTON("OK"))
    windows = O.discover_windows(FakeBackend(), _app(focused=w), 1, O.DEFAULT_LIMITS, Counter(), GatePolicy())
    fp1, n1 = O.skeleton(FakeBackend(), windows, O.DEFAULT_LIMITS, Clock())
    w.children.append(BUTTON("New"))
    fp2, n2 = O.skeleton(FakeBackend(), windows, O.DEFAULT_LIMITS, Clock())
    assert fp1 != fp2 and n2 == n1 + 1


# --- entry point (macos_ax.observe_request) with a fake backend ---

class FakeMacBackend(FakeBackend):
    def __init__(self, apps, trusted=True, app_el=None, ns=None, ax=None, self_reports=None):
        super().__init__()
        self.apps, self.trusted, self.app_el = apps, trusted, app_el or _app(focused=_window(BUTTON("OK")))
        self.ns = ns if ns is not None else (apps[0] if apps else None)
        self.ax_front = ax                                   # None = the AX read fails (as seen live: -25204)
        self.self_reports = self_reports if self_reports is not None else {a.pid for a in apps[:1]}

    def nsworkspace_frontmost(self):
        return (self.ns, None) if self.ns else (None, "NO_READING")

    def ax_focused_application(self):
        return (self.ax_front, None) if self.ax_front else (None, "AX_ERROR:-25204")

    def app_reports_frontmost(self, pid):
        return pid in self.self_reports

    def is_trusted(self):
        return self.trusted

    def app_by_bundle(self, bundle_id):
        return next((a for a in self.apps if a.bundle_id == bundle_id), None)

    def app_by_pid(self, pid):
        return next((a for a in self.apps if a.pid == pid), None)

    def running_apps(self):
        return list(self.apps)

    def app_element(self, pid):
        return self.app_el

    def screens(self):
        return [(0.0, 0.0, 1440.0, 900.0)]


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("EVIE_LIVE_MACOS", "1")


def test_observe_request_is_blocked_under_pytest():
    r = macos_ax.observe_request(bundle_id="com.apple.TextEdit", backend=FakeMacBackend([TEXTEDIT]))
    assert r.status == "UNAVAILABLE" and r.reason == "BLOCKED_UNDER_TEST"


def test_observe_request_ok(live_env):
    r = macos_ax.observe_request(bundle_id="com.apple.TextEdit", backend=FakeMacBackend([TEXTEDIT]), settle=False)
    assert r.status == "OK" and r.observation.app == TEXTEDIT and r.observation.targets[0].label == "OK"


def test_observe_request_never_launches(live_env):
    r = macos_ax.observe_request(bundle_id="com.apple.Safari", backend=FakeMacBackend([TEXTEDIT]))
    assert r.status == "UNAVAILABLE" and "APP_NOT_RUNNING" in r.reason


def test_observe_request_untrusted_and_scope(live_env):
    assert macos_ax.observe_request(pid=TEXTEDIT.pid, backend=FakeMacBackend([TEXTEDIT], trusted=False)).status == \
        "NOT_READY"
    assert macos_ax.observe_request(bundle_id="a.b", pid=3, backend=FakeMacBackend([TEXTEDIT])).status == "ERROR"


def test_observe_request_restricted(live_env):
    keychain = AppIdentity(bundle_id="com.apple.keychainaccess", pid=77, name="Keychain Access")
    r = macos_ax.observe_request(bundle_id=keychain.bundle_id, backend=FakeMacBackend([keychain]))
    assert r.status == "RESTRICTED" and r.observation.targets == ()


def test_frontmost_is_cross_checked_never_substituted(live_env):
    from cc_fixtures import SAFARI
    # NSWorkspace says Safari, Safari reports itself frontmost, AX focused-app read fails: agreed on Safari.
    r = macos_ax.observe_request(bundle_id="com.apple.TextEdit", settle=False,
                                 backend=FakeMacBackend([TEXTEDIT, SAFARI], ns=SAFARI, self_reports={SAFARI.pid}))
    assert r.observation.frontmost == SAFARI and r.observation.app == TEXTEDIT
    assert set(r.observation.frontmost_sources) == {"nsworkspace", "app_ax_frontmost"}
    # Only one source: unknown - and the observed app is NOT substituted (the Step 3 bug).
    r = macos_ax.observe_request(bundle_id="com.apple.TextEdit", settle=False,
                                 backend=FakeMacBackend([TEXTEDIT, SAFARI], ns=SAFARI, self_reports=set()))
    assert r.observation.frontmost is None and "INSUFFICIENT_SOURCES" in r.observation.frontmost_diagnostic
    # Disagreement: unknown.
    r = macos_ax.observe_request(bundle_id="com.apple.TextEdit", settle=False,
                                 backend=FakeMacBackend([TEXTEDIT, SAFARI], ns=SAFARI, ax=TEXTEDIT,
                                                        self_reports={SAFARI.pid, TEXTEDIT.pid}))
    assert r.observation.frontmost is None and r.observation.frontmost_diagnostic.startswith("DISAGREE")


def test_observe_frontmost_scope_requires_agreement(live_env):
    r = macos_ax.observe_request(frontmost=True, backend=FakeMacBackend([TEXTEDIT], self_reports=set()))
    assert r.status == "UNAVAILABLE" and "FRONTMOST_UNRESOLVED" in r.reason


def test_read_identity_matches_the_walk():
    w = _window(BUTTON("OK", AXIdentifier="ok-1"))
    obs, handles = O.observe_app_with_handles(FakeBackend(), _app(focused=w), TEXTEDIT, settle=False)
    err, live = O.read_identity(FakeBackend(), handles.targets[0])
    t = obs.targets[0]
    assert err == 0 and live == {"role": t.role, "subrole": t.subrole, "label": t.label,
                                 "identifier": t.identifier, "enabled": t.enabled}
    assert len(handles.targets) == len(obs.targets) and len(handles.windows) == len(obs.windows)
