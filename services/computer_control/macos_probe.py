"""
macOS runtime identity + Accessibility permission probe (docs/07_Computer_Control.md Section 14).

This is the ONLY module in the computer-control package that touches PyObjC or the OS, and it is
run only inside the dedicated worker process (worker.py), on that process's main thread.
AppKit/ApplicationServices are imported lazily, so importing this module never requires PyObjC.

Everything here is read-only:
- identity: pid/executable/name via libproc; the responsible process (the app macOS attributes
  TCC permissions to) via the private libSystem symbol responsibility_get_pid_responsible_for_pid,
  reported as unknown when unavailable; bundle ids only via NSRunningApplication lookups;
- trust: AXIsProcessTrustedWithOptions with the prompt option OFF (never triggers a dialog);
- functional probe: two attribute reads (AXRole of an already-running Finder/TextEdit app element,
  and AXRole of its focused window). It never launches, activates, clicks, types, or reads titles,
  values or secure fields. No AX object leaves this module - only strings, ints and error codes.

Under pytest the AX/AppKit part is blocked (like llm_client/os_adapter) unless the explicit live
test sets EVIE_LIVE_MACOS=1.
"""

import ctypes
import os
import sys
import time
from typing import Callable, List, Optional, Tuple

from services.computer_control.runtime import (
    AccessibilityProbe,
    BundleLookup,
    AccessibilityStatus,
    FunctionalProbe,
    FunctionalStatus,
    ProcessInfo,
    RuntimeIdentity,
    RuntimeProbeResult,
)

SAFE_TARGET_BUNDLES = ("com.apple.finder", "com.apple.TextEdit")   # probed only if already running
AX_MESSAGING_TIMEOUT_S = 1.0
MAX_ANCESTRY = 12
LIVE_ENV = "EVIE_LIVE_MACOS"


def _blocked_under_test() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ and os.environ.get(LIVE_ENV) != "1"


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:200]


def _sanitize(e: BaseException) -> str:
    """First line of the message, printable ASCII only, bounded; object reprs (<...>) are dropped."""
    text = str(e).splitlines()[0] if str(e) else ""
    text = "".join(ch for ch in text if 32 <= ord(ch) < 127)
    if "<" in text:
        text = text.split("<", 1)[0].rstrip() + " <object omitted>"
    return text[:120]


# --- libproc / libSystem (read-only process metadata) ---

class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32), ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32), ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32), ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


PROC_PIDTBSDINFO = 3


class _LibProc:
    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)

    def path(self, pid: int) -> Optional[str]:
        buf = ctypes.create_string_buffer(4096)
        n = self.lib.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(4096))
        return buf.value.decode("utf-8", "replace") if n > 0 else None

    def bsd(self, pid: int) -> Optional[_ProcBSDInfo]:
        info = _ProcBSDInfo()
        n = self.lib.proc_pidinfo(ctypes.c_int(pid), PROC_PIDTBSDINFO, ctypes.c_uint64(0), ctypes.byref(info),
                                  ctypes.sizeof(info))
        return info if n == ctypes.sizeof(info) else None

    def name(self, pid: int) -> Optional[str]:
        info = self.bsd(pid)
        if info is None:
            return None
        raw = info.pbi_name or info.pbi_comm
        return raw.decode("utf-8", "replace") or None

    def ppid(self, pid: int) -> Optional[int]:
        info = self.bsd(pid)
        return int(info.pbi_ppid) if info is not None else None


def _responsible_pid(pid: int) -> Tuple[Optional[int], str]:
    try:
        fn = ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid
    except (AttributeError, OSError):
        return None, "private_symbol_unavailable"
    fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_int]
    rpid = fn(pid)
    return (rpid, "responsibility_get_pid_responsible_for_pid") if rpid > 0 else (None, "lookup_returned_no_pid")


# --- PyObjC Accessibility/AppKit (lazy; worker process only) ---

class PyObjCUnavailable(RuntimeError):
    pass


