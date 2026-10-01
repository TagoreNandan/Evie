"""
CC-1a live action flow (docs/07_Computer_Control.md Sections 6-12; Step 4) - pure orchestration.

Runs inside the worker process. All OS access goes through the ActionBackend protocol (implemented
with PyObjC in macos_actions.py), so every branch here is unit-testable with fakes.

For each closed ActionRequest:
  frontmost agreement -> latest-observation freshness -> cached target identity -> code-owned gate
  (re-run here, authoritative) -> narrow validated-class check -> live re-validation of the element
  and its window -> before-evidence -> EXACTLY ONE action -> settle -> after-evidence ->
  operation-specific verifier -> re-observation -> ActionResult.
No retries. SUCCESS only on operation-specific verification.

Live-enabled (validated) primitives only:
  activate_app - an ALREADY-RUNNING app in the allowed scope: NSRunningApplication.activate(options:),
             verified only by independent frontmost evidence (never the call's return value); no launch
  focus    - TextEdit AXTextArea: AXRaise + AXMain on its window, AXFocused on the text area (no app activation)
  set_text - Cocoa AXTextArea: AXSelectedText (insert/replace selection) or AXValue (replace_all);
             the text is ALWAYS a span of the user's verbatim utterance
  scroll   - native AXScrollArea: its vertical scroll bar's AXValue, bounded amount
  press    - ONLY TextEdit's alignment segments (AXCheckBox/AXSegment with a known alignment label),
             verified by an expected-effect predicate
"""

import json
import time
from functools import partial
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Optional, Protocol, Tuple

from services.computer_control.actions import get_definition
from services.computer_control.frontmost import FrontmostCheck
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy, context_for, evaluate
from services.computer_control.models import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    ActivateAppArgs,
    EvieSelfStatus,
    ExecutionStatus,
    GateOutcome,
    Observation,
    Op,
    QuitAppArgs,
    CloseWindowArgs,
    ScrollArgs,
    SetTextArgs,
    SettleStatus,
    VerificationStatus,
    normalize,
)
from services.computer_control.observer import ObservationHandles
from services.computer_control.provenance import TextSpanError, text_for
from services.computer_control.verification import (
    CaretVerifier,
    ClickVerifier,
    ClipboardVerifier,
    PasteVerifier,
    ControlStateEvidence,
    ControlStateExpectation,
    ControlStateVerifier,
    ScrollEvidence,
    ScrollVerifier,
    TextEvidence,
    TextExpectation,
    TextVerifier,
    ActivationEvidence,
    ActivationVerifier,
    FocusVerifier,
    SelectionVerifier,
    MenuItemVerifier,
    StateConditionVerifier,
    QuitAppVerifier,
    CloseWindowVerifier,
    VerificationResult,
)

LIVE_OPS = frozenset({
    Op.ACTIVATE_APP, Op.FOCUS, Op.CLICK_ELEMENT, Op.SET_TEXT, Op.SELECT_TEXT, Op.PRESS, Op.PRESS_KEY,
    Op.SCROLL, Op.SELECT, Op.SELECT_TAB, Op.MENU_ITEM_SELECT, Op.COPY_SELECTION,
    Op.PASTE_CLIPBOARD, Op.WAIT_FOR_STATE, Op.CLOSE_WINDOW, Op.QUIT_APP, Op.SWITCH_APP, Op.SWITCH_WINDOW
})
PRESS_KEY_DELTA = {"RIGHT_ARROW": 1, "LEFT_ARROW": -1}   # the only keys (PressKeyArgs); caret movement expected
ACTIVATION_VERIFY_MAX_S = 2.0
LAUNCH_VERIFY_MAX_S = 10.0        # a cold launch (e.g. Photo Booth starting its camera) settles more slowly
CLIPBOARD_VERIFY_MAX_S = 1.0
QUIT_VERIFY_MAX_S = 8.0
ALIGNMENT_LABELS = ("align left", "align center", "align right", "fully justify")
AX_INVALID_ELEMENT = -25202
MAX_OBSERVATION_AGE_S = 60.0
SETTLE_POLL_S = 0.1
SETTLE_MAX_S = 1.5


class ActionBackend(Protocol):
    """Live reads + the few validated mutations. Elements are opaque in-process handles."""
    # reads
    def revalidate(self, element: Any) -> Tuple[int, Dict[str, Any]]: ...   # (err, role/subrole/label/identifier/enabled)
    def main_window(self, app_element: Any) -> Optional[Any]: ...
    def focused_element(self, app_element: Any) -> Optional[Any]: ...
    def is_focused(self, element: Any) -> Optional[bool]: ...
    def text_state(self, element: Any) -> Optional[Tuple[str, Tuple[int, int]]]: ...   # (text, (location, length))
    def scroll_bar(self, scroll_area: Any) -> Optional[Tuple[Any, float]]: ...         # vertical bar + value 0..1
    def visible_start(self, scroll_area: Any) -> Optional[int]: ...
    def segment_values(self, segment: Any) -> Optional[Dict[str, int]]: ...            # sibling segments: label -> 0/1
    def glyph_x(self, window: Any) -> Optional[float]: ...                              # first glyph x of the window's text
    def same(self, a: Any, b: Any) -> bool: ...
    # validated mutations (each returns an AX error code)
    def raise_window(self, window: Any) -> int: ...
    def set_main(self, window: Any) -> int: ...
    def set_focused(self, element: Any) -> int: ...
    def set_selected_text(self, element: Any, text: str) -> int: ...
    def set_value_text(self, element: Any, text: str) -> int: ...
    def set_scroll_value(self, bar: Any, value: float) -> int: ...
    def press_alignment_segment(self, segment: Any) -> int: ...
    def perform_press(self, element: Any) -> int: ...
    def perform_select(self, element: Any) -> int: ...
    # activate_app (already-running apps only)
    def running_pid(self, bundle_id: str) -> Optional[int]: ...
    def is_running(self, pid: int) -> bool: ...
    def is_active(self, pid: int) -> Optional[bool]: ...
    def app_reports_frontmost(self, pid: int) -> Optional[bool]: ...
    def activate_running_app(self, pid: int) -> bool: ...            # the validated call; its return is NOT proof
    # press_key (Step 15C): ONE bounded arrow key, key-down + key-up, to ONE pid; returns 0 once posted
    def post_arrow_key(self, pid: int, key: str) -> int: ...
    def post_copy_keys(self, pid: int) -> int: ...
    def post_paste_keys(self, pid: int) -> int: ...


