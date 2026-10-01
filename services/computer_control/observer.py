"""
Read-only, bounded Accessibility observer (docs/07_Computer_Control.md Section 7) - pure logic.

Everything here works through the AXBackend protocol, so it is platform-independent and fully
testable with fake nodes. The real backend (macos_ax.py) exists only inside the worker process.
The protocol exposes READS only (attribute values, bounded child slices, attribute/action names,
settability) - there is no way to perform an action through it.

Evidence-based rules (EVIDENCE.md Section 4):
- roots are AXFocusedWindow, AXMainWindow and AXWindows (de-duplicated), never the app root alone;
- depth 60, 3,000 nodes, 1 s messaging timeout, a walk time budget, bounded child slices;
- any cap hit is recorded and makes the observation incomplete (the session then refuses to act);
- menus are pruned by default (recorded), secure fields are never read or descended into,
  Evie's own windows are excluded, no-observation apps are refused outright;
- settle = a role + child-count skeleton identical for 3 polls ~0.5 s apart, max ~20 s.
"""

import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Sequence, Tuple

from pydantic import Field

from services.computer_control.frontmost import FrontmostCheck
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy
from services.computer_control.models import (
    SECURE_ROLES,
    AppIdentity,
    AXErrorRecord,
    EvieSelfStatus,
    Frame,
    Observation,
    ObservationCounts,
    ObservedTarget,
    Sensitivity,
    SettleInfo,
    SettleStatus,
    WindowKey,
    _Strict,
    title_hash,
)

# AXError codes that matter here
AX_SUCCESS = 0
AX_NO_VALUE = -25212                 # attribute exists but has no value: normal, not an error
AX_ATTRIBUTE_UNSUPPORTED = -25205

ACTION_NAMES = frozenset({"AXPress", "AXPick", "AXConfirm", "AXIncrement", "AXDecrement", "AXOpen"})
TEXT_ROLES = frozenset({"AXTextArea", "AXTextField", "AXComboBox"})
TEXT_SETTABLE_ATTRS = ("AXValue", "AXSelectedText", "AXSelectedTextRange")
# Short values are summarised only for these roles. Document-sized text (AXTextArea) is never summarised.
VALUE_SUMMARY_ROLES = frozenset({"AXCheckBox", "AXRadioButton", "AXStaticText", "AXTextField", "AXPopUpButton",
                                 "AXSlider", "AXValueIndicator", "AXIncrementor", "AXComboBox", "AXDisclosureTriangle"})
MENU_ROLES = frozenset({"AXMenuBar", "AXMenu"})
USEFUL_ROLES = frozenset({"AXTextArea", "AXTextField", "AXScrollArea", "AXWebArea", "AXComboBox"})


def bounded_semantic_graph(nodes: Dict[str, Any], root_id: Optional[str], limit: int) -> Tuple[Dict[str, Any], bool]:
    """
    The semantic graph without structure-only nodes: nodes that carry meaning (actions, settable text, a label, a
    value, a useful role, a secure flag, focus or selection) plus every ancestor that connects them to the root,
    in walk order, at most `limit`. child_ids are rewritten to the kept nodes. Returns (graph, truncated).
    """
    def meaningful(n) -> bool:
        return bool(n.actions or n.settable or n.label or n.safe_value_summary or n.role in USEFUL_ROLES
                    or n.sensitivity or n.focused or n.selected or n.role == "AXWindow")

    keep: List[str] = []
    kept = set()
    truncated = False
    for node_id, node in nodes.items():
        if node_id == root_id or not meaningful(node):
            continue
        chain, cur = [], node
        while cur is not None and cur.node_id not in kept and cur.node_id != root_id:
            chain.append(cur.node_id)
            cur = nodes.get(cur.parent_id) if cur.parent_id else None
        if len(keep) + len(chain) > limit:
            truncated = True
            break
        for nid in reversed(chain):
            kept.add(nid)
            keep.append(nid)
    if root_id in nodes:
        kept.add(root_id)
        keep.insert(0, root_id)
    graph = {nid: nodes[nid].model_copy(update={"child_ids": tuple(c for c in nodes[nid].child_ids if c in kept)})
             for nid in keep}
    return graph, truncated


