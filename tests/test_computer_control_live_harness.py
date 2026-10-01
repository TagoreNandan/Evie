"""
CC-1a Step 14A: Evie-owned live harness (live_harness.py), FAST tests with a fake worker. No macOS, no network.
The fake worker's `act` fails the test immediately; the harness must stop before any choice or action.
"""

import ast
from pathlib import Path

import pytest

from cc_fixtures import observation
from services.computer_control import live_harness as H
from services.computer_control.models import AppIdentity
from services.computer_control.observer import ObserveResult
from services.computer_control.runtime import AccessibilityStatus as A, FunctionalStatus as F
from services.computer_control.worker import WorkerState, WorkerStatus
from test_computer_control_runtime_identity import DEV_IDENTITY, FAILED_FN, _real_contract_identity, result

FINDER = AppIdentity(bundle_id="com.apple.finder", pid=629, name="Finder")


class FakeWorker:
    def __init__(self, probe=None, state=WorkerState.READY, observe_status="OK", failure=None):
        self.probe, self.state, self.observe_status, self.failure = probe, state, observe_status, failure
        self.observed, self.stopped = [], False

    def start(self):
        return WorkerStatus(state=self.state, pid=702, probe=self.probe, failure=self.failure)

    def observe(self, *, bundle_id=None, **kw):
        self.observed.append(bundle_id)
        if self.observe_status != "OK":
            return ObserveResult(status=self.observe_status, reason="AX_TIMEOUT")
        return ObserveResult(status="OK", observation=observation((), obs_id="obs-h1", app=FINDER, frontmost=FINDER,
                                                                  running=(FINDER,), taken_at=1.0))

    def act(self, *a, **kw):                                  # the mutation sentinel at the worker level
        pytest.fail("Step 14A harness called worker.act")

    def stop(self):
        self.stopped = True
        return WorkerStatus(state=WorkerState.STOPPED)


def verified_probe():
    p = result(_real_contract_identity())
    assert p.production_identity_verified is True
    return p


def run(worker):
    return H.run_harness(worker_factory=lambda: worker), worker


# --- success path: identity verified, one read-only observation, stop before choosing ---

def test_ready_under_verified_identity_with_zero_mutations():
    report, w = run(FakeWorker(verified_probe()))
    assert report["harness_state"] == "READY" and report["failed_stage"] is None
    assert (report["production_identity_verified"], report["worker_ready"], report["observer_ready"],
            report["observation_created"]) == (True, True, True, True)
    assert report["mutation_attempted"] is False and report["mutation_count"] == 0
    assert report["session_constructed"] and report["orchestrator_constructed"]
    assert report["session_state"] == "CHOOSING"               # observation accepted; nothing chosen after it
    assert w.observed == ["com.apple.finder"] and w.stopped and report["worker_stopped"]
    assert report["responsible_bundle_id"] == "com.evie-assistant.evie" and report["team_id"] == "ZB8SA28XVY"
    assert report["observation"]["obs_id"] == "obs-h1"


# --- fail closed ---

def test_development_identity_never_reaches_the_observer():
    report, w = run(FakeWorker(result(DEV_IDENTITY)))
    assert report["harness_state"] == "FAILED" and report["failed_stage"] == "IDENTITY"
    assert "RESPONSIBLE_APP_IS_DEVELOPMENT_HOST" in report["failure_reason"]
    assert w.observed == [] and w.stopped and report["mutation_count"] == 0


@pytest.mark.parametrize("probe_kw, reason", [(dict(ax=A.NOT_TRUSTED), "ACCESSIBILITY_NOT_TRUSTED"),
                                              (dict(fn=FAILED_FN), "FUNCTIONAL_AX_READ_FAILED")])
def test_untrusted_or_failed_ax_read_is_identity_failure(probe_kw, reason):
    report, w = run(FakeWorker(result(_real_contract_identity(), **probe_kw), state=WorkerState.NOT_READY))
    assert report["failed_stage"] == "IDENTITY" and reason in report["failure_reason"] and w.observed == []


def test_worker_start_failure():
    report, w = run(FakeWorker(None, state=WorkerState.FAILED, failure="PROTOCOL_ERROR:x"))
    assert (report["failed_stage"], report["failure_reason"]) == ("WORKER_START", "PROTOCOL_ERROR:x")
    assert w.observed == [] and w.stopped


def test_observation_failure_is_reported_not_retried():
    report, w = run(FakeWorker(verified_probe(), observe_status="ERROR"))
    assert report["failed_stage"] == "OBSERVE" and report["harness_state"] == "FAILED"
    assert w.observed == ["com.apple.finder"] and report["mutation_count"] == 0


# --- the mutation sentinel ---

def test_sentinel_refuses_and_counts():
    s = H.MutationSentinel()
    with pytest.raises(H.MutationAttempted):
        s(object(), "u", frozenset())
    assert s.count == 1


def test_a_sentinel_hit_would_mark_the_harness_failed(monkeypatch):
    """If anything ever routed an action to the act port, the report could not say READY."""
    real = H.Orchestrator

    def orchestrator_that_acts(session, observe, act, **kw):
        try:
            act(object(), "u", frozenset())
        except H.MutationAttempted:
            pass
        return real(session, observe, act, **kw)
    monkeypatch.setattr(H, "Orchestrator", orchestrator_that_acts)
    report, _ = run(FakeWorker(verified_probe()))
    assert report["mutation_attempted"] is True and report["mutation_count"] == 1
    assert report["harness_state"] == "FAILED" and report["failed_stage"] == "MUTATION_SENTINEL"


def test_harness_source_has_no_execution_path():
    tree = ast.parse(Path(H.__file__).read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"act", "run", "run_command", "propose", "authorize", "execute", "record_result", "Popen"}
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.endswith(("executor", "macos_actions", "macos_ax", "chooser_boundary", "fast_path"))
                   for m in imports), imports


# --- launcher: closed modes only ---

LAUNCHER = Path(__file__).resolve().parents[1] / "packaging/macos/EvieLauncher.swift"


def test_launcher_modes_are_a_closed_allow_list():
    src = LAUNCHER.read_text()
    assert src.count('"services.computer_control.') == 3 and src.count('"services.') == 11
    assert '"live_validation_14b": "services.cc_live_validation.step14b"' in src
    assert '"live_validation_14b_final": "services.cc_live_validation.step14b_final"' in src
    assert '"activation_boundary_check": "services.cc_live_validation.activation_boundary"' in src
    assert '"identity_check": "services.computer_control.identity_check"' in src
    assert '"live_harness": "services.computer_control.live_harness"' in src
    assert '"voice_automation_service": "services.computer_control.voice_automation_service"' in src
    assert "requested.count == 1, modes[requested[0]] != nil" in src
    assert 'let program = ["-s", "-B", "-m", modes[mode]!]' in src
    for bad in ("/bin/sh", "bash", "zsh", "system(", "posix_spawn", "executableURL = URL(fileURLWithPath: requested"):
        assert bad not in src
    assert src.count("child.executableURL = python") == 1 and src.count("child.arguments = program") == 1


def test_launcher_is_an_accessory_nsapplication_not_a_foundation_only_agent():
    """Step 14B launcher fix: AppKit main loop as an accessory app; no dispatchMain; nothing else changed."""
    src = LAUNCHER.read_text()
    assert "import AppKit" in src and "dispatchMain()" not in src
    assert src.count("NSApplication.shared.setActivationPolicy(.accessory)") == 1
    assert src.rstrip().endswith("NSApplication.shared.run()")
    assert "NSWindow" not in src and "activate(" not in src and "activateWithOptions" not in src