@dataclass
class ObservationCache:
    """The worker's LATEST observation and its in-process handles. Anything older is stale."""
    observation: Observation
    handles: ObservationHandles
    taken_monotonic: float


Reobserve = Callable[[Optional[int]], Optional[Tuple[Observation, ObservationHandles]]]   # (pid) -> fresh observation


class _Outcome(Exception):
    """Internal early exit carrying a not-executed status."""

    def __init__(self, status: ActionStatus, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _check(condition: bool, status: ActionStatus, reason: str) -> None:
    if not condition:
        raise _Outcome(status, reason)


def _capped(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """ActionResult.evidence holds at most 20 primitive entries; earlier (context) keys win."""
    return dict(list(evidence.items())[:20])


def _settled(read: Callable[[], Any], clock: Callable[[], float], sleep: Callable[[float], None]) -> Any:
    """Poll a read until two consecutive values agree (bounded); the UI may update asynchronously."""
    started, last = clock(), read()
    while clock() - started < SETTLE_MAX_S:
        sleep(SETTLE_POLL_S)
        current = read()
        if current == last:
            return current
        last = current
    return last


def execute_action(request: ActionRequest, *, utterance: str, allowed_apps: FrozenSet[str],
                   cache: Optional[ObservationCache], backend: ActionBackend, frontmost: FrontmostCheck,
                   reobserve: Reobserve, recheck_frontmost: Optional[Callable[[], FrontmostCheck]] = None,
                   policy: GatePolicy = DEFAULT_POLICY,
                   clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                   now: Callable[[], float] = time.time) -> Tuple[ActionResult, Optional[ObservationCache]]:
    """Run ONE closed action. Returns the result and the refreshed cache (the old observation is spent)."""
    requested_at = now()
    evidence: Dict[str, Any] = {"obs_before": request.obs_id, "frontmost_agreed": frontmost.agreed,
                                "frontmost": frontmost.app.bundle_id if frontmost.app else None,
                                "frontmost_sources": ",".join(frontmost.sources)[:120]}
    base = dict(session_id=request.session_id, step=request.step, op=request.op, requested_at=requested_at)

    def not_executed(status: ActionStatus, reason: str) -> ActionResult:
        return ActionResult(**base, status=status, execution=ExecutionStatus.NOT_EXECUTED,
                            verification=VerificationStatus.NOT_APPLICABLE, evidence=_capped(evidence),
                            retry_permitted=status is ActionStatus.STALE, reason=reason)

    try:
        # --- context and freshness (nothing touched yet) ---
        _check(frontmost.agreed, ActionStatus.BLOCKED, f"FRONTMOST_UNRESOLVED: {frontmost.diagnostic}")
        _check(request.op in LIVE_OPS, ActionStatus.BLOCKED, "OP_NOT_LIVE_ENABLED")
        _check(cache is not None and cache.observation.obs_id == request.obs_id, ActionStatus.STALE,
               "OBSERVATION_NOT_LATEST")
        obs, handles = cache.observation, cache.handles
        _check(clock() - cache.taken_monotonic <= MAX_OBSERVATION_AGE_S, ActionStatus.STALE, "OBSERVATION_EXPIRED")
        if request.op is Op.ACTIVATE_APP or request.op is Op.SWITCH_APP:
            return _activate(request, obs, allowed_apps, backend, frontmost, reobserve, recheck_frontmost, policy,
                             evidence, base, cache, clock, sleep, now)
        if request.op is Op.QUIT_APP:
            return _quit_app(request, obs, allowed_apps, backend, frontmost, reobserve, recheck_frontmost, policy,
                             evidence, base, cache, clock, sleep, now)
        if request.op is Op.CLOSE_WINDOW:
            return _close_window(request, obs, allowed_apps, backend, frontmost, reobserve, recheck_frontmost, policy,
                                 evidence, base, cache, clock, sleep, now)
        if request.op is Op.COPY_SELECTION:
            return _copy_selection(request, obs, handles, allowed_apps, backend, frontmost, policy, evidence, base,
                                   cache, clock, sleep, now)
        if request.op is Op.WAIT_FOR_STATE:
            return _wait_for_state(request, obs, allowed_apps, backend, frontmost, reobserve, evidence, base, cache, clock, sleep, now)

        _check(obs.settle.status is SettleStatus.SETTLED and obs.complete, ActionStatus.BLOCKED,
               "OBSERVATION_UNRELIABLE")
        _check(obs.evie_self is not EvieSelfStatus.UNKNOWN, ActionStatus.BLOCKED, "EVIE_STATUS_UNKNOWN")
        target = request.target.target
        _check(target.index < len(obs.targets) and obs.targets[target.index] == target, ActionStatus.STALE,
               "TARGET_NOT_IN_LATEST_OBSERVATION")
        evidence.update(target_role=target.role, target_subrole=target.subrole,
                        target_label=target.label if target.role != "AXTextArea" else None)

        # --- the gate stays authoritative inside the worker ---
        gate = evaluate(context_for(request.op, request.args, request.target, obs, allowed_apps), policy)
        evidence["gate"] = f"{gate.outcome.value}:{gate.risk.value}:{gate.reason}"
        _check(gate.outcome is not GateOutcome.ASK, ActionStatus.BLOCKED, f"ASK:{gate.reason}")
        _check(gate.outcome is GateOutcome.ALLOW, ActionStatus.BLOCKED, gate.reason)

        # --- narrow, validated control classes only ---
        if request.op is Op.FOCUS:
            _check(target.role == "AXTextArea" or target.enabled is not False, ActionStatus.BLOCKED, "FOCUS_TARGET_NOT_FOCUSABLE")
        if request.op is Op.SET_TEXT:
            needed = "AXValue" if request.args.mode == "replace_all" else "AXSelectedText"
            _check(needed in target.settable, ActionStatus.BLOCKED, f"{needed}_NOT_SETTABLE")
        if request.op is Op.PRESS:
            _check(normalize(target.label) in ALIGNMENT_LABELS and "AXPress" in target.actions
                   and target.enabled is True, ActionStatus.BLOCKED, "PRESS_ONLY_FOR_ALIGNMENT_SEGMENTS")
        if request.op is Op.CLICK_ELEMENT:
            _check(target.enabled is not False, ActionStatus.BLOCKED, "CONTROL_DISABLED")
        if request.op in {Op.SELECT, Op.SELECT_TAB, Op.MENU_ITEM_SELECT}:
            _check(target.enabled is not False, ActionStatus.BLOCKED, "CONTROL_DISABLED")
        if request.op is Op.PRESS_KEY:
            _check(target.role == "AXTextArea" and not target.is_secure, ActionStatus.BLOCKED,
                   "PRESS_KEY_ONLY_FOR_TEXT_AREAS")
            _check(sum(1 for t in obs.targets if t.role == "AXTextArea") == 1, ActionStatus.BLOCKED,
                   "TEXT_AREA_NOT_UNIQUE")
        text = None
        if isinstance(request.args, SetTextArgs):
            try:
                text = text_for(utterance, request.args)          # provenance: a span of the utterance only
            except TextSpanError:
                raise _Outcome(ActionStatus.BLOCKED, "TEXT_SPAN_OUTSIDE_UTTERANCE")
            _check(len(text) > 0, ActionStatus.BLOCKED, "EMPTY_TEXT")

        # --- live re-validation of the exact observed element and its window ---
        element = handles.targets[target.index]
        window = handles.windows[target.window_index] if target.window_index is not None else None
        err, live = backend.revalidate(element)
        _check(err != AX_INVALID_ELEMENT, ActionStatus.STALE, "ELEMENT_GONE")
        _check(err == 0, ActionStatus.STALE, f"REVALIDATE_ERROR:{err}")
        observed = {"role": target.role, "subrole": target.subrole, "label": target.label,
                    "identifier": target.identifier, "enabled": target.enabled}
        _check(live == observed, ActionStatus.STALE, "TARGET_CHANGED")
        _check(window is not None, ActionStatus.BLOCKED, "NO_WINDOW_CONTEXT")
        main = backend.main_window(handles.app)
        is_main = main is not None and backend.same(main, window)
        evidence["window_main_before"] = is_main
        if request.op is not Op.FOCUS:
            _check(is_main, ActionStatus.BLOCKED, "NOT_MAIN_WINDOW_LIVE")

        runners = {
            Op.FOCUS: _focus, Op.SET_TEXT: _set_text, Op.SCROLL: _scroll, Op.PRESS: _press,
            Op.CLICK_ELEMENT: _click_element, Op.SELECT: _select, Op.SELECT_TAB: _select_tab,
            Op.MENU_ITEM_SELECT: _menu_item_select, Op.SELECT_TEXT: _select_text
        }
        if request.op in (Op.PRESS_KEY, Op.PASTE_CLIPBOARD):
            # --- the key goes to a PROCESS: that process must be the observed, frontmost app, freshly resolved,
            #     and its focused element must be exactly this text element ---
            pid = obs.app.pid
            _check(frontmost.app is not None and frontmost.app.bundle_id == obs.app.bundle_id
                   and frontmost.app.pid == pid, ActionStatus.BLOCKED, "TARGET_APP_NOT_FRONTMOST")
            _check(backend.running_pid(obs.app.bundle_id) == pid, ActionStatus.STALE, "TARGET_PID_CHANGED")
            _check(backend.is_active(pid) is True and backend.app_reports_frontmost(pid) is True,
                   ActionStatus.BLOCKED, "TARGET_APP_NOT_ACTIVE")
            focused = backend.focused_element(handles.app)
            _check(focused is not None and backend.same(focused, element) and backend.is_focused(element) is True,
                   ActionStatus.BLOCKED, "TEXT_AREA_NOT_FOCUSED_LIVE")
            evidence["target_pid"] = pid
            runners[request.op] = partial(_press_key if request.op is Op.PRESS_KEY else _paste, pid=pid)

        # --- one action ---
        runner = runners[request.op]
        return _run(runner, request, element, window, handles, text, backend, evidence, base, reobserve, cache,
                    clock, sleep, now)
    except _Outcome as o:
        return not_executed(o.status, o.reason), cache


def _run(runner, request, element, window, handles, text, backend, evidence, base, reobserve, cache,
         clock, sleep, now) -> Tuple[ActionResult, Optional[ObservationCache]]:
    before, perform, after, verify, mechanism = runner(request, element, window, handles, text, backend, evidence)
    executed_start = now()
    t0 = clock()
    err = perform()                                   # EXACTLY ONE action (focus = its validated 3-call sequence)
    latency_ms = round((clock() - t0) * 1000, 1)
    executed_end = now()
    common = dict(**base, mechanism=mechanism, executed_start=executed_start, executed_end=executed_end,
                  latency_ms=latency_ms, ax_error=err,
                  state_layers=get_definition(request.op).state_layers)
    if err != 0:
        refreshed = reobserve(cache.observation.app.pid)
        new_cache = ObservationCache(*refreshed, taken_monotonic=clock()) if refreshed else None
        evidence["obs_after"] = refreshed[0].obs_id if refreshed else None
        return ActionResult.executed(execution=ExecutionStatus.ERROR, verification=VerificationStatus.NOT_APPLICABLE,
                                     fingerprint_changed=False, evidence=_capped(evidence), reason=f"AX_ERROR:{err}",
                                     **common), new_cache
    after_state = _settled(after, clock, sleep)
    outcome: VerificationResult = verify(before, after_state)
    refreshed = reobserve(cache.observation.app.pid)  # fresh observation: the old one is spent
    new_cache = ObservationCache(*refreshed, taken_monotonic=clock()) if refreshed else None
    changed = refreshed is not None and refreshed[0].fingerprint != cache.observation.fingerprint
    if request.op is Op.CLICK_ELEMENT:                # independent second signal: the app's re-observed UI
        outcome = ClickVerifier().verify(control_changed=outcome.status is VerificationStatus.VERIFIED,
                                         ui_changed=changed)
    evidence.update(obs_after=refreshed[0].obs_id if refreshed else None, verifier=outcome.reason)
    evidence.update({k: v for k, v in outcome.evidence.items() if k not in evidence})
    return ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=outcome.status,
                                 fingerprint_changed=changed, evidence=_capped(evidence), verified_at=now(),
                                 reason=None if outcome.status is VerificationStatus.VERIFIED else outcome.reason,
                                 **common), new_cache


# --- operation runners: (before, perform, after, verify, mechanism) ---

def _focus(request, element, window, handles, text, backend, evidence):
    def state():
        focused = backend.focused_element(handles.app)
        return (focused is not None and backend.same(focused, element), backend.is_focused(element))

    before = state()
    evidence["focused_before"] = bool(before[0] and before[1])

    def perform():
        # The validated in-app focus sequence (EVIDENCE.md 4): raise + make main + focus. No app activation.
        for err in (backend.raise_window(window), backend.set_main(window), backend.set_focused(element)):
            if err != 0:
                return err
        return 0

    def verify(b, a):
        evidence["focused_after"] = bool(a[0] and a[1])
        if a[0] and a[1] is True and not (b[0] and b[1]):
            return VerificationResult(status=VerificationStatus.VERIFIED, reason="TARGET_BECAME_FOCUSED")
        if a[0] and a[1] is True:
            return VerificationResult(status=VerificationStatus.VERIFIED_NO_CHANGE, reason="ALREADY_FOCUSED")
        if a == b:
            return VerificationResult(status=VerificationStatus.VERIFIED_NO_CHANGE, reason="FOCUS_UNCHANGED")
        return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="FOCUS_ELSEWHERE")

    return before, perform, state, verify, "ax.raise_main_focus"