@dataclass(frozen=True)
class ObserverLimits:
    max_depth: int = 60
    max_nodes: int = 3000
    max_children: int = 500            # bounded AXChildren slice per element
    max_windows: int = 20
    messaging_timeout_s: float = 1.0   # applied by the backend to every element it hands out
    time_budget_s: float = 15.0        # one full walk
    max_targets: int = 150             # bounded list for resolution / a future chooser
    max_semantic_nodes: int = 300      # the semantic graph that crosses the worker boundary (bounded message)
    settle_poll_s: float = 0.5
    settle_stable_polls: int = 3
    settle_budget_s: float = 20.0
    label_chars: int = 200
    value_chars: int = 120


DEFAULT_LIMITS = ObserverLimits()


class AXBackend(Protocol):
    """Read-only accessibility access. Elements are opaque handles that never leave the worker."""
    def copy(self, element: Any, attribute: str) -> Tuple[int, Any]: ...
    def children(self, element: Any, limit: int) -> Tuple[int, List[Any], int]: ...   # (err, slice, total)
    def attribute_names(self, element: Any) -> Tuple[int, List[str]]: ...
    def action_names(self, element: Any) -> Tuple[int, List[str]]: ...
    def is_settable(self, element: Any, attribute: str) -> Tuple[int, bool]: ...
    def key(self, element: Any) -> Any: ...                 # hashable bucket key (e.g. CFHash)
    def same(self, a: Any, b: Any) -> bool: ...             # identity (e.g. CFEqual)


class ObserveResult(_Strict):
    status: Literal["OK", "UNAVAILABLE", "RESTRICTED", "NOT_READY", "ERROR"]
    observation: Optional[Observation] = None
    reason: Optional[str] = Field(None, max_length=200)
    duration_ms: Optional[float] = Field(None, ge=0)


# --- helpers ---

def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _text(value: Any, limit: int) -> Optional[str]:
    if value is None or isinstance(value, (list, tuple, dict)):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    return text[:limit] if text else None


def _intersects(frame: Optional[Frame], screens: Sequence[Tuple[float, float, float, float]]) -> bool:
    if frame is None or frame.width <= 0 or frame.height <= 0:
        return False
    for sx, sy, sw, sh in screens:
        if frame.x < sx + sw and frame.x + frame.width > sx and frame.y < sy + sh and frame.y + frame.height > sy:
            return True
    return False


class _Seen:
    """Cycle/duplicate protection by backend identity (CFEqual), bucketed by a hash key."""

    def __init__(self, backend: AXBackend):
        self.backend = backend
        self.buckets: Dict[Any, List[Any]] = {}

    def add(self, element: Any) -> bool:
        bucket = self.buckets.setdefault(self.backend.key(element), [])
        if any(self.backend.same(element, other) for other in bucket):
            return False
        bucket.append(element)
        return True


@dataclass
class _Walk:
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    semantic_nodes: Dict[str, Any] = field(default_factory=dict)
    root_id: Optional[str] = None
    errors: Counter = field(default_factory=Counter)
    caps: set = field(default_factory=set)
    total: int = 0
    actionable: int = 0
    on_screen: int = 0
    secure: int = 0
    excluded: Counter = field(default_factory=Counter)
    menu_pruned: bool = False
    web: bool = False


class _Reader:
    def __init__(self, backend: AXBackend, errors: Counter):
        self.backend = backend
        self.errors = errors

    def read(self, element: Any, attribute: str, supported: Optional[set], role: Optional[str]) -> Any:
        if supported is not None and attribute not in supported:
            return None
        err, value = self.backend.copy(element, attribute)
        if err == AX_SUCCESS:
            return value
        if err not in (AX_NO_VALUE, AX_ATTRIBUTE_UNSUPPORTED):
            self.errors[(attribute, role, err)] += 1
        return None


# --- roots and windows ---

@dataclass
class _Window:
    element: Any
    key: WindowKey
    evie: Optional[bool]          # True = Evie, False = checked not Evie, None = could not check


