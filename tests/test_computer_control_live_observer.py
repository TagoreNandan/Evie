"""
LIVE: one read-only observation through the real computer-control worker (docs/07 Section 7).

Run explicitly and alone:
    python -m pytest tests/test_computer_control_live_observer.py -m live -s -q

Observes an ALREADY-RUNNING safe app: the bundle named by EVIE_LIVE_OBSERVE_BUNDLE if set, otherwise
TextEdit if running, otherwise Finder. It launches nothing,
activates nothing, changes no focus, performs no action and writes nothing. The printed report
contains roles, counts and flags; label text is printed only for standard controls, never document
or file content, and no values.
"""

import json
import os
from collections import Counter

import pytest

from services.computer_control import macos_probe
from services.computer_control.worker import ComputerControlWorker, WorkerState

pytestmark = pytest.mark.live

CONTROL_ROLES = {"AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXComboBox"}


def test_live_worker_observation(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")        # inherited by the worker process
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90)
    status = worker.start()
    try:
        assert status.state is WorkerState.READY, (status.state, status.failure,
                                                   status.probe.model_dump() if status.probe else None)
        explicit = os.environ.get("EVIE_LIVE_OBSERVE_BUNDLE")
        if explicit:
            result = worker.observe(bundle_id=explicit)          # exactly one observer pass
        else:
            result = worker.observe(bundle_id="com.apple.TextEdit")
            if result.status == "UNAVAILABLE" and "APP_NOT_RUNNING" in (result.reason or ""):
                result = worker.observe(bundle_id="com.apple.finder")
        probe, obs = status.probe, result.observation
        ident = probe.identity
        report = {
            "worker": ident.worker.model_dump(), "parent": ident.parent.model_dump() if ident.parent else None,
            "responsible": ident.responsible.model_dump() if ident.responsible else None,
            "accessibility": probe.accessibility.status.value, "runtime_ready": probe.ready,
            "development_identity_verified": probe.ready and ident.responsible_is_development_host is True,
            "production_evie_identity_verified": probe.production_identity_verified,
            "observe_status": result.status, "observe_reason": result.reason, "round_trip_ms": result.duration_ms,
        }
        if obs is not None:
            report.update({
                "obs_id": obs.obs_id, "target_app": obs.app.model_dump(), "frontmost": obs.frontmost.bundle_id,
                "settle": obs.settle.model_dump(mode="json"), "counts": obs.counts.model_dump(),
                "caps_hit": list(obs.caps_hit), "complete": obs.complete, "restricted": obs.restricted,
                "evie_self": obs.evie_self.value, "menu_bar_pruned": obs.menu_bar_pruned,
                "excluded": obs.excluded, "web_area_present": obs.web_area_present,
                "observation_duration_ms": obs.duration_ms,
                "ax_errors": [e.model_dump() for e in obs.ax_errors[:10]],
                "windows": [{"role": w.role, "subrole": w.subrole, "is_main": w.is_main, "is_focused": w.is_focused,
                             "has_document": w.document_hash is not None} for w in obs.windows],
                "targets_by_role": dict(Counter(t.role for t in obs.targets).most_common()),
                "target_sample": [{"i": t.index, "role": t.role, "subrole": t.subrole, "window": t.window_index,
                                   "label": t.label if t.role in CONTROL_ROLES else ("<present>" if t.label else None),
                                   "has_value": t.value_summary is not None, "actions": list(t.actions)[:3],
                                   "settable": list(t.settable), "secure": t.is_secure}
                                  for t in obs.targets[:15]],
            })
        print("\nCOMPUTER_CONTROL_LIVE_OBSERVER_REPORT " + json.dumps(report, indent=1))
        assert result.status in ("OK", "RESTRICTED"), result.reason
        assert all(t.obs_id == obs.obs_id for t in obs.targets)
        assert probe.production_identity_verified is False
    finally:
        stopped = worker.stop()
    assert stopped.state is WorkerState.STOPPED