def _set_text(request, element, window, handles, text, backend, evidence):
    before = backend.text_state(element)
    if before is None:
        raise _Outcome(ActionStatus.BLOCKED, "TEXT_STATE_UNREADABLE")
    original, (location, length) = before
    replace_all = request.args.mode == "replace_all"
    if replace_all:
        expected_text, expected_sel = text, None
    else:
        expected_text = original[:location] + text + original[location + length:]
        expected_sel = (location + len(text), 0)
    evidence.update(chars_before=len(original), selection_before=f"{location},{length}", mode=request.args.mode)

    def perform():
        return backend.set_value_text(element, text) if replace_all else backend.set_selected_text(element, text)

    def after():
        return backend.text_state(element)

    def verify(b, a):
        if a is None:
            return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="TEXT_UNREADABLE_AFTER")
        evidence.update(chars_after=len(a[0]), exact_text=a[0] == expected_text)
        return TextVerifier().verify(
            TextEvidence(local_text=b[0], char_count=len(b[0]), selection=b[1]),
            TextEvidence(local_text=a[0], char_count=len(a[0]), selection=a[1]),
            TextExpectation(local_text=expected_text, char_delta=len(expected_text) - len(b[0]),
                            selection=expected_sel))

    return before, perform, after, verify, "ax.set_value" if replace_all else "ax.set_selected_text"


