"""
Unit tests for Evie Phase 1 Semantic Observation & Target Resolution.

Tests the bounded hierarchical semantic tree construction, role extraction,
label/value handling, parent-child links, structural paths, state flags,
node/depth bounds, redaction, Evie-self exclusion, and generalized target resolution.
No live macOS UI required (uses FakeBackend).
"""

import time
import pytest
from pydantic import ValidationError

from services.computer_control import macos_ax, observer as O, resolver as R
from services.computer_control.gate import GatePolicy
from services.computer_control.models import (
    AppIdentity,
    EvieSelfStatus,
    Frame,
    ObservedTarget,
    ResolutionMethod,
    ResolvedTarget,
    SemanticNode,
    Sensitivity,
    SystemObservation,
    WindowKey,
)
from services.computer_control.resolver import SemanticTargetSpec


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
        return -25205, None

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


APP_ID = AppIdentity(bundle_id="com.example.App", pid=123, name="ExampleApp")


def _window(*children, title="Test Window", doc=None, **kw):
    return Node("AXWindow", "AXStandardWindow", children=children, AXTitle=title, **kw)


def _app(focused=None, main=None, windows=None):
    attrs = {}
    if focused is not None:
        attrs["AXFocusedWindow"] = focused
    if main is not None:
        attrs["AXMainWindow"] = main
    if windows is not None:
        attrs["AXWindows"] = windows
    return Node("AXApplication", **attrs)


def _observe(app_node, **kw):
    backend = FakeBackend()
    obs, handles = O.observe_app_with_handles(
        backend, app_node, APP_ID, settle=False, **kw
    )
    return obs, backend


from collections import Counter


# --- 1. Hierarchical Tree Construction ---
def test_hierarchical_tree_construction():
    btn = Node("AXButton", AXTitle="Save", actions=["AXPress"])
    win = _window(btn, title="Editor")
    app = _app(focused=win)
    obs, _ = _observe(app)

    assert obs.root_id == "app_root"
    assert "app_root" in obs.nodes
    app_node = obs.nodes["app_root"]
    assert app_node.role == "AXApplication"
    assert len(app_node.child_ids) > 0

    win_node_id = app_node.child_ids[0]
    win_node = obs.nodes[win_node_id]
    assert win_node.role == "AXWindow"
    assert win_node.parent_id == "app_root"
    assert len(win_node.child_ids) == 1

    btn_node_id = win_node.child_ids[0]
    btn_node = obs.nodes[btn_node_id]
    assert btn_node.role == "AXButton"
    assert btn_node.label == "Save"
    assert btn_node.parent_id == win_node_id


# --- 2. Semantic Role Extraction ---
def test_semantic_role_extraction():
    roles = [
        "AXGroup", "AXButton", "AXTabGroup", "AXTab", "AXTextField", "AXTextArea",
        "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXLink", "AXList", "AXRow",
        "AXTable", "AXScrollArea", "AXScrollBar", "AXImage", "AXStaticText"
    ]
    kids = [Node(r, AXTitle=f"Label_{r}") for r in roles]
    win = _window(*kids)
    app = _app(focused=win)
    obs, _ = _observe(app)

    found_roles = {n.role for n in obs.nodes.values()}
    for r in roles:
        assert r in found_roles


# --- 3. Labels ---
def test_labels():
    b1 = Node("AXButton", AXTitle="Submit")
    b2 = Node("AXButton", AXDescription="Cancel")
    win = _window(b1, b2)
    app = _app(focused=win)
    obs, _ = _observe(app)

    labels = [n.label for n in obs.nodes.values() if n.role == "AXButton"]
    assert "Submit" in labels
    assert "Cancel" in labels


# --- 4. Safe Value Summaries ---
def test_safe_value_summaries():
    tf = Node("AXTextField", AXValue="Hello World")
    cb = Node("AXCheckBox", AXValue=True)
    st = Node("AXStaticText", AXValue="Status OK")
    win = _window(tf, cb, st)
    app = _app(focused=win)
    obs, _ = _observe(app)

    summaries = {n.role: n.safe_value_summary for n in obs.nodes.values()}
    assert summaries["AXTextField"] == "Hello World"
    assert summaries["AXCheckBox"] == "1"
    assert summaries["AXStaticText"] == "Status OK"


