"""
Live macOS action backend + worker runtime for CC-1a Step 4 (docs/07_Computer_Control.md Section 24+).

Worker process only, main thread; PyObjC imported lazily (via macos_ax). This is the ONLY module
that mutates UI, and only through eight fixed calls (the scope guard pins them):
  AXUIElementPerformAction(window, "AXRaise")                 - focus sequence, step 1
  AXUIElementSetAttributeValue(window, "AXMain", True)        - focus sequence, step 2
  AXUIElementSetAttributeValue(text_area, "AXFocused", True)  - focus sequence, step 3
  AXUIElementSetAttributeValue(text_area, "AXSelectedText", <utterance span>)
  AXUIElementSetAttributeValue(text_area, "AXValue", <utterance span>)          (replace_all)
  AXUIElementSetAttributeValue(scroll_bar, "AXValue", <bounded float>)
  AXUIElementPerformAction(alignment_segment, "AXPress")
  NSRunningApplication.activateWithOptions_(AllWindows | IgnoringOtherApps)   - ALREADY-RUNNING apps only
plus ONE bounded keyboard mechanism (Step 15C, press_key):
  CGEventPostToPid(<frontmost target pid>, key-down/key-up of ARROW_KEYCODES[key])  - RIGHT/LEFT arrow only
None of these launches or quits an app or sends mouse events or text; the one activation call is the
mechanism validated in Phase 0b (EVIDENCE.md 1) and never starts a process. Which element each
may target is decided by executor.py (validated classes only) after the gate.
"""

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from services.computer_control import macos_probe
from services.computer_control.executor import ObservationCache, execute_action
from services.computer_control.frontmost import FrontmostCheck, FrontmostReading, cross_check
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy
from services.computer_control.macos_ax import PyObjCAXBackend
from services.computer_control.models import ActionRequest, ActionResult, ActionStatus, AppIdentity, ExecutionStatus, \
    VerificationStatus, normalize
from services.computer_control.observer import DEFAULT_LIMITS, ObserveResult, observe_app_with_handles, read_identity

MAX_TEXT_CHARS = 200_000
ARROW_KEYCODES = {"RIGHT_ARROW": 124, "LEFT_ARROW": 123}   # kVK_RightArrow / kVK_LeftArrow; nothing else
# nspasteboard.org markers used by password managers for secrets: such items are never read or pasted
CONCEALED_PASTEBOARD_TYPES = frozenset({"org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType",
                                        "com.agilebits.onepassword"})


