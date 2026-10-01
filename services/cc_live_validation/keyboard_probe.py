"""
Step 15B bounded live press_key PROBE (docs/07_Computer_Control.md Section 36). Validation infrastructure only:
press_key stays DISABLED in production (the gate still blocks it with OP_NOT_ENABLED).

    Evie.app (mode keyboard_probe_15b) -> evie-python -m services.cc_live_validation.keyboard_probe -> worker

Mechanism under test: CGEventPostToPid(<TextEdit pid>, key-down/key-up) for exactly TWO keys (KEYCODES), no
modifiers. The only proof of delivery is the AXSelectedTextRange transition read back from the text area.
Sequence (stops at the first failure; no retries):
  0. TextEdit not running; worker READY; identity verified (bundle lookup FOUND)
  1. scratch text fixture opened in the background (test infrastructure)
  2. activate TextEdit through the PRODUCTION path (orchestrator/session gate/worker, 3-source verification)
  3. for RIGHT_ARROW then LEFT_ARROW: identity re-check -> fresh observation -> resolve the one text area ->
     pre-checks (focused, not secure, main window, pid == observed app == frontmost on independent readings,
     gate verdict for press_key exactly OP_NOT_ENABLED) -> read AXSelectedTextRange -> post key-down + key-up
     -> poll the range (bounded) -> require exactly +1 / back to the original
  4. require the document text unchanged; end TextEdit (test infrastructure); fixture file unchanged on disk
"""

import json
import sys
import time
from typing import Any, Dict, Optional, Tuple

from services.cc_live_validation import fixtures as F
from services.cc_live_validation.step14b import FINDER, TEXTEDIT, Step14B, Stop, _activate
from services.computer_control.gate import context_for, evaluate
from services.computer_control.models import NoArgs, Op, SettleStatus
from services.computer_control.orchestrator import Goal, Orchestrator, Plan, TargetSelector
from services.computer_control.worker import ComputerControlWorker, WorkerState

KEYCODES = {"RIGHT_ARROW": 124, "LEFT_ARROW": 123}          # the ONLY keys; no modifiers, nothing else
EXPECTED_DELTA = {"RIGHT_ARROW": 1, "LEFT_ARROW": -1}
SETTLE_MAX_S, SETTLE_POLL_S = 1.0, 0.05

PROBE_PLANS = {"activate_textedit": Plan(utterance="switch to TextEdit for the keyboard probe (Finder stays)",
                                         final_observe_bundle=TEXTEDIT, goal=Goal(kind="frontmost_app",
                                                                                  bundle_id=TEXTEDIT),
                                         intents=(_activate(TEXTEDIT, FINDER),))}


def _range(rng) -> Tuple[int, int]:
    """PyObjC returns a CFRange as a (location, length) tuple (or a struct with those fields)."""
    if isinstance(rng, tuple):
        return int(rng[0]), int(rng[1])
    return int(rng.location), int(rng.length)