def discover_windows(backend: AXBackend, app_element: Any, pid: int, limits: ObserverLimits, errors: Counter,
                     policy: GatePolicy) -> List[_Window]:
    """Roots from AXFocusedWindow, AXMainWindow and AXWindows, de-duplicated, focused first."""
    reader = _Reader(backend, errors)
    focused = reader.read(app_element, "AXFocusedWindow", None, "AXApplication")
    main = reader.read(app_element, "AXMainWindow", None, "AXApplication")
    listed = reader.read(app_element, "AXWindows", None, "AXApplication") or []
    seen = _Seen(backend)
    ordered = []
    for element in [focused, main, *list(listed)[:limits.max_windows]]:
        if element is not None and seen.add(element):
            ordered.append(element)
    windows = []
    for element in ordered[:limits.max_windows]:
        err_n, names = backend.attribute_names(element)
        supported = set(names) if err_n == AX_SUCCESS else None
        role = _text(reader.read(element, "AXRole", supported, None), 64)
        subrole = _text(reader.read(element, "AXSubrole", supported, role), 64)
        title_err, title = backend.copy(element, "AXTitle") if supported is None or "AXTitle" in supported \
            else (AX_ATTRIBUTE_UNSUPPORTED, None)
        document = reader.read(element, "AXDocument", supported, role)
        title_text = _text(title, 10_000) if title_err == AX_SUCCESS else None
        t_hash = title_hash(title_text) if title_text else None
        if title_err not in (AX_SUCCESS, AX_NO_VALUE, AX_ATTRIBUTE_UNSUPPORTED):
            errors[("AXTitle", role, title_err)] += 1
            evie: Optional[bool] = None
        else:
            evie = t_hash is not None and t_hash in policy.evie_title_hashes
        key = WindowKey(pid=pid, document_hash=_short_hash(str(document)) if document else None, title_hash=t_hash,
                        role=role or "unknown", subrole=subrole,
                        is_main=main is not None and backend.same(element, main),
                        is_focused=focused is not None and backend.same(element, focused))
        windows.append(_Window(element=element, key=key, evie=evie))
    return windows


# --- full walk ---

