"""
Read-only Evie runtime identity check (docs/07_Computer_Control.md Section 34; CC-1a Step 13).

    Evie.app (signed launcher) -> evie-python -m services.computer_control.identity_check -> existing worker

The ONLY program the Evie launcher runs. It starts the existing computer-control worker, which measures its
own identity (process chain, responsible process, code signatures), Accessibility trust (prompt off) and a
harmless AXRole read. It prints one JSON report on stdout, then stops the worker. It never sends `observe`
or `act`: no application is activated, focused, clicked, typed into or scrolled. Takes no arguments.
"""

import json
import os
import sys

from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT
from services.computer_control.worker import ComputerControlWorker, WorkerState


def report(status) -> dict:
    probe = status.probe
    ident = probe.identity if probe else None
    resp = ident.responsible if ident else None
    worker = ident.worker if ident else None
    sig = resp.signature if resp else None
    wsig = worker.signature if worker else None
    return {
        "EVIE_APP_PATH": PRODUCTION_IDENTITY_CONTRACT.bundle_path if PRODUCTION_IDENTITY_CONTRACT else None,
        "EVIE_BUNDLE_ID": resp.bundle_id if resp else None,
        "EVIE_PID": resp.pid if resp else None,
        "EVIE_EXECUTABLE": resp.executable if resp else None,
        "IDENTITY_CHECK_PID": os.getpid(),
        "WORKER_PID": worker.pid if worker else None,
        "WORKER_EXECUTABLE": worker.executable if worker else None,
        "WORKER_PARENT_PID": ident.parent.pid if ident and ident.parent else None,
        "ANCESTRY": [f"{p.pid} {p.name} {p.bundle_id or ''}".strip() for p in ident.ancestry] if ident else None,
        "RESPONSIBLE_PID": resp.pid if resp else None,
        "RESPONSIBLE_APP": resp.executable if resp else None,
        "RESPONSIBLE_BUNDLE_ID": resp.bundle_id if resp else None,
        "RESPONSIBLE_BUNDLE_LOOKUP": resp.bundle_lookup.model_dump() if resp and resp.bundle_lookup else None,
        "SIGNING_IDENTITY": sig.identifier if sig else None,
        "TEAM_ID": sig.team_id if sig else None,
        "SIGNATURE_VALID": sig.valid if sig else None,
        "WORKER_SIGNING_IDENTITY": wsig.identifier if wsig else None,
        "WORKER_TEAM_ID": wsig.team_id if wsig else None,
        "WORKER_SIGNATURE_VALID": wsig.valid if wsig else None,
        "ACCESSIBILITY_TRUST": probe.accessibility.status.value if probe else None,
        "FUNCTIONAL_AX_READ": probe.functional.model_dump(mode="json") if probe else None,
        "WORKER_STATE": status.state.value,
        "WORKER_FAILURE": status.failure,                # why start() failed (e.g. WORKER_EXITED, START_TIMEOUT)
        "WORKER_LAUNCHED_PID": status.pid,
        "PRODUCTION_IDENTITY_VERIFIED": probe.production_identity_verified if probe else False,
        "PRODUCTION_IDENTITY_REASONS": list(probe.production_identity_reasons) if probe else ["NO_PROBE"],
    }


def main() -> int:
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60)
    status = worker.start()
    try:
        print("EVIE_IDENTITY_CHECK " + json.dumps(report(status), indent=1, default=str), flush=True)
    finally:
        stopped = worker.stop()
        print(f"WORKER_STOPPED state={stopped.state.value}", flush=True)
    return 0 if stopped.state is WorkerState.STOPPED else 1


if __name__ == "__main__":
    sys.exit(main())
