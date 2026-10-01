"""
Real-Evie activation-boundary diagnostic (activation_boundary.py), FAST tests with a fake AppKit port and worker.
No macOS, no activation.
"""

import ast
from pathlib import Path

import pytest

from services.cc_live_validation import activation_boundary as AB
from services.computer_control.runtime import BundleLookup
from services.computer_control.worker import WorkerState, WorkerStatus
from test_computer_control_runtime_identity import _real_contract_identity, result

EVIE = 700


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Port:
    def __init__(self, drop_after_activation=False, target=True, target_exe=AB.TARGET_EXECUTABLE,
                 front=("com.google.antigravity-ide",), responsible_of_self=EVIE, pre_status="FOUND"):
        self.drop, self.target, self.target_exe = drop_after_activation, target, target_exe
        self.front, self.self_resp, self.pre_status = list(front), responsible_of_self, pre_status
        self.activated, self.activations = False, []

    def pump(self, s):
        pass

    def lookup(self, pid):
        status = "NO_RUNNING_APPLICATION" if (self.activated and self.drop) else self.pre_status
        return ("com.evie-assistant.evie" if status == "FOUND" else None), status

    def responsible(self, pid):
        return self.self_resp if pid != EVIE else EVIE

    def executable(self, pid):
        return self.target_exe if pid == 900 else "/x/Evie.app/Contents/MacOS/Evie"

    def scratch_target(self):
        return (900, "APP") if self.target else None

    def frontmost(self):
        return AB.TARGET_BUNDLE if self.activated else self.front[0]

    def activate(self, app):
        self.activations.append(app)
        self.activated = True
        return True


class Worker:
    def __init__(self, lookup_after="FOUND"):
        self.lookup_after, self.acts = lookup_after, []
        self._status = WorkerStatus(state=WorkerState.READY, pid=702, probe=result(self._ident("FOUND")))

    @staticmethod
    def _ident(status):
        ident = _real_contract_identity()
        return ident.model_copy(update={"responsible": ident.responsible.model_copy(update={
            "pid": EVIE, "bundle_id": "com.evie-assistant.evie" if status == "FOUND" else None,
            "bundle_lookup": BundleLookup(status=status)})})

    @property
    def status(self):
        return self._status

    def probe(self):
        self._status = WorkerStatus(state=WorkerState.READY, pid=702, probe=result(self._ident(self.lookup_after)))
        return self._status

    def act(self, *a, **k):
        pytest.fail("the diagnostic must never use the worker/executor act path")


def run(port=None, worker=None):
    clock = Clock()
    return AB.run_check(worker or Worker(), port or Port(), clock=clock, sleep=clock.sleep)


def test_record_kept_for_the_whole_window_is_established():
    port = Port()
    r = run(port)
    assert r["status"] == "ESTABLISHED" and port.activations == ["APP"]
    assert r["pre"]["samples"] == AB.PRE_SAMPLES and r["post"]["duration_s"] >= AB.POST_MIN_S
    assert r["post"]["samples"] >= 160 and r["post"]["states"] == ["FOUND"]
    assert r["post"]["no_running_application_occurred"] is False and r["activation"]["transition_confirmed"]
    assert r["identity_after"]["verified"] and r["production_mutations"] == 0 and r["ax_mutations"] == 0


def test_record_lost_after_activation_is_blocked_with_the_first_failure():
    r = run(Port(drop_after_activation=True))
    assert r["status"] == "BLOCKED" and r["reason"] == "RECORD_LOST_AFTER_ACTIVATION"
    assert r["post"]["no_running_application_occurred"] is True
    assert r["post"]["first_failure"]["lookup"] == "NO_RUNNING_APPLICATION" and r["post"]["first_failure"]["t"] == 0


def test_identity_after_not_verified_is_blocked():
    r = run(worker=Worker(lookup_after="NO_RUNNING_APPLICATION"))
    assert r["status"] == "BLOCKED" and r["reason"].startswith("IDENTITY_AFTER_NOT_VERIFIED")


@pytest.mark.parametrize("port, reason", [
    (Port(target=False), "SCRATCH_TARGET_NOT_RUNNING (nothing is launched)"),
    (Port(target_exe="/Applications/Safari.app/Contents/MacOS/Safari"), "SCRATCH_TARGET_IDENTITY_MISMATCH"),
    (Port(front=(AB.TARGET_BUNDLE,)), "SCRATCH_TARGET_ALREADY_FRONTMOST (no transition possible)"),
    (Port(responsible_of_self=2494), "THIS_PROCESS_NOT_ATTRIBUTED_TO_EVIE"),
    (Port(pre_status="NO_RUNNING_APPLICATION"), "PRE_TRANSITION_LOOKUP_NOT_FOUND"),
])
def test_preconditions_block_before_any_activation(port, reason):
    r = run(port)
    assert r["status"] == "BLOCKED" and r["reason"] == reason and port.activations == []
    assert r["activation_attempted"] is False


def test_unverified_identity_blocks_before_sampling():
    w = Worker()
    w._status = WorkerStatus(state=WorkerState.READY, pid=702, probe=result(Worker._ident("NO_RUNNING_APPLICATION")))
    port = Port()
    r = run(port, w)
    assert r["reason"].startswith("IDENTITY_NOT_VERIFIED") and port.activations == []


def test_target_is_fixed_and_never_a_user_app():
    assert AB.TARGET_BUNDLE == "com.evie-assistant.scratch.activation-target"
    assert AB.TARGET_BUNDLE not in AB.NEVER_ACTIVATE
    assert AB.TARGET_EXECUTABLE.endswith("/Caches/com.evie-assistant.evie/activation-boundary/"
                                         "EvieScratchTarget.app/Contents/MacOS/EvieScratchTarget")
    assert {"com.apple.finder", "com.apple.TextEdit", "com.apple.Safari", "com.apple.Terminal"} <= AB.NEVER_ACTIVATE
    assert AB.POST_MIN_S >= 16.0


def test_module_has_no_executor_worker_act_argv_or_ax_mutation():
    src = Path(AB.__file__).read_text()
    tree = ast.parse(src)
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"act", "run_command", "propose", "authorize", "Popen", "run"}
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.endswith(("executor", "macos_actions", "orchestrator", "session")) for m in mods)
    for bad in ("sys.argv", "AXUIElementSetAttributeValue", "AXUIElementPerformAction", "subprocess",
                "openApplication", "launchApplication"):
        assert bad not in src
    assert src.count("activateWithOptions_(") == 1