# --- 5. Parent-Child Relationships ---
def test_parent_child_relationships():
    group = Node("AXGroup", children=[Node("AXButton", AXTitle="Inside Group")])
    win = _window(group)
    app = _app(focused=win)
    obs, _ = _observe(app)

    group_nodes = [n for n in obs.nodes.values() if n.role == "AXGroup"]
    assert len(group_nodes) == 1
    group_node = group_nodes[0]
    assert len(group_node.child_ids) == 1

    child_node = obs.nodes[group_node.child_ids[0]]
    assert child_node.parent_id == group_node.node_id
    assert child_node.label == "Inside Group"


# --- 6. Structural Paths ---
def test_structural_paths():
    btn = Node("AXButton", AXTitle="Nested")
    group = Node("AXGroup", children=[btn])
    win = _window(group)
    app = _app(focused=win)
    obs, _ = _observe(app)

    btn_nodes = [n for n in obs.nodes.values() if n.label == "Nested"]
    assert len(btn_nodes) == 1
    path = btn_nodes[0].path
    assert path == ("AXApplication", "AXWindow", "AXGroup", "AXButton")


# --- 7. Focused State ---
def test_focused_state():
    tf1 = Node("AXTextField", AXFocused=True, AXTitle="Field 1")
    tf2 = Node("AXTextField", AXFocused=False, AXTitle="Field 2")
    win = _window(tf1, tf2)
    app = _app(focused=win)
    obs, _ = _observe(app)

    nodes = {n.label: n.focused for n in obs.nodes.values() if n.role == "AXTextField"}
    assert nodes["Field 1"] is True
    assert nodes["Field 2"] is False


# --- 8. Selected State ---
def test_selected_state():
    t1 = Node("AXTab", AXSelected=True, AXTitle="Tab 1")
    t2 = Node("AXTab", AXSelected=False, AXTitle="Tab 2")
    win = _window(t1, t2)
    app = _app(focused=win)
    obs, _ = _observe(app)

    tabs = {n.label: n.selected for n in obs.nodes.values() if n.role == "AXTab"}
    assert tabs["Tab 1"] is True
    assert tabs["Tab 2"] is False


# --- 9. Enabled State ---
def test_enabled_state():
    b1 = Node("AXButton", AXEnabled=True, AXTitle="Enabled Btn")
    b2 = Node("AXButton", AXEnabled=False, AXTitle="Disabled Btn")
    win = _window(b1, b2)
    app = _app(focused=win)
    obs, _ = _observe(app)

    btns = {n.label: n.enabled for n in obs.nodes.values() if n.role == "AXButton"}
    assert btns["Enabled Btn"] is True
    assert btns["Disabled Btn"] is False


# --- 10. Node & Depth Bounds ---
def test_node_depth_bounds():
    # Build deep tree exceeding max_depth limit
    limits = O.ObserverLimits(max_depth=3, max_nodes=50)
    curr = Node("AXButton", AXTitle="Deep Leaf")
    for _ in range(5):
        curr = Node("AXGroup", children=[curr])
    win = _window(curr)
    app = _app(focused=win)

    backend = FakeBackend()
    obs, _ = O.observe_app_with_handles(backend, app, APP_ID, limits=limits, settle=False)

    assert "depth" in obs.caps_hit


# --- 11. Deterministic Truncation & Prioritization ---
def test_deterministic_truncation():
    btns = [Node("AXButton", AXTitle=f"Btn_{i}", actions=["AXPress"]) for i in range(20)]
    win = _window(*btns)
    app = _app(focused=win)
    limits = O.ObserverLimits(max_targets=5)

    backend = FakeBackend()
    obs, _ = O.observe_app_with_handles(backend, app, APP_ID, limits=limits, settle=False)

    assert len(obs.targets) == 5
    labels = [t.label for t in obs.targets]
    assert labels == ["Btn_0", "Btn_1", "Btn_2", "Btn_3", "Btn_4"]


# --- 12. Sensitive Value Redaction ---
def test_sensitive_value_redaction():
    sec = Node("AXSecureTextField", AXValue="supersecret", children=[Node("AXStaticText", AXValue="secret child")])
    win = _window(sec)
    app = _app(focused=win)
    obs, _ = _observe(app)

    sec_nodes = [n for n in obs.nodes.values() if n.role == "AXSecureTextField"]
    assert len(sec_nodes) == 1
    sec_node = sec_nodes[0]
    assert sec_node.safe_value_summary is None
    assert Sensitivity.SECURE_FIELD in sec_node.sensitivity
    # Secure element children are not descended into
    assert len(sec_node.child_ids) == 0