def walk(backend: AXBackend, windows: Sequence[_Window], limits: ObserverLimits,
         screens: Sequence[Tuple[float, float, float, float]], clock: Callable[[], float],
         sensitive_app: bool = False, app_identity: Optional[AppIdentity] = None) -> _Walk:
    from services.computer_control.models import SemanticNode
    result = _Walk()
    reader = _Reader(backend, result.errors)
    seen = _Seen(backend)
    started = clock()

    root_id = "app_root"
    result.root_id = root_id
    app_flags = set()
    if sensitive_app:
        app_flags.add(Sensitivity.SENSITIVE_APP)
    app_node = SemanticNode(
        node_id=root_id,
        role="AXApplication",
        subrole=None,
        label=app_identity.name if app_identity else None,
        safe_value_summary=None,
        identifier=app_identity.bundle_id if app_identity else None,
        enabled=True,
        focused=True,
        selected=None,
        path=("AXApplication",),
        parent_id=None,
        child_ids=(),
        app_identity=app_identity,
        window_identity=None,
        actions=(),
        settable=(),
        frame=None,
        sensitivity=frozenset(app_flags),
        window_index=None
    )
    result.semantic_nodes[root_id] = app_node

    for window_index, window in enumerate(windows):
        if window.evie:
            result.excluded["evie_self_window"] += 1
            continue
        stack = [(window.element, 0, (), root_id)]
        while stack:
            if clock() - started > limits.time_budget_s:
                result.caps.add("time")
                return result
            if result.total >= limits.max_nodes:
                result.caps.add("nodes")
                return result
            element, depth, ancestors, parent_node_id = stack.pop()
            if not seen.add(element):
                continue                                        # cycle / duplicate
            err_n, names = backend.attribute_names(element)
            if err_n != AX_SUCCESS:
                result.errors[("AttributeNames", None, err_n)] += 1
                continue
            supported = set(names)
            role = _text(reader.read(element, "AXRole", supported, None), 64)
            subrole = _text(reader.read(element, "AXSubrole", supported, role), 64)
            result.total += 1
            if role in MENU_ROLES:
                result.menu_pruned = True
                result.excluded["menu"] += 1
                continue
            secure = role in SECURE_ROLES or subrole in SECURE_ROLES
            label = _text(reader.read(element, "AXTitle", supported, role), limits.label_chars) or \
                _text(reader.read(element, "AXDescription", supported, role), limits.label_chars)
            if depth == 0 or role == "AXWindow":
                label = None
            identifier = _text(reader.read(element, "AXIdentifier", supported, role), 200)
            enabled = reader.read(element, "AXEnabled", supported, role)
            focused = reader.read(element, "AXFocused", supported, role)
            selected_raw = reader.read(element, "AXSelected", supported, role)
            selected = selected_raw if isinstance(selected_raw, bool) else None
            position = reader.read(element, "AXPosition", supported, role)
            size = reader.read(element, "AXSize", supported, role)
            frame = Frame(x=position[0], y=position[1], width=max(0.0, size[0]), height=max(0.0, size[1])) \
                if isinstance(position, tuple) and isinstance(size, tuple) else None
            value = None
            if not secure and role in VALUE_SUMMARY_ROLES:
                value = _text(reader.read(element, "AXValue", supported, role), limits.value_chars)
            err_a, action_list = backend.action_names(element)
            if err_a != AX_SUCCESS:
                result.errors[("ActionNames", role, err_a)] += 1
                action_list = []
            actions = tuple(str(a)[:64] for a in action_list)[:20]
            settable: Tuple[str, ...] = ()
            if not secure and role in TEXT_ROLES:
                ok = []
                for attr in TEXT_SETTABLE_ATTRS:
                    if attr in supported:
                        err_s, flag = backend.is_settable(element, attr)
                        if err_s == AX_SUCCESS and flag:
                            ok.append(attr)
                        elif err_s != AX_SUCCESS:
                            result.errors[("IsSettable:" + attr, role, err_s)] += 1
                settable = tuple(ok)
            actionable = bool(ACTION_NAMES.intersection(actions)) or bool(settable)
            result.actionable += actionable
            on_screen = _intersects(frame, screens)
            result.on_screen += on_screen
            if role == "AXWebArea":
                result.web = True
            flags = set()
            if secure:
                flags.add(Sensitivity.SECURE_FIELD)
                result.secure += 1
                result.excluded["secure"] += 1
            if sensitive_app:
                flags.add(Sensitivity.SENSITIVE_APP)

            node_id = f"node_{len(result.semantic_nodes)}"
            struct_path = ("AXApplication",) + tuple(ancestors) + (role or "unknown",)
            sem_node = SemanticNode(
                node_id=node_id,
                role=role or "unknown",
                subrole=subrole,
                label=label,
                safe_value_summary=value if not secure else None,
                identifier=identifier,
                enabled=enabled if isinstance(enabled, bool) else None,
                focused=focused if isinstance(focused, bool) else None,
                selected=selected,
                path=struct_path,
                parent_id=parent_node_id,
                child_ids=(),
                app_identity=app_identity,
                window_identity=window.key,
                actions=actions,
                settable=settable,
                frame=frame,
                sensitivity=frozenset(flags),
                window_index=window_index,
            )
            result.semantic_nodes[node_id] = sem_node
            if parent_node_id in result.semantic_nodes:
                parent_n = result.semantic_nodes[parent_node_id]
                result.semantic_nodes[parent_node_id] = parent_n.model_copy(
                    update={"child_ids": parent_n.child_ids + (node_id,)}
                )

            result.nodes.append({
                "role": role or "unknown", "subrole": subrole, "label": label, "identifier": identifier,
                "enabled": enabled if isinstance(enabled, bool) else None,
                "focused": focused if isinstance(focused, bool) else None,
                "value_summary": value, "actions": actions, "settable": settable, "frame": frame,
                "sensitivity": frozenset(flags), "window_index": window_index, "context": tuple(ancestors)[-20:],
                "actionable": actionable, "order": len(result.nodes), "root": depth == 0,
                "element": element,                             # in-process handle; never serialised
            })
            if secure:
                continue                                        # never descend into secure fields
            if "AXChildren" not in supported:
                continue
            err_c, kids, total = backend.children(element, limits.max_children)
            if err_c not in (AX_SUCCESS, AX_NO_VALUE):
                result.errors[("AXChildren", role, err_c)] += 1
                continue
            if total > len(kids):
                result.caps.add("children")
            if kids and depth + 1 > limits.max_depth:
                result.caps.add("depth")
                continue
            child_ancestors = (*ancestors, role or "unknown")
            for kid in reversed(kids):                          # pre-order, left to right
                stack.append((kid, depth + 1, child_ancestors, node_id))
    return result


