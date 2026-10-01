"""
Real-Evie activation-boundary diagnostic (docs/07_Computer_Control.md Section 36; Step 14B).

    Evie.app (mode activation_boundary_check) -> evie-python -m services.cc_live_validation.activation_boundary

Question: does the production Evie keep its LaunchServices / NSRunningApplication record after a process
attributed to Evie activates another app? (Foundation-only Evie lost it; the launcher is now an accessory
NSApplication.) This process is Evie's child (Evie is its responsible process) and has no NSApplication -
the shape that triggered the drop.
  1. start the existing worker; require production_identity_verified with bundle lookup FOUND
  2. baseline: sample Evie's bundle lookup (the production lookup code) PRE_SAMPLES times
  3. activate ONE fixed scratch app (TARGET_BUNDLE, executable exactly TARGET_EXECUTABLE) - never a user app,
     never through the worker/executor, no AX; confirm the frontmost change independently (NSWorkspace)
  4. sample Evie's lookup for at least POST_MIN_S seconds; then the worker's production identity verdict again
No arguments; nothing configurable at runtime.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

TARGET_BUNDLE = "com.evie-assistant.scratch.activation-target"
TARGET_DIR = Path.home() / "Library" / "Caches" / "com.evie-assistant.evie" / "activation-boundary"
TARGET_EXECUTABLE = str(TARGET_DIR / "EvieScratchTarget.app" / "Contents" / "MacOS" / "EvieScratchTarget")
NEVER_ACTIVATE = frozenset({"com.apple.finder", "com.apple.TextEdit", "com.apple.Safari", "com.apple.Terminal"})
PRE_SAMPLES, INTERVAL_S, POST_MIN_S = 20, 0.1, 16.0


class AppKitPort:
    """The only OS access of this diagnostic. Read-only except activate(), which is fixed to the scratch target."""

    def __init__(self):
        import AppKit
        import Foundation
        from services.computer_control.macos_probe import _LibProc, _PyObjCApi, _responsible_pid
        self.appkit, self.foundation = AppKit, Foundation
        self.api, self.lp, self._resp = _PyObjCApi(), _LibProc(), _responsible_pid

    def pump(self, seconds: float) -> None:
        self.foundation.NSRunLoop.currentRunLoop().runUntilDate_(
            self.foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds))

    def lookup(self, pid: int) -> Tuple[Optional[str], str]:
        bundle, lookup = self.api.bundle_lookup(pid)                  # the production lookup code
        return bundle, lookup.status

    def responsible(self, pid: int) -> Optional[int]:
        return self._resp(pid)[0]

    def executable(self, pid: int) -> Optional[str]:
        return self.lp.path(pid)

    def scratch_target(self) -> Optional[Tuple[int, Any]]:
        apps = self.appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(TARGET_BUNDLE)
        if not apps or len(apps) != 1:
            return None
        return int(apps[0].processIdentifier()), apps[0]

    def frontmost(self) -> Optional[str]:
        self.pump(0.2)
        app = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.bundleIdentifier()) if app is not None and app.bundleIdentifier() else None

    def activate(self, app) -> bool:
        options = self.appkit.NSApplicationActivateAllWindows | self.appkit.NSApplicationActivateIgnoringOtherApps
        return bool(app.activateWithOptions_(options))


def _identity(status) -> Dict[str, Any]:
    p = status.probe
    resp = p.identity.responsible if p else None
    return {"verified": bool(p and p.production_identity_verified),
            "reasons": list(p.production_identity_reasons) if p else ["NO_PROBE"],
            "responsible_pid": resp.pid if resp else None, "bundle_id": resp.bundle_id if resp else None,
            "bundle_lookup": resp.bundle_lookup.status if resp and resp.bundle_lookup else None,
            "team_id": resp.signature.team_id if resp and resp.signature else None,
            "signature_valid": resp.signature.valid if resp and resp.signature else None,
            "accessibility": p.accessibility.status.value if p else None}


def run_check(worker, port, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    report: Dict[str, Any] = {"status": "BLOCKED", "reason": None, "activation_attempted": False,
                              "production_mutations": 0, "ax_mutations": 0}
    ident = _identity(worker.status)
    report["identity_before"] = ident
    if not (ident["verified"] and ident["bundle_lookup"] == "FOUND"):
        report["reason"] = f"IDENTITY_NOT_VERIFIED:{ident['reasons']}"
        return report
    evie = ident["responsible_pid"]
    if port.responsible(os.getpid()) != evie:
        report["reason"] = "THIS_PROCESS_NOT_ATTRIBUTED_TO_EVIE"
        return report

    def sample(t0: float) -> Dict[str, Any]:
        port.pump(0.05)
        bundle, status = port.lookup(evie)
        return {"t": round(clock() - t0, 2), "lookup": status, "bundle": bundle,
                "responsible": port.responsible(evie), "alive": port.executable(evie) is not None}

    t0 = clock()
    pre = []
    for _ in range(PRE_SAMPLES):
        pre.append(sample(t0))
        sleep(INTERVAL_S)
    report["pre"] = {"samples": len(pre), "states": sorted({s["lookup"] for s in pre})}
    if any(s["lookup"] != "FOUND" for s in pre):
        report["reason"] = "PRE_TRANSITION_LOOKUP_NOT_FOUND"
        report["pre"]["rows"] = pre
        return report

    target = port.scratch_target()
    if target is None:
        report["reason"] = "SCRATCH_TARGET_NOT_RUNNING (nothing is launched)"
        return report
    pid, app = target
    if port.executable(pid) != TARGET_EXECUTABLE or TARGET_BUNDLE in NEVER_ACTIVATE:
        report["reason"] = "SCRATCH_TARGET_IDENTITY_MISMATCH"
        return report
    before = port.frontmost()
    if before == TARGET_BUNDLE:
        report["reason"] = "SCRATCH_TARGET_ALREADY_FRONTMOST (no transition possible)"
        return report
    report["activation_attempted"] = True
    returned = port.activate(app)                                  # the ONE activation: the scratch target only
    after = port.frontmost()
    report["activation"] = {"target_pid": pid, "frontmost_before": before, "returned": returned,
                            "frontmost_after": after, "transition_confirmed": after == TARGET_BUNDLE}
    if after != TARGET_BUNDLE:
        report["reason"] = "ACTIVATION_NOT_CONFIRMED"
        return report

    t1 = clock()
    post = []
    while clock() - t1 < POST_MIN_S:
        post.append(sample(t1))
        sleep(INTERVAL_S)
    bad = [s for s in post if s["lookup"] != "FOUND" or s["responsible"] != evie or not s["alive"]]
    report["post"] = {"samples": len(post), "duration_s": round(clock() - t1, 2),
                      "states": sorted({s["lookup"] for s in post}),
                      "no_running_application_occurred": any(s["lookup"] == "NO_RUNNING_APPLICATION" for s in post),
                      "first_failure": bad[0] if bad else None, "failures": len(bad)}
    after_ident = _identity(worker.probe())
    report["identity_after"] = after_ident
    if bad:
        report["reason"] = "RECORD_LOST_AFTER_ACTIVATION"
    elif not (after_ident["verified"] and after_ident["bundle_lookup"] == "FOUND"):
        report["reason"] = f"IDENTITY_AFTER_NOT_VERIFIED:{after_ident['reasons']}"
    else:
        report["status"] = "ESTABLISHED"
    return report


def main() -> int:
    from services.computer_control.worker import ComputerControlWorker, WorkerState
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60)
    worker.start()
    try:
        report = run_check(worker, AppKitPort())
    except Exception as e:                                          # never hide an unexpected error
        report = {"status": "BLOCKED", "reason": f"HARNESS_ERROR:{type(e).__name__}: {e}"[:300]}
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("ACTIVATION_BOUNDARY_CHECK " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["status"] == "ESTABLISHED" else 1


if __name__ == "__main__":
    sys.exit(main())
