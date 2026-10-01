"""
READ-ONLY keyboard-event mechanism/permission preflight under the signed Evie identity (Step 15A).

    Evie.app (mode keyboard_event_preflight) -> evie-python -m services.cc_live_validation.keyboard_preflight

Reports, for THIS Evie-attributed process: whether the Quartz event APIs exist, whether an in-memory keyboard
event can be CONSTRUCTED (it is never posted), and the official read-only preflight values
CGPreflightPostEventAccess / CGPreflightListenEventAccess (these never prompt; the CGRequest* variants are never
called), plus Accessibility trust (prompt off). Identity comes from the existing worker probe (start/stop only).
Target-by-pid identification is shown read-only on Finder (NSRunningApplication + libproc). Nothing is posted,
activated, typed or changed.
"""

import json
import os
import sys

RIGHT_ARROW = 124                                   # kVK_RightArrow - used only to construct an event in memory


def main() -> int:
    import ApplicationServices as AX
    import AppKit
    import Quartz
    from services.computer_control.macos_probe import _LibProc, _responsible_pid
    from services.computer_control.worker import ComputerControlWorker, WorkerState

    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60)
    status = worker.start()
    try:
        p = status.probe
        resp = p.identity.responsible if p else None
        event = Quartz.CGEventCreateKeyboardEvent(None, RIGHT_ARROW, True)        # constructed, NEVER posted
        finder = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")
        fpid = int(finder[0].processIdentifier()) if finder and len(finder) else None
        report = {
            "identity": {"verified": bool(p and p.production_identity_verified),
                         "reasons": list(p.production_identity_reasons) if p else ["NO_PROBE"],
                         "responsible_pid": resp.pid if resp else None, "bundle_id": resp.bundle_id if resp else None,
                         "bundle_lookup": resp.bundle_lookup.status if resp and resp.bundle_lookup else None,
                         "team_id": resp.signature.team_id if resp and resp.signature else None},
            "this_process_responsible_pid": _responsible_pid(os.getpid())[0],
            "api_available": {n: hasattr(Quartz, n) for n in (
                "CGEventCreateKeyboardEvent", "CGEventPostToPid", "CGEventPost", "CGPreflightPostEventAccess",
                "CGPreflightListenEventAccess")},
            "event_constructed": event is not None,
            "event_type": int(Quartz.CGEventGetType(event)) if event is not None else None,
            "event_keycode": int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
            if event is not None else None,
            "preflight_post_event_access": bool(Quartz.CGPreflightPostEventAccess()),
            "preflight_listen_event_access": bool(Quartz.CGPreflightListenEventAccess()),
            "accessibility_trusted": bool(AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: False})),
            "target_pid_example": {"bundle": "com.apple.finder", "pid": fpid,
                                   "executable": _LibProc().path(fpid) if fpid else None},
            "posted_events": 0,
        }
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("KEYBOARD_EVENT_PREFLIGHT " + json.dumps(report, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