# --- live re-validation (same semantics as walk()) ---

def read_identity(backend: AXBackend, element: Any, limits: ObserverLimits = DEFAULT_LIMITS) -> Tuple[int, Dict[str, Any]]:
    """Re-read the identity fields an ObservedTarget was built from. (err, fields); err != 0 = unreadable."""
    err, names = backend.attribute_names(element)
    if err != AX_SUCCESS:
        return err, {}
    supported = set(names)
    reader = _Reader(backend, Counter())
    role = _text(reader.read(element, "AXRole", supported, None), 64)
    subrole = _text(reader.read(element, "AXSubrole", supported, role), 64)
    label = _text(reader.read(element, "AXTitle", supported, role), limits.label_chars) or \
        _text(reader.read(element, "AXDescription", supported, role), limits.label_chars)
    identifier = _text(reader.read(element, "AXIdentifier", supported, role), 200)
    enabled = reader.read(element, "AXEnabled", supported, role)
    return AX_SUCCESS, {"role": role or "unknown", "subrole": subrole, "label": label, "identifier": identifier,
                        "enabled": enabled if isinstance(enabled, bool) else None}


# --- targets ---

def _useful(node: Dict[str, Any]) -> bool:
    # Window roots are represented by Observation.windows (title hashed), never as labelled targets.
    if node["root"]:
        return False
    return node["actionable"] or node["label"] is not None or node["value_summary"] is not None \
        or node["role"] in USEFUL_ROLES or Sensitivity.SECURE_FIELD in node["sensitivity"]


def select_targets(nodes: Sequence[Dict[str, Any]], limit: int) -> Tuple[List[Dict[str, Any]], int]:
    """Bounded, deterministic: actionable targets first (tree order), then other useful ones; no ranking."""
    useful = [n for n in nodes if _useful(n)]
    if len(useful) <= limit:
        return useful, 0
    actionable = [n for n in useful if n["actionable"]]
    rest = [n for n in useful if not n["actionable"]]
    chosen = (actionable + rest)[:limit]
    chosen.sort(key=lambda n: n["order"])
    return chosen, len(useful) - len(chosen)


# --- settle ---

def skeleton(backend: AXBackend, windows: Sequence[_Window], limits: ObserverLimits,
             clock: Callable[[], float]) -> Tuple[str, int]:
    """Role + child-count fingerprint of the window subtrees (menus pruned, secure fields not descended)."""
    parts: List[str] = []
    seen = _Seen(backend)
    started = clock()
    for window in windows:
        if window.evie:
            continue
        stack = [(window.element, 0)]
        while stack and len(parts) < limits.max_nodes and clock() - started <= limits.time_budget_s:
            element, depth = stack.pop()
            if not seen.add(element):
                continue
            err, role = backend.copy(element, "AXRole")
            role = _text(role, 64) if err == AX_SUCCESS else None
            if role in MENU_ROLES or role in SECURE_ROLES:
                parts.append(f"{depth}:{role}:-")
                continue
            err_c, kids, total = backend.children(element, limits.max_children)
            kids = kids if err_c == AX_SUCCESS else []
            parts.append(f"{depth}:{role}:{total if err_c == AX_SUCCESS else 'E'}")
            if depth < limits.max_depth:
                stack.extend((k, depth + 1) for k in reversed(kids))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32], len(parts)


