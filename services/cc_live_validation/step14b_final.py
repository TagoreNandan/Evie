"""
CC-1a Step 14B FINAL continuation (docs/07_Computer_Control.md Section 36): the last authorised live attempt.

    Evie.app (mode live_validation_14b_final) -> evie-python -m services.cc_live_validation.step14b_final -> worker

Validates ONLY what the first run left unvalidated, on the EXISTING scratch text fixture left open by that run.
No fixture lifecycle action at all (nothing is written, opened, closed or terminated). Stops at the first failure;
no retries. Every action goes through the existing Orchestrator.run(Plan) path, with the identity sentinel
(production_identity_verified + bundle lookup FOUND) before EVERY execution:
  0. read-only preflight: TextEdit + Finder running; exactly ONE TextEdit window = the text fixture; the fixture
     file is exactly EVIE_FIXTURE_MARKER; identity verified
  1. SETUP        activate TextEdit                          (goal: TextEdit frontmost)   - authorised option B
  2. ACTIVATION 1 activate Finder                            (goal: Finder frontmost)
  3. ACTIVATION 2 activate TextEdit                          (goal: TextEdit frontmost)
  4. MULTI-STEP   activate Finder -> activate TextEdit -> set_text EVIE_MULTISTEP_TEST -> set_text EVIE_FIXTURE_MARKER
                                                             (goal: TextEdit frontmost; text restored exactly)
  5. read-only postflight: still exactly one TextEdit window = the fixture; fixture file unchanged
"""

import json
import sys
from typing import Any, Dict

from services.cc_live_validation import fixtures as F
from services.cc_live_validation.step14b import (
    FINDER, TEXTEDIT, Step14B, Stop, _activate, _set_text,
)
from services.computer_control.orchestrator import Goal, Plan
from services.computer_control.worker import ComputerControlWorker, WorkerState

U_SETUP = "switch to TextEdit (setup; Finder stays running)"
U_A1 = "switch from TextEdit to Finder"
U_A2 = "switch from Finder to TextEdit"
U_MULTI = "switch to Finder, switch to TextEdit, replace the text with EVIE_MULTISTEP_TEST, restore EVIE_FIXTURE_MARKER"


def _goal(bundle: str) -> Goal:
    return Goal(kind="frontmost_app", bundle_id=bundle)


FINAL_PLANS: Dict[str, Plan] = {
    "setup_activate_textedit": Plan(utterance=U_SETUP, final_observe_bundle=FINDER, goal=_goal(TEXTEDIT),
                                    intents=(_activate(TEXTEDIT, FINDER),)),
    "activation_textedit_to_finder": Plan(utterance=U_A1, final_observe_bundle=FINDER, goal=_goal(FINDER),
                                          intents=(_activate(FINDER, FINDER),)),
    "activation_finder_to_textedit": Plan(utterance=U_A2, final_observe_bundle=FINDER, goal=_goal(TEXTEDIT),
                                          intents=(_activate(TEXTEDIT, FINDER),)),
    "multistep": Plan(utterance=U_MULTI, final_observe_bundle=TEXTEDIT, goal=_goal(TEXTEDIT), intents=(
        _activate(FINDER, TEXTEDIT), _activate(TEXTEDIT, FINDER),
        _set_text(U_MULTI, "EVIE_MULTISTEP_TEST"), _set_text(U_MULTI, "EVIE_FIXTURE_MARKER"))),
}
STAGES = (("setup_activate_textedit", 1), ("activation_textedit_to_finder", 1),
          ("activation_finder_to_textedit", 1), ("multistep", 4))


class Step14BFinal(Step14B):
    def __init__(self, worker, lifecycle: F.FixtureLifecycle, **kw):
        super().__init__(worker, lifecycle, plans=FINAL_PLANS, **kw)
        self.verify_after_activation = False           # the final-continuation record is kept as it ran

    def _fixture_state(self, where: str) -> Dict[str, Any]:
        """READ-ONLY: TextEdit running, exactly one window showing the text fixture, file content exact."""
        pids = self.lifecycle.textedit_pids()
        if len(pids) != 1:
            raise Stop("FIXTURE_NOT_READY", f"{where}: expected one TextEdit process, found {pids}")
        obs = self._textedit_windows(F.TEXT)                 # raises FIXTURE_NOT_READY if >1 window / not found
        content = F.TEXT.path.read_text() if F.TEXT.path.exists() else None
        if content != F.TEXT_MARKER:
            raise Stop("FIXTURE_NOT_READY", f"{where}: fixture file content changed ({len(content or '')} chars)")
        return {"where": where, "textedit_pid": pids[0], "windows": len(obs.windows), "document_matches": True,
                "file_bytes": len(content.encode()), "file_sha256_16": self.lifecycle.disk_hash(F.TEXT),
                "frontmost": obs.frontmost.bundle_id if obs.frontmost else None, "obs_id": obs.obs_id}

    def _finder_observation(self, stage: str):
        r = self.worker.observe(bundle_id=FINDER)
        if r.status != "OK" or r.observation is None:
            raise Stop(f"PHASE_{stage.upper()}_FAILED", f"pre-observation {r.status}: {r.reason}")
        return r.observation

    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"result": "FAILED", "stopped_at": None, "stop_reason": None, "fixture_checks": []}
        try:
            status = self.worker.status
            if status.state is not WorkerState.READY:
                raise Stop("WORKER", f"{status.state.value}: {status.failure}")
            if not self._identity("start")["ok"]:
                raise Stop("IDENTITY", ",".join(self.identity_checks[-1]["reasons"]))
            report["fixture_checks"].append(self._fixture_state("preflight"))
            fobs = self._finder_observation("preflight")
            running = {a.bundle_id for a in fobs.running_apps}
            if not {TEXTEDIT, FINDER} <= running:
                raise Stop("ACTIVATION_FIXTURE_NOT_READY", "TextEdit/Finder not both running (nothing is launched)")
            report["starting_frontmost"] = fobs.frontmost.bundle_id if fobs.frontmost else None
            for name, n in STAGES:
                pre = self._finder_observation(name)
                self._phase(name, n, pre, "DONE_VERIFIED",
                            {"starting_frontmost": pre.frontmost.bundle_id if pre.frontmost else None})
            report["fixture_checks"].append(self._fixture_state("postflight"))
            report["result"] = "PASS"
        except Stop as s:
            report.update(stopped_at=s.stage, stop_reason=s.reason, result=self._classify())
        except Exception as e:                                     # never hide an unexpected error
            report.update(stopped_at="HARNESS_ERROR", stop_reason=f"{type(e).__name__}: {e}"[:300], result="FAILED")
        report.update(
            computer_control_mutation_count=len(self.mutations),
            executed_mutations=sum(1 for m in self.mutations if m["execution"] != "NOT_EXECUTED"),
            unexpected_computer_control_operations=self.unexpected,
            fixture_process_lifecycle_actions=len(self.lifecycle.actions), lifecycle_actions=list(self.lifecycle.actions),
            retries=0, mutations=self.mutations, identity_checks=self.identity_checks, phases=self.phases,
            textedit_running_at_end=self.lifecycle.textedit_pids())
        return report


def main() -> int:
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    worker.start()
    try:
        report = Step14BFinal(worker, F.FixtureLifecycle()).run()
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("STEP14B_FINAL " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
