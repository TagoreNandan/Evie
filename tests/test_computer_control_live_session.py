"""
LIVE (CC-1a Step 11): permission-identity precondition for end-to-end session tests.
Run explicitly and alone:
    python -m pytest tests/test_computer_control_live_session.py -m live -s -q

Step 11 rule (docs/07 Section 32): live MUTATION through the end-to-end session is allowed only when the
worker permission model has resolved the PRODUCTION Evie runtime identity. Until then this test performs
only the read-only runtime probe (identity, Accessibility trust, a read-only AXRole read), reports it, and
stops before any observation or action. It launches, clicks, types, scrolls and activates nothing.
Step 12 added the production-identity reasons (runtime.assess_production_identity) to the diagnostic.
"""

import json

import pytest

from services.computer_control import macos_probe
from services.computer_control.worker import ComputerControlWorker, WorkerState

pytestmark = pytest.mark.live


def test_live_session_requires_resolved_production_identity(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60)
    status = worker.start()
    try:
        probe = status.probe
        identity = probe.identity if probe else None
        resp = identity.responsible if identity else None
        report = {
            # the process that started the worker (here: the pytest runner standing in for Evie)
            "EVIE_RUNTIME_IDENTITY": identity.parent.model_dump() if identity and identity.parent else None,
            "WORKER_IDENTITY": identity.worker.model_dump() if identity else None,
            "ANCESTRY": [f"{p.pid} {p.name} {p.bundle_id or ''}".strip() for p in identity.ancestry] if identity
            else None,
            "RESPONSIBLE_APP": resp.model_dump() if resp else None,
            "BUNDLE_ID": resp.bundle_id if resp else None,
            "RESPONSIBLE_IS_DEVELOPMENT_HOST": identity.responsible_is_development_host if identity else None,
            "ACCESSIBILITY_TRUST": probe.accessibility.status.value if probe else None,
            "FUNCTIONAL_AX_READ": probe.functional.model_dump(mode="json") if probe else None,
            "WORKER_STATE": status.state.value,
            "PRODUCTION_IDENTITY_VERIFIED": probe.production_identity_verified if probe else None,
            "PRODUCTION_IDENTITY_REASONS": list(probe.production_identity_reasons) if probe else None,
        }
        print("\nSTEP11_LIVE_PRECONDITION " + json.dumps(report, indent=1))
        if not (probe and probe.ready):
            pytest.skip("PRECONDITION worker runtime not ready; no live mutation attempted")
        if not probe.production_identity_verified:
            pytest.skip("PRECONDITION production Evie runtime identity is unresolved in the worker permission "
                        "model; Step 11 live mutation not attempted (docs/07 Sections 32-33)")
        pytest.fail("no production identity contract is declared in CC-1a; this branch must not be reached")
    finally:
        stopped = worker.stop()
        assert stopped.state is WorkerState.STOPPED
