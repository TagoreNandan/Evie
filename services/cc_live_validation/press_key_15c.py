"""
Step 15C live PRODUCTION validation of press_key (docs/07_Computer_Control.md Section 36).

    Evie.app (mode press_key_15c) -> evie-python -m services.cc_live_validation.press_key_15c -> worker

Uses ONLY the production path: Orchestrator.run(Plan) -> session gate -> worker -> executor (which performs the
pinned production keyboard post and verifies the caret). This module posts nothing itself. Sequence, one run, no retries:
  preflight (identity FOUND; leftover fixture TextEdit retired only if it is exactly our unchanged text fixture)
  -> scratch text fixture (test infrastructure) -> plan: activate TextEdit, press_key RIGHT_ARROW,
  press_key LEFT_ARROW (goal: TextEdit frontmost) -> caret back at its start, text exactly EVIE_FIXTURE_MARKER
  (read-only AX) -> end TextEdit (test infrastructure) -> fixture unchanged on disk.
"""

import json
import sys
from typing import Any, Dict

from services.cc_live_validation import fixtures as F
from services.cc_live_validation.step14b import FINDER, TEXT_SEL, TEXTEDIT, Step14B, Stop, _activate
from services.computer_control.models import Op
from services.computer_control.orchestrator import ActionIntent, Goal, Orchestrator, Plan  # noqa: F401
from services.computer_control.worker import ComputerControlWorker, WorkerState

APPROVED_KEYS = frozenset({"RIGHT_ARROW", "LEFT_ARROW"})


def _key(name: str) -> ActionIntent:
    return ActionIntent(op=Op.PRESS_KEY, observe_bundle=TEXTEDIT, target=TEXT_SEL, args={"key": name})


PLANS = {"press_key": Plan(utterance="switch to TextEdit, press right arrow then left arrow",
                           final_observe_bundle=TEXTEDIT, goal=Goal(kind="frontmost_app", bundle_id=TEXTEDIT),
                           intents=(_activate(TEXTEDIT, FINDER), _key("RIGHT_ARROW"), _key("LEFT_ARROW")))}


class PressKey15C(Step14B):
    def __init__(self, worker, lifecycle, read_text=None, **kw):
        super().__init__(worker, lifecycle, plans=PLANS, **kw)
        self.read_text = read_text                  # read-only AX (text, range) of the focused element, by pid

    def _approved(self, request) -> bool:
        if request.op is Op.PRESS_KEY:
            return request.args.key in APPROVED_KEYS
        return request.op is Op.ACTIVATE_APP and request.args.bundle_id == TEXTEDIT

    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"status": "PRESS_KEY_PRODUCTION_BLOCKED", "stopped_at": None, "stop_reason": None}
        try:
            if self.worker.status.state is not WorkerState.READY or not self._identity("start")["ok"]:
                raise Stop("IDENTITY", "worker not ready or identity not verified")
            running = self.lifecycle.textedit_pids()
            if running:
                report["stale_fixture"] = self._retire_stale_fixture(running)
            prep = self._prepare(F.TEXT)
            prep.pop("_obs")
            report["fixture"] = prep
            fobs = self._finder_pre("press_key")
            self._phase("press_key", 3, fobs, "DONE_VERIFIED",
                        {"starting_frontmost": fobs.frontmost.bundle_id if fobs.frontmost else None})
            steps = self.phases[-1]["steps"]
            first, last = steps[1]["evidence"], steps[2]["evidence"]
            report["caret"] = {"start": first.get("range_before"), "after_right": first.get("range_after"),
                               "after_left": last.get("range_after")}
            if first.get("range_before") != last.get("range_after"):
                raise Stop("RESTORATION_NOT_VERIFIED", f"caret {report['caret']}")
            if self.read_text is not None:
                state = self.read_text(prep["textedit_pid"])
                report["final_text_exact"] = bool(state and state[0] == F.TEXT_MARKER)
                if not report["final_text_exact"]:
                    raise Stop("RESTORATION_NOT_VERIFIED", "document text changed")
            report["fixture_end"] = self._end_textedit("press_key_15c")
            if self.lifecycle.disk_hash(F.TEXT) != self.written.get("text"):
                raise Stop("RESTORATION_NOT_VERIFIED", "fixture file changed on disk")
            report["status"] = "PRESS_KEY_PRODUCTION_PASS"
        except Stop as s:
            report.update(stopped_at=s.stage, stop_reason=s.reason)
        except Exception as e:                                        # never hide an unexpected error
            report.update(stopped_at="HARNESS_ERROR", stop_reason=f"{type(e).__name__}: {e}"[:300])
        report.update(production_mutations=len(self.mutations), mutations=self.mutations,
                      unexpected_computer_control_operations=self.unexpected, retries=0,
                      identity_checks=self.identity_checks, phases=self.phases,
                      lifecycle_actions=list(self.lifecycle.actions),
                      textedit_running_at_end=self.lifecycle.textedit_pids())
        return report


def _read_text(pid: int):
    """Read-only: (text, range) of the focused element of pid, via the Step 15B probe's AX reader."""
    from services.cc_live_validation.keyboard_probe import Keyboard
    state = Keyboard().text_state(pid)
    return (state[3], state[2]) if state else None


def main() -> int:
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    worker.start()
    try:
        report = PressKey15C(worker, F.FixtureLifecycle(), read_text=_read_text).run()
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("PRESS_KEY_15C " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["status"] == "PRESS_KEY_PRODUCTION_PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
