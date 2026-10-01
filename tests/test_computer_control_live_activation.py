"""
LIVE (CC-1a Step 5): activate_app for ALREADY-RUNNING allowed apps, through the real worker.
Run explicitly and alone:
    python -m pytest tests/test_computer_control_live_activation.py -m live -s -q

A = the app frontmost at the start (cross-checked; must not be restricted/sensitive/Evie).
B = Finder (or TextEdit if Finder is A); both must already be running - nothing is launched.
Sequence: activate B once -> verify -> fresh observation of B -> activate A once -> verify -> stop.
Only Finder/TextEdit are observed (read-only); no documents are opened, nothing is typed or clicked,
no files are touched, and no shell/open command is used. Failed steps are never retried.
"""

import json
import time
import uuid

import pytest

from services.computer_control import macos_probe
from services.computer_control.gate import DEFAULT_POLICY, derive_allowed_apps
from services.computer_control.session import ComputerControlSession, SessionState
from services.computer_control.worker import ComputerControlWorker, WorkerState

pytestmark = pytest.mark.live
FINDER, TEXTEDIT = "com.apple.finder", "com.apple.TextEdit"


class RealClock:
    def now(self):
        return time.monotonic()


def _front(obs):
    return {"frontmost": obs.frontmost.bundle_id if obs.frontmost else None,
            "sources": list(obs.frontmost_sources), "diagnostic": obs.frontmost_diagnostic}


def _summ(result):
    if result is None:
        return None
    return {"status": result.status.value, "execution": result.execution.value,
            "verification": result.verification.value, "reason": result.reason, "latency_ms": result.latency_ms,
            "mechanism": result.mechanism, "evidence": result.evidence}


def test_live_activate_app(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")
    report = {}
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    status = worker.start()
    try:
        assert status.state is WorkerState.READY, (status.state, status.failure)
        first = worker.observe(bundle_id=FINDER)
        assert first.status == "OK", first.reason
        obs = first.observation
        report["start"] = _front(obs)
        a = obs.frontmost
        running = {x.bundle_id: x for x in obs.running_apps}
        if a is None:
            pytest.skip(f"PRECONDITION frontmost unresolved: {obs.frontmost_diagnostic}; nothing activated")
        if a.bundle_id in DEFAULT_POLICY.restricted_bundles | DEFAULT_POLICY.no_observe_bundles:
            pytest.skip(f"PRECONDITION original frontmost {a.bundle_id} is restricted; nothing activated")
        b_id = TEXTEDIT if a.bundle_id == FINDER else FINDER
        if b_id not in running:
            pytest.skip(f"PRECONDITION {b_id} not running; not launching it")
        b = running[b_id]
        utterance = f"switch to {b.name} then back to {a.name}"
        allowed = derive_allowed_apps(utterance, a, obs.running_apps)
        report.update(A=a.model_dump(), B=b.model_dump(), allowed_apps=sorted(allowed))
        if not {a.bundle_id, b.bundle_id} <= allowed:
            pytest.skip("PRECONDITION both apps are not in the utterance-derived allowed scope; nothing activated")

        session = ComputerControlSession(f"live-{uuid.uuid4().hex[:8]}", utterance, allowed, clock=RealClock())
        session.start(status.probe.to_permission_status())

        # 1) activate B once
        assert session.observe(obs) is SessionState.CHOOSING, session.stop_reason
        req1 = session.propose({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": b.bundle_id}},
                               obs.obs_id)
        assert req1 is not None and session.authorize(req1), session.stop_reason
        r1 = worker.act(req1, utterance=utterance, allowed_apps=allowed)
        session.record_result(r1)
        report["activate_B"] = _summ(r1)

        # 2) fresh observation of B, then activate A once (returns you to where you were)
        second = worker.observe(bundle_id=b.bundle_id)
        report["after_B_observation"] = {"status": second.status, **(_front(second.observation)
                                                                     if second.observation else {})}
        r2 = None
        if r1.status.value == "SUCCESS" and second.status == "OK" and \
                session.observe(second.observation) is SessionState.CHOOSING:
            req2 = session.propose({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": a.bundle_id}},
                                   second.observation.obs_id)
            if req2 is not None and session.authorize(req2):
                r2 = worker.act(req2, utterance=utterance, allowed_apps=allowed)
                session.record_result(r2)
        report["activate_A_reversal"] = _summ(r2) if r2 else {"dispatched": False, "session": session.stop_reason}
        print("\nLIVE_ACTIVATION_REPORT " + json.dumps(report, indent=1, default=str))
        assert r1.status.value == "SUCCESS"
        assert r2 is not None and r2.status.value == "SUCCESS"
    finally:
        assert worker.stop().state is WorkerState.STOPPED