def classify_settle(history: Sequence[Tuple[str, int]], stable_polls: int) -> SettleStatus:
    if len(history) >= stable_polls and len({fp for fp, _ in history[-stable_polls:]}) == 1:
        return SettleStatus.SETTLED
    counts = [n for _, n in history]
    if len(counts) < 2:
        return SettleStatus.UNABLE_TO_DETERMINE
    diffs = [b - a for a, b in zip(counts, counts[1:])]
    if all(d >= 0 for d in diffs) and any(d > 0 for d in diffs):
        return SettleStatus.GROWING
    if all(d <= 0 for d in diffs) and any(d < 0 for d in diffs):
        return SettleStatus.SHRINKING
    if any(d != 0 for d in diffs) or len({fp for fp, _ in history}) > 1:
        return SettleStatus.OSCILLATING
    return SettleStatus.UNABLE_TO_DETERMINE


def wait_for_settle(poll: Callable[[], Tuple[str, int]], limits: ObserverLimits, clock: Callable[[], float],
                    sleep: Callable[[float], None]) -> SettleInfo:
    """Poll the skeleton until it is identical for `settle_stable_polls` polls, or the budget runs out."""
    started = clock()
    history: List[Tuple[str, int]] = []
    while True:
        history.append(poll())
        status = classify_settle(history, limits.settle_stable_polls)
        elapsed = clock() - started
        if status is SettleStatus.SETTLED or elapsed >= limits.settle_budget_s:
            final = status if status is SettleStatus.SETTLED else classify_settle(history, limits.settle_stable_polls)
            return SettleInfo(status=final, elapsed_ms=int(elapsed * 1000))
        sleep(limits.settle_poll_s)


# --- observation ---

def _fingerprint(targets: Sequence[ObservedTarget], windows: Sequence[WindowKey]) -> str:
    data = [[w.identity(), w.is_main, w.is_focused] for w in windows] + [
        [t.role, t.subrole, t.label, t.identifier, t.enabled, t.focused, t.value_summary, t.window_index]
        for t in targets]
    return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()[:32]


def _frontmost_fields(frontmost: Optional[AppIdentity], check: Optional[FrontmostCheck]) -> Dict[str, Any]:
    if check is None:
        return {"frontmost": frontmost}
    return {"frontmost": check.app if check.agreed else None, "frontmost_sources": check.sources,
            "frontmost_diagnostic": check.diagnostic}


def _restricted(app: AppIdentity, front: Dict[str, Any], running: Sequence[AppIdentity], reason: str,
                evie: EvieSelfStatus, now: Callable[[], float], obs_id: str) -> Observation:
    return Observation(obs_id=obs_id, taken_at=now(), app=app, running_apps=tuple(running), **front,
                       settle=SettleInfo(status=SettleStatus.UNABLE_TO_DETERMINE, elapsed_ms=0),
                       counts=ObservationCounts(), restricted=reason, evie_self=evie,
                       fingerprint=_short_hash(f"restricted:{reason}:{app.bundle_id}"))


@dataclass
class ObservationHandles:
    """Live element handles aligned with Observation.windows / .targets. Worker process only; never serialised."""
    app: Any = None
    windows: List[Any] = field(default_factory=list)
    targets: List[Any] = field(default_factory=list)


def observe_app(backend: AXBackend, app_element: Any, app: AppIdentity, **kwargs) -> Observation:
    """One read-only observation of an already-running app. Never acts, never launches, never focuses."""
    return observe_app_with_handles(backend, app_element, app, **kwargs)[0]