def _scroll_position(backend, element) -> Tuple[Optional[str], Optional[int]]:
    """An independent position signal (never the scroll bar value that is set): the first visible character of a
    text view, else the scrolled content's offset above the scroll area's top edge. Both grow when scrolling down."""
    start = backend.visible_start(element)
    if start is not None:
        return "visible_text", start
    reader = getattr(backend, "scroll_content_offset", None)
    offset = reader(element) if reader is not None else None
    return ("content_offset", offset) if offset is not None else (None, None)


def _scroll(request, element, window, handles, text, backend, evidence):
    args: ScrollArgs = request.args
    bar = backend.scroll_bar(element)
    if bar is None:
        raise _Outcome(ActionStatus.BLOCKED, "SCROLL_STATE_UNREADABLE:NO_VERTICAL_SCROLL_BAR")
    signal, start = _scroll_position(backend, element)
    if start is None:
        raise _Outcome(ActionStatus.BLOCKED, "SCROLL_STATE_UNREADABLE:NO_INDEPENDENT_POSITION")
    bar_element, value = bar
    target_value = min(1.0, value + args.amount) if args.direction == "down" else max(0.0, value - args.amount)
    evidence.update(scroll_before=round(value, 4), scroll_target=round(target_value, 4), visible_before=start,
                    scroll_signal=signal)
    if target_value == value:
        raise _Outcome(ActionStatus.BLOCKED, "AT_SCROLL_LIMIT")

    def perform():
        return backend.set_scroll_value(bar_element, target_value)

    def after():
        return _scroll_position(backend, element)[1] if signal == "content_offset" else backend.visible_start(element)

    def verify(b, a):
        if a is None:
            return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="VISIBLE_RANGE_UNREADABLE")
        evidence["visible_after"] = a
        return ScrollVerifier().verify(ScrollEvidence(visible_start=b), ScrollEvidence(visible_start=a), args.direction)

    return start, perform, after, verify, "ax.scrollbar_value"


