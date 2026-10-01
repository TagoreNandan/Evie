"""
CC-1b.1 dedicated live check: two-paragraph set_text through the PRODUCTION path (Section 18 task 4 never exercised
it - the command observed Chrome after a frontmost change). Validation infrastructure only.

    Evie.app (mode two_paragraph_1b1) -> evie-python -m services.cc_live_validation.two_paragraph_1b1 -> worker

Plan (Orchestrator.run -> session gate -> worker; identity sentinel before every execution and after activation):
activate TextEdit -> set_text insert_at_cursor of exactly TWO_PARAGRAPHS (executor verifies the exact text)
-> set_text replace_all EVIE_FIXTURE_MARKER (restore). Then read the document text back (read-only AX), end
TextEdit (test infrastructure) and require the fixture file unchanged on disk. One run, no retries.
"""

import json
import sys
from typing import Any, Dict

from services.cc_live_validation import fixtures as F
from services.cc_live_validation.step14b import FINDER, TEXTEDIT, Step14B, Stop, _activate, _set_text
from services.computer_control.models import Op
from services.computer_control.orchestrator import ActionIntent, Goal, Plan, TargetSelector
from services.computer_control.worker import ComputerControlWorker, WorkerState

TWO_PARAGRAPHS = "First benchmark paragraph, line one.\n\nSecond benchmark paragraph, line two."
UTTERANCE = f"switch to TextEdit, type {TWO_PARAGRAPHS} then restore EVIE_FIXTURE_MARKER"


def _span(text: str):
    start = UTTERANCE.index(text)
    return [start, start + len(text)]


PLANS = {"text": Plan(utterance=UTTERANCE, final_observe_bundle=TEXTEDIT, goal=Goal(kind="frontmost_app",
                                                                                   bundle_id=TEXTEDIT), intents=(
    _activate(TEXTEDIT, FINDER),
    ActionIntent(op=Op.SET_TEXT, observe_bundle=TEXTEDIT, target=TargetSelector(role="AXTextArea"),
                 args={"text_span": _span(TWO_PARAGRAPHS), "mode": "insert_at_cursor"}),
    _set_text(UTTERANCE, "EVIE_FIXTURE_MARKER")))}
EXPECTED_AFTER_INSERT = len(TWO_PARAGRAPHS) + len(F.TEXT_MARKER)      # inserted at the caret (0) before the marker


class TwoParagraphs(Step14B):
    def __init__(self, worker, lifecycle, read_text=None, **kw):
        super().__init__(worker, lifecycle, plans=PLANS, **kw)
        self.read_text = read_text

    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"status": "FAILED", "stopped_at": None, "stop_reason": None}
        try:
            if self.worker.status.state is not WorkerState.READY or not self._identity("start")["ok"]:
                raise Stop("IDENTITY", "worker not ready or identity not verified")
            running = self.lifecycle.textedit_pids()
            if running:
                report["stale_fixture"] = self._retire_stale_fixture(running)
            prep = self._prepare(F.TEXT)
            prep.pop("_obs")
            report["fixture"] = prep
            fobs = self._finder_pre("text")
            self._phase("text", 3, fobs, "DONE_VERIFIED",
                        {"starting_frontmost": fobs.frontmost.bundle_id if fobs.frontmost else None})
            insert = self.phases[-1]["steps"][1]["evidence"]
            report["insert"] = {"exact_text": insert.get("exact_text"), "chars_before": insert.get("chars_before"),
                                "chars_after": insert.get("chars_after"), "expected_chars": EXPECTED_AFTER_INSERT}
            if not (insert.get("exact_text") is True and insert.get("chars_after") == EXPECTED_AFTER_INSERT):
                raise Stop("INSERT_NOT_VERIFIED", json.dumps(report["insert"]))
            if self.read_text is not None:
                state = self.read_text(prep["textedit_pid"])
                report["final_text_exact"] = bool(state and state[0] == F.TEXT_MARKER)
                if not report["final_text_exact"]:
                    raise Stop("RESTORATION_NOT_VERIFIED", "document text differs from the fixture marker")
            report["fixture_end"] = self._end_textedit("two_paragraph")
            report["fixture_file_unchanged"] = self.lifecycle.disk_hash(F.TEXT) == self.written.get("text")
            if not report["fixture_file_unchanged"]:
                raise Stop("RESTORATION_NOT_VERIFIED", "fixture file changed on disk")
            report["status"] = "PASS"
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
    from services.cc_live_validation.keyboard_probe import Keyboard
    state = Keyboard().text_state(pid)
    return (state[3], state[2]) if state else None


def main() -> int:
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    worker.start()
    try:
        report = TwoParagraphs(worker, F.FixtureLifecycle(), read_text=_read_text).run()
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("TWO_PARAGRAPH_1B1 " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