def observe_app_with_handles(backend: AXBackend, app_element: Any, app: AppIdentity, *,
                              frontmost: Optional[AppIdentity] = None,
                              frontmost_check: Optional[FrontmostCheck] = None,
                              running_apps: Sequence[AppIdentity] = (),
                              installed_apps: Sequence[AppIdentity] = (),
                              screens: Sequence[Tuple[float, float, float, float]] = (),
                              policy: GatePolicy = DEFAULT_POLICY, limits: ObserverLimits = DEFAULT_LIMITS, settle: bool = True,
                              clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                              now: Callable[[], float] = time.time,
                              obs_id: Optional[str] = None) -> Tuple[Observation, ObservationHandles]:
    obs_id = obs_id or f"obs-{uuid.uuid4().hex[:16]}"
    started = clock()
    running = tuple(running_apps)[:500]
    installed = tuple(installed_apps)[:1000]
    front = _frontmost_fields(frontmost, frontmost_check)
    if app.bundle_id in policy.no_observe_bundles:
        return _restricted(app, front, running, "NO_OBSERVE_APP", EvieSelfStatus.NOT_EXCLUDED, now,
                           obs_id), ObservationHandles()
    if app.pid in policy.evie_pids or app.bundle_id in policy.evie_bundle_ids:
        return _restricted(app, front, running, "EVIE_SELF", EvieSelfStatus.EXCLUDED, now, obs_id), ObservationHandles()

    errors: Counter = Counter()
    windows = discover_windows(backend, app_element, app.pid, limits, errors, policy)
    if settle:
        settle_info = wait_for_settle(
            lambda: skeleton(backend, discover_windows(backend, app_element, app.pid, limits, Counter(), policy),
                             limits, clock),
            limits, clock, sleep)
        windows = discover_windows(backend, app_element, app.pid, limits, errors, policy)   # fresh roots
    else:
        settle_info = SettleInfo(status=SettleStatus.UNABLE_TO_DETERMINE, elapsed_ms=0)

    result = walk(backend, windows, limits, screens, clock,
                  sensitive_app=app.bundle_id in policy.restricted_bundles, app_identity=app)
    for (attribute, role, code), count in list(errors.items()):
        result.errors[(attribute, role, code)] += count
    chosen, truncated = select_targets(result.nodes, limits.max_targets)
    targets = tuple(ObservedTarget(
        index=i, obs_id=obs_id, role=n["role"], subrole=n["subrole"], label=n["label"], identifier=n["identifier"],
        context=n["context"], enabled=n["enabled"], focused=n["focused"], value_summary=n["value_summary"],
        actions=n["actions"], settable=n["settable"], frame=n["frame"], sensitivity=n["sensitivity"],
        window_index=n["window_index"]) for i, n in enumerate(chosen))

    window_keys = tuple(w.key for w in windows)
    if any(w.evie for w in windows):
        evie = EvieSelfStatus.EXCLUDED
    elif any(w.evie is None for w in windows):
        evie = EvieSelfStatus.UNKNOWN
    else:
        evie = EvieSelfStatus.NOT_EXCLUDED
    primary = next((w.key for w in windows if w.key.is_focused and not w.evie), None) or \
        next((w.key for w in windows if w.key.is_main and not w.evie), None)
    ax_errors = tuple(AXErrorRecord(attribute=a[:64], role=r, code=c, count=n)
                      for (a, r, c), n in sorted(result.errors.items(), key=lambda kv: -kv[1])[:200])
    semantic, semantic_truncated = bounded_semantic_graph(result.semantic_nodes, result.root_id,
                                                          limits.max_semantic_nodes)
    if semantic_truncated:          # the graph is a summary; targets (what actions use) are bounded separately
        result.excluded["semantic_nodes_over_limit"] += len(result.semantic_nodes) - len(semantic)
    counts = ObservationCounts(
        total_nodes=result.total, actionable_nodes=result.actionable, on_screen_nodes=result.on_screen,
        excluded_nodes=sum(result.excluded.values()), secure_nodes=result.secure,
        ax_errors=sum(result.errors.values()), windows=len(window_keys), targets=len(targets),
        targets_truncated=truncated)
    observation = Observation(
        obs_id=obs_id, taken_at=now(), app=app, window=primary, windows=window_keys, **front,
        targets=targets, running_apps=running, installed_apps=installed,
        root_id=result.root_id, nodes=semantic,
        settle=settle_info, counts=counts, excluded=dict(result.excluded),
        # The app-level AXMenuBar is never walked (roots are windows), so the menu bar is always pruned;
        # excluded['menu'] counts menus met under windows.
        ax_errors=ax_errors, caps_hit=tuple(sorted(result.caps)), menu_bar_pruned=True, evie_self=evie,
        web_area_present=result.web, duration_ms=round((clock() - started) * 1000, 1),
        fingerprint=_fingerprint(targets, window_keys))
    handles = ObservationHandles(app=app_element, windows=[w.element for w in windows],
                                 targets=[n["element"] for n in chosen])
    return observation, handles