def _select_text(request, element, window, handles, text, backend, evidence):
    """Select a range of the text element (bounded to its text). Verified by reading the range back; the text
    itself must not change."""
    before = backend.text_state(element)
    if before is None:
        raise _Outcome(ActionStatus.BLOCKED, "TEXT_STATE_UNREADABLE")
    original, selection = before
    location = min(request.args.location, len(original))
    length = min(request.args.length, len(original) - location)
    _check(length > 0, ActionStatus.BLOCKED, "NOTHING_TO_SELECT")
    evidence.update(chars_before=len(original), range_before=f"{selection[0]},{selection[1]}")

    def perform():
        return backend.set_selected_range(element, location, length)

    def after():
        return backend.text_state(element)

    def verify(b, a):
        if a is None:
            return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="TEXT_STATE_UNREADABLE_AFTER")
        evidence.update(range_after=f"{a[1][0]},{a[1][1]}", text_unchanged=a[0] == b[0])
        if a[0] != b[0]:
            return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="TEXT_CHANGED")
        if a[1] == (location, length):
            # the requested range is what is selected now (already selected before is the same end state)
            return VerificationResult(status=VerificationStatus.VERIFIED,
                                      reason="RANGE_SELECTED" if b[1] != a[1] else "RANGE_ALREADY_SELECTED")
        return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="RANGE_NOT_SELECTED")

    return before, perform, after, verify, "ax.set_selected_text_range"


def _press(request, element, window, handles, text, backend, evidence):
    target_label = normalize(request.target.target.label)
    values = backend.segment_values(element)
    if not values or set(values) - set(ALIGNMENT_LABELS) or target_label not in values:
        raise _Outcome(ActionStatus.BLOCKED, "NOT_THE_VALIDATED_ALIGNMENT_GROUP")
    if values[target_label] == 1:
        raise _Outcome(ActionStatus.BLOCKED, "ALREADY_IN_REQUESTED_STATE")
    expected = {label: (1 if label == target_label else 0) for label in values}
    glyph = backend.glyph_x(window)
    evidence.update(values_before=json.dumps(values, sort_keys=True), glyph_before=glyph)

    def perform():
        return backend.press_alignment_segment(element)

    def after():
        return (backend.segment_values(element), backend.glyph_x(window))

    def verify(b, a):
        a_values, a_glyph = a
        if not a_values:
            return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="STATE_UNREADABLE_AFTER")
        evidence.update(values_after=json.dumps(a_values, sort_keys=True), glyph_after=a_glyph)
        return ControlStateVerifier().verify(
            ControlStateEvidence(values=b, independent_signal=glyph),
            ControlStateEvidence(values=a_values, independent_signal=a_glyph),
            ControlStateExpectation(values=expected, independent_signal_must_change=glyph is not None))

    return values, perform, after, verify, "ax.press"


def _press_key(request, element, window, handles, text, backend, evidence, *, pid: int):
    key = request.args.key
    delta = PRESS_KEY_DELTA[key]                                  # KeyError for anything outside the closed set
    before = backend.text_state(element)
    if before is None:
        raise _Outcome(ActionStatus.BLOCKED, "TEXT_STATE_UNREADABLE")
    original, (location, length) = before
    _check(length == 0, ActionStatus.BLOCKED, "SELECTION_NOT_EMPTY")
    _check(0 <= location + delta <= len(original), ActionStatus.BLOCKED, "CARET_AT_LIMIT")
    evidence.update(key=key, range_before=f"{location},{length}", chars_before=len(original))

    def perform():
        return backend.post_arrow_key(pid, key)                    # posting is not proof; the caret is

    def after():
        return backend.text_state(element)

    def verify(b, a):
        if a is None:
            return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="TEXT_STATE_UNREADABLE_AFTER")
        evidence.update(range_after=f"{a[1][0]},{a[1][1]}", text_unchanged=a[0] == b[0])
        return CaretVerifier().verify(b, a, delta)

    return before, perform, after, verify, "cg.post_to_pid_arrow"


# --- activate_app: an already-running app, verified by independent frontmost evidence ---