class _PyObjCApi:
    """Thin read-only wrapper. Converts every AX value to a plain str before returning."""

    def __init__(self):
        try:
            import AppKit
            import ApplicationServices
        except ImportError as e:
            raise PyObjCUnavailable(_err(e)) from e
        self._appkit = AppKit
        self._ax = ApplicationServices

    def is_trusted(self) -> bool:
        options = {self._ax.kAXTrustedCheckOptionPrompt: False}   # never show the system prompt
        return bool(self._ax.AXIsProcessTrustedWithOptions(options))

    def bundle_lookup(self, pid: int) -> Tuple[Optional[str], BundleLookup]:
        """The bundle id of a pid via NSRunningApplication, with WHY it is missing. Never raises."""
        try:
            app = self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None:
                return None, BundleLookup(status="NO_RUNNING_APPLICATION")
            bid = app.bundleIdentifier()
        except Exception as e:
            return None, BundleLookup(status="LOOKUP_EXCEPTION", error_type=type(e).__name__[:64],
                                      error_message=_sanitize(e))
        if not bid:
            return None, BundleLookup(status="BUNDLE_ID_ABSENT")
        return str(bid), BundleLookup(status="FOUND")

    def bundle_for_pid(self, pid: int) -> Optional[str]:
        return self.bundle_lookup(pid)[0]

    def main_bundle_id(self) -> Optional[str]:
        bid = self._appkit.NSBundle.mainBundle().bundleIdentifier()
        return str(bid) if bid else None

    def running_pid(self, bundle_id: str) -> Optional[int]:
        apps = self._appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        return int(apps[0].processIdentifier()) if apps and len(apps) else None

    def _app(self, pid: int):
        el = self._ax.AXUIElementCreateApplication(pid)
        self._ax.AXUIElementSetMessagingTimeout(el, AX_MESSAGING_TIMEOUT_S)
        return el

    def app_role(self, pid: int) -> Tuple[int, Optional[str]]:
        err, value = self._ax.AXUIElementCopyAttributeValue(self._app(pid), "AXRole", None)
        return int(err), (str(value)[:64] if err == 0 and value is not None else None)

    def focused_window_role(self, pid: int) -> Tuple[int, Optional[str]]:
        err, window = self._ax.AXUIElementCopyAttributeValue(self._app(pid), "AXFocusedWindow", None)
        if err != 0 or window is None:
            return int(err), None
        werr, role = self._ax.AXUIElementCopyAttributeValue(window, "AXRole", None)
        return int(werr), (str(role)[:64] if werr == 0 and role is not None else None)


# --- identity ---

def _default_signature(pid: int):
    from services.computer_control.macos_signature import process_signature    # Security.framework, read-only
    return process_signature(pid)


def _lookup(api, pid: int) -> Tuple[Optional[str], BundleLookup]:
    try:
        return api.bundle_lookup(pid)
    except Exception as e:                                    # an api without the diagnostic method, or a bug
        return None, BundleLookup(status="LOOKUP_EXCEPTION", error_type=type(e).__name__[:64],
                                  error_message=_sanitize(e))


def collect_identity(api=None, libproc: Optional[_LibProc] = None,
                     signature: Optional[Callable[[int], object]] = None) -> RuntimeIdentity:
    """Identity of THIS process (the worker) and the chain above it. Unknown values stay None.
    With the live API, the code signatures of the worker and of the responsible process are checked too."""
    if signature is None and api is not None:
        signature = _default_signature
    try:
        lp = libproc or _LibProc()
    except OSError:
        lp = None

    def info(pid: int) -> ProcessInfo:
        name = lp.name(pid) if lp else None
        path = lp.path(pid) if lp else None
        bundle, lookup = None, None
        if api is not None:
            bundle, lookup = _lookup(api, pid)
            if lookup.status == "NO_RUNNING_APPLICATION" and lp is not None and path is None and name is None:
                lookup = BundleLookup(status="PROCESS_NOT_FOUND")       # libproc knows no such process either
        return ProcessInfo(pid=pid, name=name, executable=path, bundle_id=bundle, bundle_lookup=lookup)

    pid = os.getpid()
    worker_bundle = None
    if api is not None:
        try:
            worker_bundle = api.main_bundle_id() or api.bundle_for_pid(pid)
        except Exception:
            worker_bundle = None
    worker = ProcessInfo(pid=pid, name=(lp.name(pid) if lp else None) or os.path.basename(sys.executable),
                         executable=os.path.realpath(sys.executable), bundle_id=worker_bundle,
                         signature=signature(pid) if signature else None)

    ancestry: List[ProcessInfo] = []
    current = os.getppid()
    seen = {pid}
    while current and current not in seen and current != 1 and len(ancestry) < MAX_ANCESTRY:
        seen.add(current)
        ancestry.append(info(current))
        nxt = lp.ppid(current) if lp else None
        if nxt is None:
            break
        current = nxt

    rpid, lookup = _responsible_pid(pid)
    responsible = info(rpid) if rpid is not None else None
    if responsible is not None and signature is not None:
        responsible = responsible.model_copy(update={"signature": signature(rpid)})
    return RuntimeIdentity(worker=worker, parent=ancestry[0] if ancestry else None, responsible=responsible,
                           ancestry=tuple(ancestry), responsible_lookup=lookup)