class PyObjCActionBackend(PyObjCAXBackend):
    # --- reads for re-validation and verification ---

    def revalidate(self, element: Any) -> Tuple[int, Dict[str, Any]]:
        return read_identity(self, element)

    def main_window(self, app_element: Any) -> Optional[Any]:
        err, window = self.copy(app_element, "AXMainWindow")
        return window if err == 0 else None

    def focused_element(self, app_element: Any) -> Optional[Any]:
        err, element = self.copy(app_element, "AXFocusedUIElement")
        return element if err == 0 else None

    def is_focused(self, element: Any) -> Optional[bool]:
        err, value = self.copy(element, "AXFocused")
        return bool(value) if err == 0 and isinstance(value, bool) else None

    def _range(self, element: Any, attribute: str) -> Optional[Tuple[int, int]]:
        err, raw = self.ax.AXUIElementCopyAttributeValue(element, attribute, None)
        if err != 0 or raw is None:
            return None
        ok, r = self.ax.AXValueGetValue(raw, self.ax.kAXValueCFRangeType, None)
        if not ok:
            return None
        return (int(r[0]), int(r[1])) if isinstance(r, tuple) else (int(r.location), int(r.length))

    def text_state(self, element: Any) -> Optional[Tuple[str, Tuple[int, int]]]:
        err, count = self.copy(element, "AXNumberOfCharacters")
        if err != 0 or not isinstance(count, int) or count > MAX_TEXT_CHARS:
            return None
        err, value = self.copy(element, "AXValue")
        selection = self._range(element, "AXSelectedTextRange")
        if err != 0 or not isinstance(value, str) or selection is None:
            return None
        return value, selection

    def scroll_bar(self, scroll_area: Any) -> Optional[Tuple[Any, float]]:
        err, bar = self.copy(scroll_area, "AXVerticalScrollBar")
        if err != 0 or bar is None:
            return None
        err, value = self.copy(bar, "AXValue")
        return (bar, float(value)) if err == 0 and isinstance(value, (int, float)) else None

    def _text_area_in(self, root: Any, depth: int = 4) -> Optional[Any]:
        frontier = [root]
        for _ in range(depth):
            nxt = []
            for element in frontier:
                err, kids, _total = self.children(element, 200)
                for kid in (kids if err == 0 else []):
                    if self.copy(kid, "AXRole")[1] == "AXTextArea":
                        return kid
                    nxt.append(kid)
            frontier = nxt
        return None

    def visible_start(self, scroll_area: Any) -> Optional[int]:
        text_area = self._text_area_in(scroll_area, depth=2)
        visible = self._range(text_area, "AXVisibleCharacterRange") if text_area is not None else None
        return visible[0] if visible else None

    def scroll_content_offset(self, scroll_area: Any) -> Optional[int]:
        """
        Independent scroll signal for scroll areas without a text view (lists, tables, web areas, browsers):
        how far the scrolled content's top edge sits above the scroll area's top edge, in points. It grows
        when scrolling down. Read from AXPosition of the content element only - never set.
        """
        err, area_pos = self.copy(scroll_area, "AXPosition")
        if err != 0 or not isinstance(area_pos, tuple):
            return None
        err, kids, _total = self.children(scroll_area, 50)
        for kid in (kids if err == 0 else []):
            if self.copy(kid, "AXRole")[1] in ("AXScrollBar", None):
                continue
            err_p, pos = self.copy(kid, "AXPosition")
            if err_p == 0 and isinstance(pos, tuple):
                return int(round(area_pos[1] - pos[1]))
        return None

    def control_state(self, element: Any) -> Optional[Tuple[Any, ...]]:
        """
        Live, comparable state of one control for click verification. String values are reduced to a hash
        (never kept or logged); nothing else about the control is read.
        """
        err, names = self.attribute_names(element)
        if err != 0:
            return None
        supported = set(names)
        state = []
        for attribute in ("AXValue", "AXSelected", "AXEnabled", "AXExpanded", "AXTitle", "AXDescription"):
            if attribute not in supported:
                state.append(None)
                continue
            err_v, value = self.copy(element, attribute)
            if err_v != 0 or not isinstance(value, (str, int, float, bool)):
                state.append(None)
            elif isinstance(value, str):
                state.append(hash(value))
            else:
                state.append(value)
        return tuple(state)

    def is_secure_element(self, element: Any) -> Optional[bool]:
        role, subrole = self.copy(element, "AXRole")[1], self.copy(element, "AXSubrole")[1]
        if role is None:
            return None
        return role == "AXSecureTextField" or subrole == "AXSecureTextField"

    def selected_text_length(self, element: Any) -> Optional[int]:
        err, value = self.copy(element, "AXSelectedText")
        return len(value) if err == 0 and isinstance(value, str) else None

    # --- pasteboard metadata (the worker reads the general pasteboard in memory only; nothing is kept) ---

    def pasteboard_change_count(self) -> Optional[int]:
        board = self.appkit.NSPasteboard.generalPasteboard()
        return int(board.changeCount()) if board is not None else None

    def pasteboard_text(self) -> Tuple[bool, Optional[str]]:
        """(concealed, text). Concealed/transient items (password managers mark them) are never read."""
        board = self.appkit.NSPasteboard.generalPasteboard()
        if board is None:
            return False, None
        types = {str(t) for t in (board.types() or [])}
        if types & CONCEALED_PASTEBOARD_TYPES:
            return True, None
        value = board.stringForType_(self.appkit.NSPasteboardTypeString)
        return False, (str(value) if value is not None and len(value) <= MAX_TEXT_CHARS else None)

    def segment_values(self, segment: Any) -> Optional[Dict[str, int]]:
        err, parent = self.copy(segment, "AXParent")
        if err != 0 or parent is None:
            return None
        err, kids, _total = self.children(parent, 50)
        values: Dict[str, int] = {}
        for kid in (kids if err == 0 else []):
            if self.copy(kid, "AXRole")[1] == "AXCheckBox" and self.copy(kid, "AXSubrole")[1] == "AXSegment":
                label = normalize(self.copy(kid, "AXDescription")[1] or self.copy(kid, "AXTitle")[1])
                err_v, value = self.copy(kid, "AXValue")
                if label and err_v == 0 and isinstance(value, (int, bool)):
                    values[label] = int(value)
        return values or None

    def glyph_x(self, window: Any) -> Optional[float]:
        text_area = self._text_area_in(window)
        if text_area is None:
            return None
        rng = self.ax.AXValueCreate(self.ax.kAXValueCFRangeType, (0, 1))
        err, raw = self.ax.AXUIElementCopyParameterizedAttributeValue(text_area, "AXBoundsForRange", rng, None)
        if err != 0 or raw is None:
            return None
        ok, rect = self.ax.AXValueGetValue(raw, self.ax.kAXValueCGRectType, None)
        return round(float(rect.origin.x), 1) if ok else None

    # --- the seven validated mutations ---

    def raise_window(self, window: Any) -> int:
        return int(self.ax.AXUIElementPerformAction(window, "AXRaise"))

    def set_main(self, window: Any) -> int:
        return int(self.ax.AXUIElementSetAttributeValue(window, "AXMain", True))

    def set_focused(self, element: Any) -> int:
        return int(self.ax.AXUIElementSetAttributeValue(element, "AXFocused", True))

    def set_selected_text(self, element: Any, text: str) -> int:
        return int(self.ax.AXUIElementSetAttributeValue(element, "AXSelectedText", text))

    def set_value_text(self, element: Any, text: str) -> int:
        return int(self.ax.AXUIElementSetAttributeValue(element, "AXValue", text))

    def set_selected_range(self, element: Any, location: int, length: int) -> int:
        rng = self.ax.AXValueCreate(self.ax.kAXValueCFRangeType, (int(location), int(length)))
        return int(self.ax.AXUIElementSetAttributeValue(element, "AXSelectedTextRange", rng))

    def set_scroll_value(self, bar: Any, value: float) -> int:
        return int(self.ax.AXUIElementSetAttributeValue(bar, "AXValue", float(value)))

    def post_arrow_key(self, pid: int, key: str) -> int:
        """press_key: exactly one key-down and one key-up of ONE arrow key to ONE pid; no flags, no text."""
        code = ARROW_KEYCODES[key]                                 # KeyError for anything else
        for key_down in (True, False):
            event = self.quartz.CGEventCreateKeyboardEvent(None, code, key_down)
            self.quartz.CGEventPostToPid(pid, event)
        return 0

    def press_alignment_segment(self, segment: Any) -> int:
        return int(self.ax.AXUIElementPerformAction(segment, "AXPress"))

    def perform_press(self, element: Any) -> int:
        """Perform AXPress action on an accessibility element."""
        return int(self.ax.AXUIElementPerformAction(element, "AXPress"))

    def perform_select(self, element: Any) -> int:
        """Set AXSelected / AXValue attribute or fallback to AXPress."""
        err = int(self.ax.AXUIElementSetAttributeValue(element, "AXSelected", True))
        if err == 0:
            return 0
        err_v = int(self.ax.AXUIElementSetAttributeValue(element, "AXValue", 1))
        if err_v == 0:
            return 0
        return int(self.ax.AXUIElementPerformAction(element, "AXPress"))

    def post_copy_keys(self, pid: int) -> int:
        """Bounded Cmd+C to frontmost PID."""
        c_code = 8  # kVK_ANSI_C
        cmd_flag = self.quartz.kCGEventFlagMaskCommand
        for key_down in (True, False):
            event = self.quartz.CGEventCreateKeyboardEvent(None, c_code, key_down)
            self.quartz.CGEventSetFlags(event, cmd_flag if key_down else 0)
            self.quartz.CGEventPostToPid(pid, event)
        return 0

    def post_paste_keys(self, pid: int) -> int:
        """Bounded Cmd+V to frontmost PID."""
        v_code = 9  # kVK_ANSI_V
        cmd_flag = self.quartz.kCGEventFlagMaskCommand
        for key_down in (True, False):
            event = self.quartz.CGEventCreateKeyboardEvent(None, v_code, key_down)
            self.quartz.CGEventSetFlags(event, cmd_flag if key_down else 0)
            self.quartz.CGEventPostToPid(pid, event)
        return 0

    # --- activate_app: already-running apps only ---

    def _running(self, pid: int):
        app = self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return app if app is not None and not app.isTerminated() else None

    def running_pid(self, bundle_id: str) -> Optional[int]:
        app = self.app_by_bundle(bundle_id)
        return app.pid if app is not None else None

    def is_running(self, pid: int) -> bool:
        return self._running(pid) is not None

    def is_active(self, pid: int) -> Optional[bool]:
        self.foundation.NSRunLoop.currentRunLoop().runUntilDate_(          # refresh KVO-backed state
            self.foundation.NSDate.dateWithTimeIntervalSinceNow_(0.1))
        app = self._running(pid)
        return bool(app.isActive()) if app is not None else None

    def activate_running_app(self, pid: int) -> bool:
        """The validated activation call. It cannot start a process: it needs an existing NSRunningApplication."""
        app = self._running(pid)
        if app is None:
            return False
        return bool(app.activateWithOptions_(
            self.appkit.NSApplicationActivateAllWindows | self.appkit.NSApplicationActivateIgnoringOtherApps))

    def quit_app(self, pid: int) -> bool:
        """Terminate a running application by PID using NSRunningApplication.terminate()."""
        app = self._running(pid)
        if app is None:
            return False
        return bool(app.terminate())

    def close_window(self, window_element: Any) -> bool:
        """Close an AXWindow by pressing its AXCloseButton or performing AXCancel."""
        if self.ax is None or window_element is None:
            return False
        err, close_button = self.copy(window_element, "AXCloseButton")
        if err == 0 and close_button is not None:
            err_press = self.ax.AXUIElementPerformAction(close_button, "AXPress")
            if err_press == 0:
                return True
        err_cancel = self.ax.AXUIElementPerformAction(window_element, "AXCancel")
        return err_cancel == 0

    def launch_app(self, bundle_id: str) -> bool:
        """Launch an installed application by bundle identifier using NSWorkspace."""
        if self.appkit is None:
            return False
        ws = self.appkit.NSWorkspace.sharedWorkspace()
        try:
            res = ws.launchAppWithBundleIdentifier_options_additionalEventParamDescriptor_launchIdentifier_(
                bundle_id, 0, None, None
            )
            return bool(res[0] if isinstance(res, tuple) else res)
        except Exception:
            return False