def _activate(request, obs, allowed_apps, backend, frontmost, reobserve, recheck_frontmost, policy, evidence, base,
              cache, clock, sleep, now) -> Tuple[ActionResult, Optional[ObservationCache]]:
    args: ActivateAppArgs = request.args
    evidence["target_bundle"] = args.bundle_id
    gate = evaluate(context_for(request.op, args, None, obs, allowed_apps), policy)
    evidence["gate"] = f"{gate.outcome.value}:{gate.risk.value}:{gate.reason}"
    _check(gate.outcome is not GateOutcome.ASK, ActionStatus.BLOCKED, f"ASK:{gate.reason}")
    _check(gate.outcome is GateOutcome.ALLOW, ActionStatus.BLOCKED, gate.reason)
    _check(recheck_frontmost is not None, ActionStatus.BLOCKED, "NO_INDEPENDENT_VERIFIER")

    known = {a.bundle_id: a for a in obs.running_apps}
    app = known.get(args.bundle_id)
    live_pid = backend.running_pid(args.bundle_id) if (backend and hasattr(backend, "running_pid")) else (app.pid if app else None)
    if app is not None:
        evidence["target_pid"] = app.pid
        running = backend.is_running(app.pid) if hasattr(backend, "is_running") else True
        _check(live_pid is not None and running, ActionStatus.BLOCKED,
               "APP_NOT_RUNNING (nothing is launched)")
        _check(live_pid == app.pid, ActionStatus.STALE, "APP_PROCESS_CHANGED")
        _check(frontmost.app is None or frontmost.app.bundle_id != args.bundle_id, ActionStatus.BLOCKED, "ALREADY_FRONTMOST")
        executed_start = now()
        t0 = clock()
        returned = bool(backend.activate_running_app(app.pid)) if hasattr(backend, "activate_running_app") else True
        latency_ms = round((clock() - t0) * 1000, 1)
        executed_end = now()
        evidence["activate_returned"] = returned
    else:
        executed_start = now()
        t0 = clock()
        returned = bool(getattr(backend, "launch_app", lambda b: False)(args.bundle_id))
        latency_ms = round((clock() - t0) * 1000, 1)
        executed_end = now()
        evidence["activate_returned"] = returned
        _check(returned, ActionStatus.BLOCKED, "APP_NOT_RUNNING_OR_UNKNOWN (nothing is launched)")

    # Independent verification: poll the cross-check (bounded) until it agrees on the target, or give up.
    launched = app is None
    limit = LAUNCH_VERIFY_MAX_S if launched else ACTIVATION_VERIFY_MAX_S
    after = recheck_frontmost()
    started = clock()
    while not (after.agreed and after.app is not None and after.app.bundle_id == args.bundle_id) \
            and clock() - started < limit:
        sleep(SETTLE_POLL_S)
        after = recheck_frontmost()
    # The target's pid is resolved AFTER the window: a launched app has no pid before it, and the frontmost
    # reading's pid is used only when it is the target app (never another app's pid).
    current_pid = backend.running_pid(args.bundle_id) if hasattr(backend, "running_pid") else None
    front_pid = after.app.pid if after.app is not None and after.app.bundle_id == args.bundle_id else None
    target_pid = current_pid or front_pid or (live_pid if not launched else None)
    evidence.update(launched=launched, target_pid_after=target_pid,
                    target_pid_changed=bool(live_pid and target_pid and target_pid != live_pid))
    running = backend.is_running(target_pid) if target_pid else False
    ax_front = backend.app_reports_frontmost(target_pid) if running and target_pid else None
    is_active = backend.is_active(target_pid) if running and target_pid else None
    evidence.update(frontmost_after=after.app.bundle_id if after.app else None, frontmost_after_agreed=after.agreed,
                    frontmost_after_diagnostic=after.diagnostic[:120],
                    frontmost_after_sources=",".join(after.sources)[:120], target_ax_frontmost_after=ax_front,
                    target_is_active_after=is_active, target_running_after=running)

    refreshed = reobserve(target_pid) if running and target_pid else None           # fresh observation of the activated app
    new_cache = ObservationCache(*refreshed, taken_monotonic=clock()) if refreshed else None
    evidence["obs_after"] = refreshed[0].obs_id if refreshed else None
    common = dict(**base, mechanism="ns_running_application.activate", executed_start=executed_start,
                  executed_end=executed_end, latency_ms=latency_ms,
                  state_layers=get_definition(Op.ACTIVATE_APP).state_layers)

    if not returned:
        return ActionResult.executed(execution=ExecutionStatus.ERROR, verification=VerificationStatus.NOT_APPLICABLE,
                                     fingerprint_changed=False, evidence=_capped(evidence),
                                     reason="ACTIVATE_RETURNED_FALSE", **common), new_cache
    if not target_pid:
        outcome = VerificationResult(status=VerificationStatus.INCONCLUSIVE,
                                     reason="TARGET_PROCESS_NOT_FOUND" if launched else "TARGET_PROCESS_EXITED")
    elif not running:
        outcome = VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="TARGET_PROCESS_EXITED")
    elif evidence["target_pid_changed"]:
        outcome = VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="TARGET_PROCESS_CHANGED")
    elif not after.agreed:
        outcome = VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="FRONTMOST_SOURCES_DISAGREE")
    else:
        readings = tuple(r.app.bundle_id for r in after.readings if r.app is not None)
        outcome = ActivationVerifier().verify(args.bundle_id, ActivationEvidence(
            frontmost_readings=readings, target_is_active=is_active, target_ax_frontmost=ax_front))
    evidence["verifier"] = outcome.reason
    changed = after.app is not None and frontmost.app is not None and after.app.bundle_id != frontmost.app.bundle_id
    return ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=outcome.status,
                                 fingerprint_changed=changed, evidence=_capped(evidence), verified_at=now(),
                                 reason=None if outcome.status is VerificationStatus.VERIFIED else outcome.reason,
                                 **common), new_cache


def _click_element(request, element, window, handles, text, backend, evidence):
    reader = getattr(backend, "control_state", None)

    def state():
        if reader is not None:
            return reader(element)
        err, live = backend.revalidate(element)
        return tuple(sorted(live.items())) if err == 0 else None

    before = state()

    def perform():
        return backend.perform_press(element)

    def after():
        return state()

    def verify(b, a):
        # The control's own state (value/selected/expanded/title; None = it disappeared). Only a CHANGE counts;
        # _run adds the app's re-observed UI as the second, independent signal.
        evidence["control_state_changed"] = a != b
        if a != b:
            return VerificationResult(status=VerificationStatus.VERIFIED, reason="CONTROL_STATE_CHANGED")
        return VerificationResult(status=VerificationStatus.INCONCLUSIVE, reason="CONTROL_STATE_UNCHANGED")

    return before, perform, after, verify, "ax.perform_press"


def _select(request, element, window, handles, text, backend, evidence):
    def state():
        err, live = backend.revalidate(element)
        return live.get("selected") if err == 0 else None

    before = state()

    def perform():
        return backend.perform_select(element)

    def after():
        return state()

    def verify(b, a):
        return SelectionVerifier().verify(b, a)

    return before, perform, after, verify, "ax.perform_select"


def _select_tab(request, element, window, handles, text, backend, evidence):
    def state():
        err, live = backend.revalidate(element)
        return live.get("selected") if err == 0 else None

    before = state()

    def perform():
        return backend.perform_select(element)

    def after():
        return state()

    def verify(b, a):
        return SelectionVerifier().verify(b, a)

    return before, perform, after, verify, "ax.perform_select_tab"


def _menu_item_select(request, element, window, handles, text, backend, evidence):
    def state():
        err, live = backend.revalidate(element)
        return live if err == 0 else None

    before = state()

    def perform():
        return backend.perform_press(element)

    def after():
        return state()

    def verify(b, a):
        return MenuItemVerifier().verify(b, a)

    return before, perform, after, verify, "ax.menu_item_press"


