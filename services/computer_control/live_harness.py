"""
Evie-owned live harness, PRE-MUTATION wiring check (docs/07_Computer_Control.md Section 35; CC-1a Step 14A).

    Evie.app launcher (mode "live_harness") -> evie-python -m services.computer_control.live_harness -> worker

Proves that the Step 11 live infrastructure can be constructed under the signed Evie identity, then stops
BEFORE any choice or action:
  1. start the existing worker (its probe measures identity, signatures, Accessibility, AXRole read);
  2. fail closed unless production_identity_verified is true;
  3. build the Step 11 session and orchestrator exactly as the live tests do - with an act port that REFUSES
     and counts any call (the mutation sentinel);
  4. one read-only observation of Finder through the worker's observe command (the Step 11 observe port) and
     hand it to the session;
  5. cancel the session (nothing is chosen, gated or executed), stop the worker, print STEP14A_HARNESS JSON.
Takes no arguments. Never calls worker.act, Orchestrator.run / run_command, or any executor.
"""

import json
import os
import sys
import time
import uuid
from typing import Any, Callable, Dict, Optional

from services.computer_control.models import ActionResult, PermissionStatus
from services.computer_control.orchestrator import Orchestrator
from services.computer_control.session import ComputerControlSession, SessionState
from services.computer_control.worker import ComputerControlWorker, WorkerState

OBSERVE_BUNDLE = "com.apple.finder"          # always running, read-only observation only
UTTERANCE = "step 14a harness: observe only"


class MutationAttempted(RuntimeError):
    """Raised by the sentinel act port. Step 14A must never reach it."""


class MutationSentinel:
    def __init__(self):
        self.count = 0

    def __call__(self, request, utterance, allowed_apps) -> ActionResult:
        self.count += 1
        raise MutationAttempted(f"Step 14A harness refuses to execute {getattr(request, 'op', '?')}")


class _Clock:
    def now(self) -> float:
        return time.monotonic()


def run_harness(worker_factory: Callable[[], Any] = lambda: ComputerControlWorker(
                    start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90)) -> Dict[str, Any]:
    sentinel = MutationSentinel()
    report: Dict[str, Any] = {
        "harness_pid": os.getpid(), "evie_pid": None, "worker_pid": None, "responsible_pid": None,
        "responsible_bundle_id": None, "responsible_bundle_lookup": None, "responsible_executable": None,
        "team_id": None, "signature_valid": None,
        "worker_signature_valid": None, "ancestry": None, "accessibility_trust": None, "functional_ax_read": None,
        "production_identity_verified": False, "production_identity_reasons": None, "worker_ready": False,
        "worker_failure": None, "session_constructed": False, "orchestrator_constructed": False,
        "observer_ready": False, "observation_created": False, "observation": None, "session_state": None,
        "mutation_attempted": False, "mutation_count": 0, "harness_state": "FAILED", "failed_stage": None,
        "failure_reason": None,
    }

    def failed(stage: str, reason: Optional[str]) -> Dict[str, Any]:
        report.update(failed_stage=stage, failure_reason=(reason or "UNKNOWN")[:200], harness_state="FAILED")
        return report

    worker = worker_factory()
    status = worker.start()
    try:
        probe = status.probe
        report.update(worker_pid=status.pid, worker_failure=status.failure,
                      worker_ready=status.state is WorkerState.READY and bool(probe and probe.ready))
        if probe is not None:
            ident = probe.identity
            resp = ident.responsible
            report.update(
                evie_pid=resp.pid if resp else None, responsible_pid=resp.pid if resp else None,
                responsible_bundle_id=resp.bundle_id if resp else None,
                responsible_bundle_lookup=resp.bundle_lookup.model_dump() if resp and resp.bundle_lookup else None,
                responsible_executable=resp.executable if resp else None,
                team_id=resp.signature.team_id if resp and resp.signature else None,
                signature_valid=resp.signature.valid if resp and resp.signature else None,
                worker_signature_valid=ident.worker.signature.valid if ident.worker.signature else None,
                ancestry=[f"{p.pid} {p.name} {p.bundle_id or ''}".strip() for p in ident.ancestry],
                accessibility_trust=probe.accessibility.status.value,
                functional_ax_read=probe.functional.status.value,
                production_identity_verified=probe.production_identity_verified,
                production_identity_reasons=list(probe.production_identity_reasons))
        if probe is None:
            return failed("WORKER_START", status.failure)
        if not probe.production_identity_verified:                     # fail closed: not Evie, stop here
            return failed("IDENTITY", ",".join(probe.production_identity_reasons))
        if not report["worker_ready"]:
            return failed("WORKER_READY", status.failure or status.state.value)

        # The Step 11 live setup: session + orchestrator with the worker's observe port and a REFUSING act port.
        session = ComputerControlSession(f"step14a-{uuid.uuid4().hex[:8]}", UTTERANCE,
                                         frozenset({OBSERVE_BUNDLE}), clock=_Clock())
        report["session_constructed"] = True
        observe = lambda bundle: worker.observe(bundle_id=bundle)
        Orchestrator(session, observe, sentinel)                      # constructed; never run in Step 14A
        report["orchestrator_constructed"] = True
        permission: PermissionStatus = probe.to_permission_status()
        if session.start(permission) is not SessionState.OBSERVING:
            return failed("SESSION_START", session.stop_reason)

        result = observe(OBSERVE_BUNDLE)                               # read-only
        report["observer_ready"] = result.status == "OK"
        obs = result.observation
        if result.status != "OK" or obs is None:
            return failed("OBSERVE", f"{result.status}:{result.reason}")
        report["observation_created"] = True
        report["observation"] = {
            "obs_id": obs.obs_id, "app": obs.app.bundle_id,
            "frontmost": obs.frontmost.bundle_id if obs.frontmost else None, "windows": len(obs.windows),
            "targets": len(obs.targets), "total_nodes": obs.counts.total_nodes if obs.counts else None, "settle": obs.settle.status.value,
            "restricted": obs.restricted, "degraded": obs.degraded, "duration_ms": obs.duration_ms}
        state = session.observe(obs)
        report["session_state"] = state.value
        session.cancel("STEP14A_STOP_BEFORE_CHOOSE")                  # nothing is chosen, gated or executed
        report["harness_state"] = "READY"
        return report
    finally:
        report.update(mutation_attempted=sentinel.count > 0, mutation_count=sentinel.count)
        if sentinel.count:
            report.update(harness_state="FAILED", failed_stage="MUTATION_SENTINEL")
        stopped = worker.stop()
        report["worker_stopped"] = stopped.state is WorkerState.STOPPED


def main() -> int:
    report = run_harness()
    print("STEP14A_HARNESS " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["harness_state"] == "READY" and report.get("worker_stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
