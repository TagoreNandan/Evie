"""
CC-1a Step 14B FINAL continuation (step14b_final.py), FAST tests on the simulated world. No macOS.
The fixture file lives in a pytest temp dir; the real scratch fixture is never read or written here.
"""

import pytest

from services.cc_live_validation import fixtures as F, step14b_final as SF
from services.computer_control.models import Op
from services.computer_control.runtime import BundleLookup
from services.computer_control.worker import WorkerState, WorkerStatus
from test_cc_live_validation import TEXTEDIT, FakeLifecycle, FakeWorker, World
from test_computer_control_runtime_identity import _real_contract_identity, result


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "SCRATCH_DIR", tmp_path)
    F.TEXT.path.write_text(F.TEXT_MARKER)


def world_with_open_fixture(**kw):
    w = World()
    w.textedit_running, w.fixture = True, F.TEXT
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def run(world=None, worker_kw=None, worker=None):
    world = world or world_with_open_fixture()
    worker = worker or FakeWorker(world, **(worker_kw or {}))
    lifecycle = FakeLifecycle(world, running_at_start=world.textedit_running)
    return SF.Step14BFinal(worker, lifecycle, sleep=lambda s: None).run(), worker, lifecycle


def test_final_sequence_passes_with_no_fixture_lifecycle():
    report, worker, lc = run()
    assert report["result"] == "PASS", (report["stopped_at"], report["stop_reason"])
    assert worker.acts == ["activate_app"] * 5 + ["set_text", "set_text"]
    assert [p["phase"] for p in report["phases"]] == ["setup_activate_textedit", "activation_textedit_to_finder",
                                                      "activation_finder_to_textedit", "multistep"]
    assert all(p["passed"] and p["final_state"] == "DONE_VERIFIED" for p in report["phases"])
    assert report["computer_control_mutation_count"] == 7 and report["fixture_process_lifecycle_actions"] == 0
    assert lc.actions == [] and report["unexpected_computer_control_operations"] == 0 and report["retries"] == 0
    assert len(report["identity_checks"]) == 8 and all(c["ok"] for c in report["identity_checks"])
    assert [c["where"] for c in report["fixture_checks"]] == ["preflight", "postflight"]
    assert report["phases"][-1]["restoration"]["text_restored_exact"] is True


def test_stage_goals_are_the_required_frontmost_apps():
    goals = {n: p.goal.bundle_id for n, p in SF.FINAL_PLANS.items()}
    assert goals == {"setup_activate_textedit": "com.apple.TextEdit", "activation_textedit_to_finder": "com.apple.finder",
                     "activation_finder_to_textedit": "com.apple.TextEdit", "multistep": "com.apple.TextEdit"}
    ops = [i.op for i in SF.FINAL_PLANS["multistep"].intents]
    assert ops == [Op.ACTIVATE_APP, Op.ACTIVATE_APP, Op.SET_TEXT, Op.SET_TEXT]
    m = SF.FINAL_PLANS["multistep"]
    assert [m.utterance[slice(*i.args["text_span"])] for i in m.intents[2:]] == ["EVIE_MULTISTEP_TEST",
                                                                                 "EVIE_FIXTURE_MARKER"]


def test_changed_fixture_file_stops_before_any_mutation():
    F.TEXT.path.write_text("something else")
    report, worker, lc = run()
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and worker.acts == [] and lc.actions == []


def test_extra_textedit_window_stops_before_any_mutation():
    report, worker, lc = run(world_with_open_fixture(extra_windows=1))
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and worker.acts == [] and lc.actions == []


def test_textedit_not_running_stops_and_launches_nothing():
    w = World()
    report, worker, lc = run(w)
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and worker.acts == [] and lc.actions == []


def test_setup_activation_failure_stops_everything():
    report, worker, lc = run(worker_kw={"act_outcomes": {1: "FAILED"}})
    assert report["result"] == "FAILED" and report["stopped_at"] == "PHASE_SETUP_ACTIVATE_TEXTEDIT_FAILED"
    assert worker.acts == ["activate_app"] and lc.actions == []


def test_second_activation_failure_stops_without_retry():
    report, worker, _ = run(worker_kw={"act_outcomes": {2: "FAILED"}})
    assert worker.acts == ["activate_app", "activate_app"]
    assert report["stopped_at"] == "PHASE_ACTIVATION_TEXTEDIT_TO_FINDER_FAILED"


def test_bundle_lookup_not_found_stops_before_the_next_mutation():
    world = world_with_open_fixture()
    worker = FakeWorker(world)
    real_probe = worker.probe

    def probe():
        status = real_probe()
        if worker.probes >= 3:                                  # start, setup ok; lookup fails before activation 1
            ident = _real_contract_identity()
            ident = ident.model_copy(update={"responsible": ident.responsible.model_copy(
                update={"bundle_id": None, "bundle_lookup": BundleLookup(status="NO_RUNNING_APPLICATION")})})
            status = WorkerStatus(state=WorkerState.READY, pid=702, probe=result(ident))
            worker._status = status
        return status
    worker.probe = probe
    report, worker, _ = run(world, worker=worker)
    assert worker.acts == ["activate_app"] and report["result"] == "PARTIAL_STOPPED"
    last = report["identity_checks"][-1]
    assert last["ok"] is False and last["bundle_lookup"]["status"] == "NO_RUNNING_APPLICATION"
    assert "RESPONSIBLE_NO_RUNNING_APPLICATION" in last["reasons"]


def test_final_module_has_no_fixture_lifecycle_calls():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(SF.__file__).read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"open_in_background", "terminate_textedit", "write", "_prepare", "_end_textedit"}