def _paste(request, element, window, handles, text, backend, evidence, *, pid: int):
    """Command-V to the frontmost target pid; proven by the field's text, compared with the clipboard in memory."""
    before = backend.text_state(element)
    if before is None:
        raise _Outcome(ActionStatus.BLOCKED, "TEXT_STATE_UNREADABLE")
    concealed, clip = backend.pasteboard_text()
    _check(not concealed, ActionStatus.BLOCKED, "CLIPBOARD_CONCEALED")
    _check(bool(clip), ActionStatus.BLOCKED, "CLIPBOARD_HAS_NO_TEXT")
    original, (location, length) = before
    expected = original[:location] + clip + original[location + length:]
    evidence.update(chars_before=len(original), clipboard_chars=len(clip), selection_before=f"{location},{length}")

    def perform():
        return backend.post_paste_keys(pid)

    def after():
        return backend.text_state(element)

    def verify(b, a):
        evidence.update(chars_after=len(a[0]) if a else None, exact_text=bool(a) and a[0] == expected)
        return PasteVerifier().verify(b[0], a[0] if a else None, expected)

    return before, perform, after, verify, "cg.post_to_pid_paste"


def _copy_selection(request, obs, handles, allowed_apps, backend, frontmost, policy, evidence, base, cache, clock,
                    sleep, now):
    """Command-C to the observed, frontmost app - only when its live focused element is NOT secure and holds a
    non-empty text selection. Verified by pasteboard metadata (change count, length); contents are never kept."""
    gate = evaluate(context_for(request.op, request.args, None, obs, allowed_apps), policy)
    evidence["gate"] = f"{gate.outcome.value}:{gate.risk.value}:{gate.reason}"
    _check(gate.outcome is GateOutcome.ALLOW, ActionStatus.BLOCKED, gate.reason)
    _check(obs.evie_self is not EvieSelfStatus.UNKNOWN, ActionStatus.BLOCKED, "EVIE_STATUS_UNKNOWN")
    pid = obs.app.pid
    _check(frontmost.app is not None and frontmost.app.bundle_id == obs.app.bundle_id
           and frontmost.app.pid == pid, ActionStatus.BLOCKED, "TARGET_APP_NOT_FRONTMOST")
    _check(backend.running_pid(obs.app.bundle_id) == pid, ActionStatus.STALE, "TARGET_PID_CHANGED")
    _check(backend.is_active(pid) is True, ActionStatus.BLOCKED, "TARGET_APP_NOT_ACTIVE")
    focused = backend.focused_element(handles.app) if handles is not None else None
    _check(focused is not None, ActionStatus.BLOCKED, "NO_FOCUSED_ELEMENT")
    _check(backend.is_secure_element(focused) is False, ActionStatus.BLOCKED, "SECURE_FIELD")
    selected = backend.selected_text_length(focused)
    _check(bool(selected), ActionStatus.BLOCKED, "NO_TEXT_SELECTED")
    count_before = backend.pasteboard_change_count()
    _check(count_before is not None, ActionStatus.BLOCKED, "CLIPBOARD_UNREADABLE")
    evidence.update(target_pid=pid, selection_chars=selected)

    executed_start = now()
    t0 = clock()
    err = backend.post_copy_keys(pid)
    latency_ms = round((clock() - t0) * 1000, 1)
    executed_end = now()
    common = dict(**base, mechanism="cg.post_to_pid_copy", executed_start=executed_start,
                  executed_end=executed_end, latency_ms=latency_ms, ax_error=err,
                  state_layers=get_definition(Op.COPY_SELECTION).state_layers)
    started = clock()
    count_after = backend.pasteboard_change_count()
    while count_after == count_before and clock() - started < CLIPBOARD_VERIFY_MAX_S:
        sleep(SETTLE_POLL_S)
        count_after = backend.pasteboard_change_count()
    changed = count_after is not None and count_after != count_before
    concealed, clip = backend.pasteboard_text() if changed else (False, None)
    length_matches = clip is not None and len(clip) == selected
    clip = None                                                    # never kept past this comparison
    evidence.update(clipboard_changed=changed, clipboard_length_matches=length_matches)
    outcome = ClipboardVerifier().verify(changed, length_matches)
    evidence["verifier"] = outcome.reason
    return ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=outcome.status,
                                 fingerprint_changed=False, evidence=_capped(evidence), verified_at=now(),
                                 reason=None if outcome.status is VerificationStatus.VERIFIED else outcome.reason,
                                 **common), cache


def _wait_for_state(request, obs, allowed_apps, backend, frontmost, reobserve, evidence, base, cache, clock, sleep, now):
    args = request.args
    t0 = clock()
    timeout_s = getattr(args, "timeout_s", 5.0)
    condition_met = False
    refreshed_cache = cache
    while clock() - t0 < timeout_s:
        refreshed = reobserve(obs.app.pid)
        if refreshed:
            refreshed_cache = ObservationCache(*refreshed, taken_monotonic=clock())
            new_obs = refreshed[0]
            cond = getattr(args, "condition", "settled")
            expected = getattr(args, "expected_value", None)
            if cond == "frontmost" and new_obs.frontmost and new_obs.frontmost.bundle_id == expected:
                condition_met = True
                break
            elif cond == "settled" and new_obs.settle.status == SettleStatus.SETTLED:
                condition_met = True
                break
            elif cond == "window_appeared" and len(new_obs.windows) > len(obs.windows):
                condition_met = True
                break
            elif cond == "control_appeared" and any(t.label == expected for t in new_obs.targets):
                condition_met = True
                break
            elif cond == "control_enabled" and any(t.label == expected and t.enabled is True for t in new_obs.targets):
                condition_met = True
                break
        sleep(0.1)
    latency_ms = round((clock() - t0) * 1000, 1)
    common = dict(**base, mechanism="wait_for_state", executed_start=now(), executed_end=now(),
                  latency_ms=latency_ms, ax_error=0,
                  state_layers=get_definition(Op.WAIT_FOR_STATE).state_layers)
    outcome = StateConditionVerifier().verify(condition_met, getattr(args, "condition", "settled"))
    return ActionResult.executed(execution=ExecutionStatus.EXECUTED if condition_met else ExecutionStatus.ERROR,
                                 verification=outcome.status, fingerprint_changed=condition_met,
                                 evidence=_capped(evidence), verified_at=now(),
                                 reason=None if outcome.status is VerificationStatus.VERIFIED else outcome.reason,
                                 **common), refreshed_cache


