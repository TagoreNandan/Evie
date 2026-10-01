"""
LIVE: the computer-control worker's real runtime identity and Accessibility probe (docs/07 Section 14).

Run explicitly and alone:
    python -m pytest tests/test_computer_control_live_runtime.py -m live -s -q

Read-only: starts the worker, which reads its own identity, checks AXIsProcessTrustedWithOptions with
the prompt OFF, and reads AXRole from an ALREADY-RUNNING Finder/TextEdit app element. It launches
nothing, clicks/types nothing, changes no focus, and stores nothing. The result describes the worker
runtime it ran in - not production Evie (production_identity_verified is always False).
"""

import json

import pytest

from services.computer_control import macos_probe
from services.computer_control.worker import ComputerControlWorker, WorkerState

pytestmark = pytest.mark.live


def test_live_worker_runtime_probe(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")      # inherited by the worker process
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60)
    status = worker.start()
    try:
        report = {"state": status.state.value, "failure": status.failure, "worker_pid": status.pid,
                  "probe": json.loads(status.probe.model_dump_json()) if status.probe else None}
        print("\nCOMPUTER_CONTROL_LIVE_RUNTIME_REPORT " + json.dumps(report, indent=1))
        assert status.state in (WorkerState.READY, WorkerState.NOT_READY), status.failure
        assert status.probe.identity.worker.pid == status.pid
        assert status.probe.production_identity_verified is False
    finally:
        stopped = worker.stop()
    assert stopped.state is WorkerState.STOPPED