class WorkerRuntime:
    """Worker-side state: one backend, the LATEST observation (+ handles), and the scope to re-observe."""

    def __init__(self, backend=None, policy: Optional[GatePolicy] = None):
        self.blocked = macos_probe._blocked_under_test()
        self.backend = None if self.blocked else (backend or PyObjCActionBackend(DEFAULT_LIMITS))
        self.policy = policy or GatePolicy(evie_pids=DEFAULT_POLICY.evie_pids | {os.getpid(), os.getppid()})
        self.cache: Optional[ObservationCache] = None
        self._scope_pid: Optional[int] = None

    def frontmost(self) -> FrontmostCheck:
        from services.computer_control.macos_ax import frontmost_readings
        return cross_check(frontmost_readings(self.backend))

    def _observe_pid(self, pid: int, settle: bool = True):
        app = self.backend.app_by_pid(pid)
        if app is None:
            return None
        return observe_app_with_handles(self.backend, self.backend.app_element(app.pid), app,
                                        frontmost_check=self.frontmost(), running_apps=self.backend.running_apps(),
                                        screens=self.backend.screens(), policy=self.policy, settle=settle)

    def observe(self, *, bundle_id=None, pid=None, frontmost=False, settle=True) -> ObserveResult:
        from services.computer_control.macos_ax import observe_request
        if self.blocked:
            return ObserveResult(status="UNAVAILABLE", reason="BLOCKED_UNDER_TEST")
        result, handles = observe_request(bundle_id=bundle_id, pid=pid, frontmost=frontmost, settle=settle,
                                          backend=self.backend, policy=self.policy, with_handles=True)
        if result.observation is not None and handles is not None and result.status == "OK":
            self.cache = ObservationCache(result.observation, handles, time.monotonic())
            self._scope_pid = result.observation.app.pid
        else:
            self.cache = None                       # nothing actionable without a fresh, complete observation
        return result

    def act(self, request: ActionRequest, utterance: str, allowed_apps) -> ActionResult:
        if self.blocked:
            return ActionResult(session_id=request.session_id, step=request.step, op=request.op,
                                status=ActionStatus.BLOCKED, execution=ExecutionStatus.NOT_EXECUTED,
                                verification=VerificationStatus.NOT_APPLICABLE, requested_at=time.time(),
                                reason="BLOCKED_UNDER_TEST")
        result, self.cache = execute_action(
            request, utterance=utterance, allowed_apps=frozenset(allowed_apps), cache=self.cache,
            backend=self.backend, frontmost=self.frontmost(),
            reobserve=lambda pid: self._observe_pid(pid) if pid is not None else None,
            recheck_frontmost=self.frontmost, policy=self.policy)
        if self.cache is not None:
            self._scope_pid = self.cache.observation.app.pid
        return result

    def installed_apps(self) -> List[AppIdentity]:
        if self.blocked or self.backend is None:
            return []
        if hasattr(self.backend, "installed_apps"):
            return self.backend.installed_apps()
        return []