def _quit_app(request, obs, allowed_apps, backend, frontmost, reobserve, recheck_frontmost, policy, evidence, base,
              cache, clock, sleep, now) -> Tuple[ActionResult, Optional[ObservationCache]]:
    args = request.args
    bundle_id = getattr(args, "bundle_id", None) or (obs.app.bundle_id if obs.app else None)
    _check(bundle_id is not None, ActionStatus.BLOCKED, "APP_REQUIRED")
    evidence["target_bundle"] = bundle_id
    if "evie" in bundle_id.lower():
        _check(False, ActionStatus.BLOCKED, "EVIE_SELF_PROTECTION")

    gate = evaluate(context_for(request.op, args, None, obs, allowed_apps), policy)
    evidence["gate"] = f"{gate.outcome.value}:{gate.risk.value}:{gate.reason}"
    _check(gate.outcome is not GateOutcome.ASK, ActionStatus.BLOCKED, f"ASK:{gate.reason}")
    _check(gate.outcome is GateOutcome.ALLOW, ActionStatus.BLOCKED, gate.reason)

    known = {a.bundle_id: a for a in obs.running_apps}
    app = known.get(bundle_id)
    live_pid = backend.running_pid(bundle_id) if (backend and hasattr(backend, "running_pid")) else (app.pid if app else None)
    if live_pid is not None and policy is not None and live_pid in policy.evie_pids:
        _check(False, ActionStatus.BLOCKED, "EVIE_SELF_PROTECTION")
    _check(live_pid is not None, ActionStatus.BLOCKED, "APP_NOT_RUNNING")

    executed_start = now()
    t0 = clock()
    returned = bool(backend.quit_app(live_pid)) if (backend and hasattr(backend, "quit_app")) else True
    latency_ms = round((clock() - t0) * 1000, 1)
    executed_end = now()
    evidence["quit_returned"] = returned

    # Apps finish terminating asynchronously (Safari takes seconds): poll, bounded, until the process is gone.
    started = clock()
    sleep(0.3)
    is_running_after = backend.is_running(live_pid) if (backend and hasattr(backend, "is_running")) else False
    while returned and is_running_after and clock() - started < QUIT_VERIFY_MAX_S:
        sleep(SETTLE_POLL_S * 2)
        is_running_after = backend.is_running(live_pid)
    v_res = QuitAppVerifier().verify(is_running_after)
    evidence.update(is_running_after=is_running_after, verification_status=v_res.status.value)

    common = dict(**base, mechanism="ns_running_application.terminate", executed_start=executed_start,
                  executed_end=executed_end, latency_ms=latency_ms, ax_error=0,
                  state_layers=get_definition(Op.QUIT_APP).state_layers)

    if v_res.status is VerificationStatus.VERIFIED:
        res = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=VerificationStatus.VERIFIED,
                                     fingerprint_changed=True, evidence=_capped(evidence), verified_at=now(),
                                     reason=None, **common)
    else:
        res = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=v_res.status,
                                     fingerprint_changed=False, evidence=_capped(evidence), verified_at=now(),
                                     reason=v_res.reason, **common)
    return res, None


def _close_window(request, obs, allowed_apps, backend, frontmost, reobserve, recheck_frontmost, policy, evidence, base,
                  cache, clock, sleep, now) -> Tuple[ActionResult, Optional[ObservationCache]]:
    args = request.args
    gate = evaluate(context_for(request.op, args, request.target, obs, allowed_apps), policy)
    evidence["gate"] = f"{gate.outcome.value}:{gate.risk.value}:{gate.reason}"
    _check(gate.outcome is not GateOutcome.ASK, ActionStatus.BLOCKED, f"ASK:{gate.reason}")
    _check(gate.outcome is GateOutcome.ALLOW, ActionStatus.BLOCKED, gate.reason)

    if obs.app and "evie" in obs.app.bundle_id.lower():
        _check(False, ActionStatus.BLOCKED, "EVIE_SELF_PROTECTION")

    app_pid = obs.app.pid if obs.app else None
    _check(app_pid is not None, ActionStatus.BLOCKED, "NO_TARGET_APP")

    target_handle = None
    if request.target is not None and cache is not None:
        target_handle = cache.handles.for_target(request.target.target.index)
    if target_handle is None and cache is not None and cache.handles is not None:
        target_handle = cache.handles.main_window

    executed_start = now()
    t0 = clock()
    returned = bool(backend.close_window(target_handle)) if (backend and hasattr(backend, "close_window")) else True
    latency_ms = round((clock() - t0) * 1000, 1)
    executed_end = now()
    evidence["close_window_returned"] = returned

    sleep(0.3)
    app_running_after = backend.is_running(app_pid) if (backend and hasattr(backend, "is_running")) else True
    refreshed = reobserve(app_pid) if (app_running_after and app_pid) else None
    window_closed = (refreshed is None or refreshed[0].window is None or refreshed[0].window != obs.window) if refreshed else True
    v_res = CloseWindowVerifier().verify(window_closed=window_closed, app_running=app_running_after)
    evidence.update(window_closed=window_closed, app_running_after=app_running_after, verification_status=v_res.status.value)

    new_cache = ObservationCache(*refreshed, taken_monotonic=clock()) if refreshed else None
    common = dict(**base, mechanism="ax.close_window", executed_start=executed_start,
                  executed_end=executed_end, latency_ms=latency_ms, ax_error=0,
                  state_layers=get_definition(Op.CLOSE_WINDOW).state_layers)

    if v_res.status is VerificationStatus.VERIFIED:
        res = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=VerificationStatus.VERIFIED,
                                     fingerprint_changed=True, evidence=_capped(evidence), verified_at=now(),
                                     reason=None, **common)
    else:
        res = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=v_res.status,
                                     fingerprint_changed=False, evidence=_capped(evidence), verified_at=now(),
                                     reason=v_res.reason, **common)
    return res, new_cache