class Keyboard:
    """Read-only AX readings of the TextEdit text area + the single bounded post. Probe process only."""

    def __init__(self):
        import AppKit
        import ApplicationServices as AX
        import Foundation
        import Quartz
        self.appkit, self.ax, self.foundation, self.quartz = AppKit, AX, Foundation, Quartz

    def _focused(self, pid: int):
        app = self.ax.AXUIElementCreateApplication(pid)
        self.ax.AXUIElementSetMessagingTimeout(app, 1.0)
        err, el = self.ax.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
        return el if err == 0 else None

    def text_state(self, pid: int) -> Optional[Tuple[str, Optional[str], Tuple[int, int], str]]:
        """(role, identifier, selected range, document text) of the focused element, or None."""
        el = self._focused(pid)
        if el is None:
            return None
        read = lambda a: self.ax.AXUIElementCopyAttributeValue(el, a, None)
        err_r, role = read("AXRole")
        err_i, ident = read("AXIdentifier")
        err_s, raw = read("AXSelectedTextRange")
        err_v, value = read("AXValue")
        if err_r != 0 or err_s != 0 or err_v != 0 or raw is None:
            return None
        ok, rng = self.ax.AXValueGetValue(raw, self.ax.kAXValueCFRangeType, None)
        if not ok:
            return None
        return str(role), (str(ident) if err_i == 0 and ident else None), _range(rng), str(value)

    def frontmost(self, pid: int) -> Dict[str, Any]:
        self.foundation.NSRunLoop.currentRunLoop().runUntilDate_(
            self.foundation.NSDate.dateWithTimeIntervalSinceNow_(0.2))
        ws = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        app = self.ax.AXUIElementCreateApplication(pid)
        err, front = self.ax.AXUIElementCopyAttributeValue(app, "AXFrontmost", None)
        running = self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return {"nsworkspace": int(ws.processIdentifier()) if ws is not None else None,
                "app_ax_frontmost": bool(front) if err == 0 else None,
                "is_active": bool(running.isActive()) if running is not None else None}

    def post_key(self, pid: int, key: str) -> int:
        """Key-down then key-up of ONE key from KEYCODES to ONE pid. Returns the number of events posted."""
        code = KEYCODES[key]                                         # KeyError for anything else
        posted = 0
        for down in (True, False):
            event = self.quartz.CGEventCreateKeyboardEvent(None, code, down)
            self.quartz.CGEventPostToPid(pid, event)
            posted += 1
        return posted


