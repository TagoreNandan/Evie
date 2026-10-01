"""
Read-only macOS Accessibility backend for the observer (docs/07_Computer_Control.md Section 7).

Runs ONLY inside the computer-control worker process, on its main thread; PyObjC is imported
lazily. The backend exposes reads and nothing else:
  AXUIElementCopyAttributeValue / CopyAttributeValues (bounded slice) / GetAttributeValueCount /
  CopyAttributeNames / CopyActionNames / IsAttributeSettable (a query), plus CFEqual/CFHash,
  NSRunningApplication lookups, the system-wide AXFocusedApplication read, and CGDisplayBounds.
AXUIElementSetMessagingTimeout only sets this client's own request timeout on an element reference;
it does not change the observed application. No action, attribute write, event, launch, activation
or focus change exists here. Raw AX objects never leave this module except as opaque handles to
observer.py inside the same process; Observations contain only plain data.
"""

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from services.computer_control import macos_probe
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy
from services.computer_control.models import AppIdentity
from services.computer_control.observer import DEFAULT_LIMITS, ObserveResult, ObserverLimits, observe_app_with_handles

_BUNDLE_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_]{0,254}$")


class PyObjCAXBackend:
    def __init__(self, limits: ObserverLimits = DEFAULT_LIMITS):
        try:
            import AppKit
            import ApplicationServices
            import CoreFoundation
            import Foundation
            import Quartz
        except ImportError as e:
            raise macos_probe.PyObjCUnavailable(macos_probe._err(e)) from e
        self.ax, self.appkit, self.cf, self.foundation, self.quartz = ApplicationServices, AppKit, CoreFoundation, \
            Foundation, Quartz
        self.timeout = limits.messaging_timeout_s
        self._element_type = ApplicationServices.AXUIElementGetTypeID()
        self._value_type = ApplicationServices.AXValueGetTypeID()

    # --- AXBackend protocol (reads only) ---

    def _prepare(self, element: Any) -> Any:
        self.ax.AXUIElementSetMessagingTimeout(element, self.timeout)
        return element

    def _convert(self, value: Any) -> Any:
        if value is None:
            return None
        type_id = self.cf.CFGetTypeID(value)
        if type_id == self._element_type:
            return self._prepare(value)
        if type_id == self._value_type:
            kind = self.ax.AXValueGetType(value)
            ok, struct = self.ax.AXValueGetValue(value, kind, None)
            if not ok:
                return None
            if kind == self.ax.kAXValueCGPointType:
                return (float(struct.x), float(struct.y))
            if kind == self.ax.kAXValueCGSizeType:
                return (float(struct.width), float(struct.height))
            return None                                   # ranges/rects are not needed by the observer
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            return str(value)                             # plain str, not a PyObjC string proxy
        if isinstance(value, (list, tuple)) or hasattr(value, "count") and hasattr(value, "objectAtIndex_"):
            return [self._convert(v) for v in value]
        return str(value)

    def copy(self, element: Any, attribute: str) -> Tuple[int, Any]:
        err, value = self.ax.AXUIElementCopyAttributeValue(element, attribute, None)
        return int(err), (self._convert(value) if err == 0 else None)

    def children(self, element: Any, limit: int) -> Tuple[int, List[Any], int]:
        err, total = self.ax.AXUIElementGetAttributeValueCount(element, "AXChildren", None)
        if err != 0:
            return int(err), [], 0
        if total == 0:
            return 0, [], 0
        err, values = self.ax.AXUIElementCopyAttributeValues(element, "AXChildren", 0, min(int(total), limit), None)
        if err != 0:
            return int(err), [], int(total)
        return 0, [self._prepare(v) for v in (values or [])], int(total)

    def attribute_names(self, element: Any) -> Tuple[int, List[str]]:
        err, names = self.ax.AXUIElementCopyAttributeNames(element, None)
        return int(err), [str(n) for n in (names or [])]

    def action_names(self, element: Any) -> Tuple[int, List[str]]:
        err, names = self.ax.AXUIElementCopyActionNames(element, None)
        return int(err), [str(n) for n in (names or [])]

    def is_settable(self, element: Any, attribute: str) -> Tuple[int, bool]:
        err, flag = self.ax.AXUIElementIsAttributeSettable(element, attribute, None)
        return int(err), bool(flag)

    def key(self, element: Any) -> Any:
        return self.cf.CFHash(element)

    def same(self, a: Any, b: Any) -> bool:
        return bool(self.cf.CFEqual(a, b))

    # --- process-level reads ---

    def is_trusted(self) -> bool:
        return bool(self.ax.AXIsProcessTrustedWithOptions({self.ax.kAXTrustedCheckOptionPrompt: False}))

    def _identity(self, app) -> Optional[AppIdentity]:
        if app is None:
            return None
        bundle = app.bundleIdentifier()
        if not bundle or not _BUNDLE_OK.match(str(bundle)):
            return None
        name = app.localizedName()
        return AppIdentity(bundle_id=str(bundle), pid=int(app.processIdentifier()),
                           name=str(name)[:200] if name else None)

    def app_by_bundle(self, bundle_id: str) -> Optional[AppIdentity]:
        apps = self.appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        return self._identity(apps[0]) if apps and len(apps) else None

    def app_by_pid(self, pid: int) -> Optional[AppIdentity]:
        return self._identity(self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))

    # --- frontmost readings: each is ONE source; frontmost.cross_check decides (never a fallback) ---

    def ax_focused_application(self) -> Tuple[Optional[AppIdentity], Optional[str]]:
        system = self._prepare(self.ax.AXUIElementCreateSystemWide())
        err, app = self.ax.AXUIElementCopyAttributeValue(system, "AXFocusedApplication", None)
        if err != 0 or app is None:
            return None, f"AX_ERROR:{int(err)}"
        err, pid = self.ax.AXUIElementGetPid(app, None)
        return (self.app_by_pid(int(pid)), None) if err == 0 else (None, f"AX_ERROR:{int(err)}")

    def nsworkspace_frontmost(self) -> Tuple[Optional[AppIdentity], Optional[str]]:
        self.foundation.NSRunLoop.currentRunLoop().runUntilDate_(          # refresh NSWorkspace's KVO state
            self.foundation.NSDate.dateWithTimeIntervalSinceNow_(0.2))
        app = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        ident = self._identity(app)
        return (ident, None) if ident else (None, "NO_READING")

    def app_reports_frontmost(self, pid: int) -> Optional[bool]:
        err, value = self.copy(self.app_element(pid), "AXFrontmost")
        return bool(value) if err == 0 and isinstance(value, bool) else None

    def revalidate(self, element: Any) -> Tuple[int, Dict[str, Any]]:
        from services.computer_control.observer import read_identity
        return read_identity(self, element)

    def running_apps(self) -> List[AppIdentity]:
        self.foundation.NSRunLoop.currentRunLoop().runUntilDate_(
            self.foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))
        out = []
        for app in self.appkit.NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() == 0:                  # regular apps only
                ident = self._identity(app)
                if ident is not None:
                    out.append(ident)
        return out[:500]

    def installed_apps(self) -> List[AppIdentity]:
        """Dynamically discover installed macOS applications via system application directories."""
        app_dirs = [
            "/Applications",
            "/System/Applications",
            "/System/Applications/Utilities",
            "/Applications/Utilities",
            os.path.expanduser("~/Applications"),
        ]
        out = []
        seen = set()
        for d in app_dirs:
            if not os.path.isdir(d):
                continue
            try:
                items = os.listdir(d)
            except Exception:
                continue
            for item in items:
                if item.endswith(".app"):
                    p = os.path.join(d, item)
                    try:
                        bundle = self.foundation.NSBundle.bundleWithPath_(p)
                        if bundle:
                            bid = bundle.bundleIdentifier()
                            if bid and _BUNDLE_OK.match(str(bid)) and str(bid) not in seen:
                                name = (
                                    bundle.objectForInfoDictionaryKey_("CFBundleDisplayName")
                                    or bundle.objectForInfoDictionaryKey_("CFBundleName")
                                    or item[:-4]
                                )
                                seen.add(str(bid))
                                out.append(AppIdentity(bundle_id=str(bid), pid=0, name=str(name)[:200]))
                    except Exception:
                        pass
        return out[:500]

    def app_element(self, pid: int) -> Any:
        return self._prepare(self.ax.AXUIElementCreateApplication(pid))

    def screens(self) -> List[Tuple[float, float, float, float]]:
        err, ids, count = self.quartz.CGGetActiveDisplayList(16, None, None)
        if err != 0:
            return []
        rects = []
        for display in list(ids)[:count]:
            r = self.quartz.CGDisplayBounds(display)
            rects.append((float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height)))
        return rects