# --- probes ---

def probe_accessibility(api) -> AccessibilityProbe:
    try:
        return AccessibilityProbe(status=AccessibilityStatus.TRUSTED if api.is_trusted()
                                  else AccessibilityStatus.NOT_TRUSTED)
    except Exception as e:
        return AccessibilityProbe(status=AccessibilityStatus.UNAVAILABLE, error=_err(e))


def probe_functional(api, clock: Callable[[], float] = time.perf_counter) -> FunctionalProbe:
    for bundle in SAFE_TARGET_BUNDLES:
        try:
            pid = api.running_pid(bundle)
        except Exception as e:
            return FunctionalProbe(status=FunctionalStatus.FAILED, error=_err(e))
        if pid is None:
            continue
        t0 = clock()
        try:
            err, role = api.app_role(pid)
            werr, wrole = api.focused_window_role(pid) if err == 0 else (None, None)
        except Exception as e:
            return FunctionalProbe(status=FunctionalStatus.FAILED, target_bundle_id=bundle, target_pid=pid,
                                   error=_err(e))
        ok = err == 0 and role == "AXApplication"
        return FunctionalProbe(
            status=FunctionalStatus.OK if ok else FunctionalStatus.FAILED, target_bundle_id=bundle, target_pid=pid,
            app_role_ax_error=err, app_role=role, window_role_ax_error=werr, window_role=wrole,
            elapsed_ms=round((clock() - t0) * 1000, 1),
            error=None if ok else f"AXRole read returned AX error {err}",
        )
    return FunctionalProbe(status=FunctionalStatus.UNAVAILABLE, error="NO_SAFE_TARGET_RUNNING (nothing is launched)")


def probe_runtime(api=None, *, worker_started: bool = True, now: Callable[[], float] = time.time,
                  api_factory: Callable[[], object] = _PyObjCApi) -> RuntimeProbeResult:
    """Measure identity, trust and a functional AX read for THIS process. Never raises."""
    diagnostics: List[str] = []
    if _blocked_under_test():
        diagnostics.append("AX_BLOCKED_UNDER_TEST")
        return RuntimeProbeResult(
            probed_at=now(), worker_started=worker_started, identity=collect_identity(None),
            accessibility=AccessibilityProbe(status=AccessibilityStatus.UNAVAILABLE, error="BLOCKED_UNDER_TEST"),
            functional=FunctionalProbe(status=FunctionalStatus.UNAVAILABLE, error="BLOCKED_UNDER_TEST"),
            diagnostics=tuple(diagnostics))
    if api is None:
        try:
            api = api_factory()
        except PyObjCUnavailable as e:
            diagnostics.append("PYOBJC_UNAVAILABLE")
            return RuntimeProbeResult(
                probed_at=now(), worker_started=worker_started, identity=collect_identity(None),
                accessibility=AccessibilityProbe(status=AccessibilityStatus.UNAVAILABLE, error=str(e)[:200]),
                functional=FunctionalProbe(status=FunctionalStatus.UNAVAILABLE, error="PYOBJC_UNAVAILABLE"),
                diagnostics=tuple(diagnostics))
    identity = collect_identity(api)
    if not identity.sufficient:
        diagnostics.append(f"IDENTITY_INSUFFICIENT:{identity.responsible_lookup}")
    accessibility = probe_accessibility(api)
    functional = probe_functional(api)
    if identity.responsible_is_development_host:
        diagnostics.append("RESPONSIBLE_APP_IS_DEVELOPMENT_HOST")
    return RuntimeProbeResult(probed_at=now(), worker_started=worker_started, identity=identity,
                              accessibility=accessibility, functional=functional, diagnostics=tuple(diagnostics))