class KeyboardProbe(Step14B):
    def __init__(self, worker, lifecycle, keyboard, **kw):
        super().__init__(worker, lifecycle, plans=PROBE_PLANS, **kw)
        self.kb, self.events_posted, self.keys = keyboard, 0, []

    def _checked_target(self, key: str, textedit_pid: int) -> Dict[str, Any]:
        """Fresh observation + existing resolver + pre-checks + gate verdict. Raises Stop on any failure."""
        if not self._identity(f"{key}:before_key")["ok"]:
            raise Stop("IDENTITY", ",".join(self.identity_checks[-1]["reasons"]))
        r = self.worker.observe(bundle_id=TEXTEDIT)
        obs = r.observation
        if r.status != "OK" or obs is None or obs.settle.status is not SettleStatus.SETTLED:
            raise Stop("OBSERVE", f"{r.status}: {r.reason}")
        resolved = Orchestrator._resolve(obs, TargetSelector(role="AXTextArea"))
        if resolved.kind != "RESOLVED":
            raise Stop("RESOLVE", f"{resolved.kind}:{resolved.reason}")
        t = resolved.target.target
        window = obs.window_for(t)
        gate = evaluate(context_for(Op.PRESS_KEY, NoArgs(), resolved.target, obs, frozenset({TEXTEDIT})))
        front = self.kb.frontmost(textedit_pid)
        checks = {
            "obs_id": obs.obs_id, "target_index": t.index, "target_focused": t.focused is True,
            "target_not_secure": not t.is_secure, "window_main": bool(window and window.is_main),
            "pid_matches_observed_app": obs.app.pid == textedit_pid,
            "frontmost_observed": bool(obs.frontmost and obs.frontmost.pid == textedit_pid),
            "frontmost_nsworkspace": front["nsworkspace"] == textedit_pid,
            "frontmost_app_ax": front["app_ax_frontmost"] is True, "target_app_active": front["is_active"] is True,
            "gate_press_key": f"{gate.outcome.value}:{gate.reason}",
            # Step 15B ran before press_key was enabled (OP_NOT_ENABLED); after Step 15C the gate ALLOWs it
            "gate_press_key_ok": gate.reason in ("OP_NOT_ENABLED", "LOW_ALLOWED"),
        }
        if not all(v for k, v in checks.items() if k not in ("obs_id", "target_index", "gate_press_key")):
            raise Stop("PRECHECK", json.dumps(checks))
        return checks

    def _press(self, key: str, textedit_pid: int, expected_start: Optional[int]) -> Dict[str, Any]:
        checks = self._checked_target(key, textedit_pid)
        before = self.kb.text_state(textedit_pid)
        if before is None or before[0] != "AXTextArea":
            raise Stop("FOCUSED_ELEMENT", f"focused element is not the text area: {before and before[0]}")
        (loc, length), text = before[2], before[3]
        if length != 0 or (expected_start is not None and loc != expected_start):
            raise Stop("SELECTION_STATE", f"range {before[2]} (expected caret at {expected_start})")
        if not 0 <= loc + EXPECTED_DELTA[key] <= len(text):
            raise Stop("SELECTION_STATE", f"caret {loc} cannot move {EXPECTED_DELTA[key]} in {len(text)} chars")
        self.events_posted += self.kb.post_key(textedit_pid, key)          # exactly one key-down + key-up
        want = (loc + EXPECTED_DELTA[key], 0)
        after, waited = None, 0.0
        while waited <= SETTLE_MAX_S:
            after = self.kb.text_state(textedit_pid)
            if after is not None and after[2] == want:
                break
            self.sleep(SETTLE_POLL_S)
            waited += SETTLE_POLL_S
        row = {"key": key, "keycode": KEYCODES[key], "prechecks": checks, "range_before": list(before[2]),
               "range_after": list(after[2]) if after else None, "expected": list(want),
               "text_unchanged": bool(after and after[3] == text), "settle_ms": round(waited * 1000)}
        row["verified"] = bool(after and after[2] == want and after[3] == text)
        self.keys.append(row)
        if not row["verified"]:
            raise Stop("KEY_NOT_VERIFIED", json.dumps(row))
        return row

    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"status": "PRESS_KEY_BLOCKED", "stopped_at": None, "stop_reason": None}
        try:
            if self.worker.status.state is not WorkerState.READY or not self._identity("start")["ok"]:
                raise Stop("IDENTITY", "worker not ready or identity not verified")
            running = self.lifecycle.textedit_pids()
            if running:                                   # only our unchanged leftover text fixture may be retired
                report["stale_fixture"] = self._retire_stale_fixture(running)
            prep = self._prepare(F.TEXT)
            prep.pop("_obs")
            report["fixture"] = prep
            fobs = self.worker.observe(bundle_id=FINDER).observation
            if fobs is None:
                raise Stop("OBSERVE", "Finder pre-observation failed")
            self._phase("activate_textedit", 1, fobs, "DONE_VERIFIED",
                        {"starting_frontmost": fobs.frontmost.bundle_id if fobs.frontmost else None})
            pid = prep["textedit_pid"]
            right = self._press("RIGHT_ARROW", pid, None)
            self._press("LEFT_ARROW", pid, right["range_after"][0])
            if self.keys[-1]["range_after"] != self.keys[0]["range_before"]:
                raise Stop("RESTORATION_NOT_VERIFIED", "caret not back at its original position")
            final = self.kb.text_state(pid)
            if final is None or final[3] != F.TEXT_MARKER:
                raise Stop("RESTORATION_NOT_VERIFIED", "document text changed")
            report["fixture_end"] = self._end_textedit("keyboard_probe")
            if self.lifecycle.disk_hash(F.TEXT) != self.written.get("text"):
                raise Stop("RESTORATION_NOT_VERIFIED", "fixture file changed on disk")
            report["status"] = "PRESS_KEY_READY_FOR_REVIEW"
        except Stop as s:
            report.update(stopped_at=s.stage, stop_reason=s.reason)
        except Exception as e:                                         # never hide an unexpected error
            report.update(stopped_at="HARNESS_ERROR", stop_reason=f"{type(e).__name__}: {e}"[:300])
        report.update(keys=self.keys, keyboard_events_posted=self.events_posted,
                      production_mutations=len(self.mutations), mutations=self.mutations,
                      unexpected_computer_control_operations=self.unexpected, retries=0,
                      identity_checks=self.identity_checks, phases=self.phases,
                      lifecycle_actions=list(self.lifecycle.actions), textedit_running_at_end=self.lifecycle.textedit_pids())
        return report


def main() -> int:
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    worker.start()
    try:
        report = KeyboardProbe(worker, F.FixtureLifecycle(), Keyboard()).run()
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("KEYBOARD_PROBE_15B " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["status"] == "PRESS_KEY_READY_FOR_REVIEW" else 1


if __name__ == "__main__":
    sys.exit(main())