def frontmost_readings(backend) -> List[FrontmostReading]:
    """Independent, non-mutating readings: NSWorkspace, AX focused application, and the candidates' own AXFrontmost."""
    readings: List[FrontmostReading] = []
    for source, read in (("nsworkspace", backend.nsworkspace_frontmost), ("ax_focused_application",
                                                                           backend.ax_focused_application)):
        try:
            app, error = read()
        except Exception as e:
            app, error = None, macos_probe._err(e)
        readings.append(FrontmostReading(source=source, app=app, error=error))
    candidates = {r.app.pid: r.app for r in readings if r.app is not None}
    reporting = [a for a in candidates.values() if backend.app_reports_frontmost(a.pid) is True]
    for app in reporting:
        readings.append(FrontmostReading(source="app_ax_frontmost", app=app))
    if not reporting:
        readings.append(FrontmostReading(source="app_ax_frontmost", error="NO_CANDIDATE_REPORTS_FRONTMOST"))
    return readings


def observe_request(*, bundle_id: Optional[str] = None, pid: Optional[int] = None, frontmost: bool = False,
                    settle: bool = True, limits: ObserverLimits = DEFAULT_LIMITS, backend=None,
                    policy: Optional[GatePolicy] = None, with_handles: bool = False):
    """Observe one ALREADY-RUNNING app, read-only. Never launches, activates or focuses anything.
    Returns an ObserveResult, or (ObserveResult, handles) for the worker runtime when with_handles=True."""
    started = time.monotonic()
    handles = None

    def done(status, reason=None, observation=None):
        result = ObserveResult(status=status, reason=reason, observation=observation,
                               duration_ms=round((time.monotonic() - started) * 1000, 1))
        return (result, handles) if with_handles else result

    if sum(x is not None and x is not False for x in (bundle_id, pid, frontmost or None)) != 1:
        return done("ERROR", "exactly one of bundle_id, pid or frontmost is required")
    if macos_probe._blocked_under_test():
        return done("UNAVAILABLE", "BLOCKED_UNDER_TEST")
    try:
        backend = backend or PyObjCAXBackend(limits)
    except macos_probe.PyObjCUnavailable:
        return done("UNAVAILABLE", "PYOBJC_UNAVAILABLE")
    try:
        if not backend.is_trusted():
            return done("NOT_READY", "AX_NOT_TRUSTED")
        check = cross_check(frontmost_readings(backend))      # never substitutes the observed app
        if frontmost and not check.agreed:
            return done("UNAVAILABLE", f"FRONTMOST_UNRESOLVED: {check.diagnostic}")
        app = backend.app_by_bundle(bundle_id) if bundle_id else backend.app_by_pid(pid) if pid is not None \
            else check.app
        if app is None:
            return done("UNAVAILABLE", "APP_NOT_RUNNING (nothing is launched)")
        # Evie's own processes: this worker and the process that started it.
        policy = policy or GatePolicy(evie_pids=DEFAULT_POLICY.evie_pids | {os.getpid(), os.getppid()})
        observation, handles = observe_app_with_handles(
            backend, backend.app_element(app.pid), app, frontmost_check=check, running_apps=backend.running_apps(),
            screens=backend.screens(), policy=policy, limits=limits, settle=settle)
    except Exception as e:
        return done("ERROR", macos_probe._err(e))
    if observation.restricted:
        return done("RESTRICTED", observation.restricted, observation)
    return done("OK", None, observation)