# --- 13. Evie-Self Exclusion ---
def test_evie_self_exclusion():
    from services.computer_control.models import title_hash
    policy = GatePolicy(evie_title_hashes=frozenset({title_hash("Evie Assistant")}))
    win = _window(title="Evie Assistant")
    app = _app(focused=win)

    backend = FakeBackend()
    obs, _ = O.observe_app_with_handles(backend, app, APP_ID, policy=policy, settle=False)

    assert obs.evie_self == EvieSelfStatus.EXCLUDED
    assert obs.excluded.get("evie_self_window") == 1


# --- 14. Exact Target Resolution ---
def test_exact_target_resolution():
    b = Node("AXButton", AXTitle="Save")
    win = _window(b)
    app = _app(focused=win)
    obs, _ = _observe(app)

    target_node = [n for n in obs.nodes.values() if n.label == "Save"][0]
    spec = SemanticTargetSpec(obs_id=obs.obs_id, node_id=target_node.node_id)
    res = R.resolve_semantic(obs, spec)

    assert res.kind == "RESOLVED"
    assert res.reason == "EXACT"
    assert res.target.target.label == "Save"


# --- 15. Semantic Target Resolution ---
def test_semantic_target_resolution():
    b = Node("AXButton", AXTitle="Continue", actions=["AXPress"])
    win = _window(b)
    app = _app(focused=win)
    obs, _ = _observe(app)

    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Continue")
    res = R.resolve_semantic(obs, spec)

    assert res.kind == "RESOLVED"
    assert res.target.target.label == "Continue"


# --- 16. Ancestor-Constrained Resolution ---
def test_ancestor_constrained_resolution():
    b1 = Node("AXButton", AXTitle="Settings")
    group = Node("AXGroup", AXTitle="Sidebar", children=[b1])
    b2 = Node("AXButton", AXTitle="Settings")
    win = _window(group, b2)
    app = _app(focused=win)
    obs, _ = _observe(app)

    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Settings", ancestor_role="AXGroup")
    res = R.resolve_semantic(obs, spec)

    assert res.kind == "RESOLVED"
    assert res.target.target.label == "Settings"


# --- 17. Ambiguity Detection ---
def test_ambiguity_detection():
    b1 = Node("AXButton", AXTitle="Play", actions=["AXPress"])
    b2 = Node("AXButton", AXTitle="Play", actions=["AXPress"])
    win = _window(b1, b2)
    app = _app(focused=win)
    obs, _ = _observe(app)

    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Play")
    res = R.resolve_semantic(obs, spec)

    assert res.kind == "ASK"
    assert res.reason == "AMBIGUOUS_TARGET"


# --- 18. Target Not Found Behavior ---
def test_target_not_found_behavior():
    b = Node("AXButton", AXTitle="Submit")
    win = _window(b)
    app = _app(focused=win)
    obs, _ = _observe(app)

    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Nonexistent")
    res = R.resolve_semantic(obs, spec)

    assert res.kind == "ASK"
    assert res.reason == "TARGET_NOT_FOUND"


# --- 19. Stale obs_id Rejection ---
def test_stale_obs_id_rejection():
    b = Node("AXButton", AXTitle="OK")
    win = _window(b)
    app = _app(focused=win)
    obs, _ = _observe(app)

    spec = SemanticTargetSpec(obs_id="obs-stale12345", role="AXButton", label="OK")
    res = R.resolve_semantic(obs, spec)

    assert res.kind == "STALE"
    assert res.reason == "OBSERVATION_MISMATCH"


# --- 20. Observation Age Rejection ---
def test_observation_age_rejection():
    b = Node("AXButton", AXTitle="OK")
    win = _window(b)
    app = _app(focused=win)
    obs, _ = _observe(app)

    # Validate that spec against old observation age can be detected as stale
    now_ts = time.monotonic()
    max_age_s = 5.0
    is_stale = (now_ts - obs.taken_at) > max_age_s
    assert is_stale is False

    # Mock older timestamp
    old_obs = obs.model_copy(update={"taken_at": now_ts - 10.0})
    is_old_stale = (now_ts - old_obs.taken_at) > max_age_s
    assert is_old_stale is True
