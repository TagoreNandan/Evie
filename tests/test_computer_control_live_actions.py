"""
LIVE (CC-1a Step 4): the first reversible UI actions, through the real worker. Run ONE test at a time:
    python -m pytest "tests/test_computer_control_live_actions.py::test_live_set_text" -m live -s -q

Fixture setup is explicit and scratch-only: each test writes its own file into pytest's temporary
directory and opens it in TextEdit in the BACKGROUND (`open -g`: TextEdit is not activated). Actions go
through the real pure session (propose -> gate -> authorize) and the worker (fresh-observation check,
gate re-check, live re-validation, exactly one action, verification). No retries. Cleanup is itself a
validated action (text: replace_all from the utterance; scroll/press: the reverse action). If a
precondition cannot be met without additional UI changes, the test is skipped with the reason.
Fixture windows stay open afterwards (closing is not an enabled action).
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from services.computer_control import macos_probe
from services.computer_control.gate import derive_allowed_apps
from services.computer_control.models import normalize
from services.computer_control.session import ComputerControlSession, SessionState
from services.computer_control.worker import ComputerControlWorker, WorkerState

pytestmark = pytest.mark.live
TEXTEDIT = "com.apple.TextEdit"


class RealClock:
    def now(self):
        return time.monotonic()


def _doc_hash(path: Path) -> str:
    return hashlib.sha256(str(Path(os.path.realpath(path)).as_uri()).encode()).hexdigest()[:16]


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")
    w = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    status = w.start()
    if status.state is not WorkerState.READY:
        w.stop()
        pytest.fail(f"PRECONDITION worker not ready: {status.state.value} {status.failure}")
    yield w
    assert w.stop().state is WorkerState.STOPPED


def _open_background(path: Path) -> None:
    subprocess.run(["open", "-g", "-a", "TextEdit", str(path)], check=True, shell=False, timeout=15)


def _observe_fixture(worker, path: Path, want_main=True, tries=6):
    """Observe TextEdit until the fixture document is visible (read-only polling, bounded)."""
    want = _doc_hash(path)
    last = None
    for _ in range(tries):
        r = worker.observe(bundle_id=TEXTEDIT)
        last = r
        if r.status == "OK" and r.observation is not None:
            obs = r.observation
            for i, w in enumerate(obs.windows):
                if w.document_hash == want and (w.is_main or not want_main):
                    return obs, i
        time.sleep(0.5)
    pytest.skip(f"PRECONDITION fixture window not observable: {last.status if last else None} "
                f"{last.reason if last else None}")


def _preconditions(obs, report):
    report["preconditions"] = {
        "observation_settled": obs.settle.status.value, "complete": obs.complete, "evie_self": obs.evie_self.value,
        "frontmost": obs.frontmost.bundle_id if obs.frontmost else None,
        "frontmost_sources": list(obs.frontmost_sources), "frontmost_diagnostic": obs.frontmost_diagnostic,
    }
    ok = obs.settle.status.value == "settled" and obs.complete and obs.evie_self.value == "not_excluded" \
        and obs.frontmost is not None
    report["preconditions"]["all_met"] = ok
    return ok


def _session(worker, utterance, obs):
    s = ComputerControlSession(f"live-{uuid.uuid4().hex[:8]}", utterance,
                               derive_allowed_apps(utterance, obs.frontmost, obs.running_apps), clock=RealClock())
    s.start(worker.status.probe.to_permission_status())
    return s


def _act(worker, session, obs, decision):
    """One action through session -> worker. Returns (result, request) or (None, reason) if not dispatched."""
    assert session.observe(obs) is SessionState.CHOOSING, session.stop_reason
    request = session.propose(decision, obs.obs_id)
    if request is None:
        return None, session.stop_reason
    if not session.authorize(request):
        return None, session.stop_reason
    result = worker.act(request, utterance=session.utterance, allowed_apps=session.allowed_apps)
    session.record_result(result)
    return result, request


def _summ(result, request):
    if result is None:
        return {"dispatched": False, "reason": request}
    return {"dispatched": True, "status": result.status.value, "execution": result.execution.value,
            "verification": result.verification.value, "mechanism": result.mechanism, "ax_error": result.ax_error,
            "latency_ms": result.latency_ms, "reason": result.reason, "noop": result.noop,
            "target": {"role": request.target.target.role, "subrole": request.target.target.subrole,
                       "label": request.target.target.label if request.target.target.role != "AXTextArea" else None,
                       "window_index": request.target.target.window_index},
            "evidence": result.evidence}


def _print(name, report):
    print(f"\nLIVE_ACTION_REPORT[{name}] " + json.dumps(report, indent=1, default=str))


def _index(obs, window, role, label=None):
    hits = [t.index for t in obs.targets if t.window_index == window and t.role == role
            and (label is None or normalize(t.label) == label)]
    return hits[0] if len(hits) == 1 else None


# --- A. focus ---

def test_live_focus(worker, tmp_path):
    report = {}
    target_doc, other_doc = tmp_path / "evie_focus_target.txt", tmp_path / "evie_focus_other.txt"
    target_doc.write_text("EVIE_FOCUS_TARGET")
    other_doc.write_text("EVIE_FOCUS_OTHER")
    _open_background(target_doc)
    _observe_fixture(worker, target_doc)
    _open_background(other_doc)                       # the target document is no longer main/focused
    obs, _ = _observe_fixture(worker, other_doc)
    want = _doc_hash(target_doc)
    target_windows = [i for i, w in enumerate(obs.windows) if w.document_hash == want and not w.is_main]
    report["windows_observed"] = len(obs.windows)
    if not _preconditions(obs, report) or not target_windows:
        _print("focus", report)
        pytest.skip("PRECONDITION the non-main target window is not exposed by AX (or preconditions unmet); "
                    "focus not attempted")
    idx = _index(obs, target_windows[0], "AXTextArea")
    report["target_focused_before"] = obs.targets[idx].focused if idx is not None else None
    utterance = "in TextEdit focus the other document"
    result, request = _act(worker, _session(worker, utterance, obs), obs,
                           {"status": "ACTION", "op": "focus", "target_index": idx})
    report["action"] = _summ(result, request)
    report["cleanup"] = "none needed (focus inside TextEdit; fixture files untouched)"
    _print("focus", report)
    assert result is not None and result.status.value == "SUCCESS"


# --- B. set_text + restore ---

def test_live_set_text(worker, tmp_path):
    report = {}
    doc = tmp_path / "evie_set_text.txt"
    doc.write_text("EVIE_FIXTURE_MARKER")
    before_disk = doc.read_bytes()
    _open_background(doc)
    obs, win = _observe_fixture(worker, doc)
    if not _preconditions(obs, report):
        _print("set_text", report)
        pytest.skip("PRECONDITION not met; nothing attempted")
    idx = _index(obs, win, "AXTextArea")
    utterance = "in TextEdit type EVIE_STEP4_TEST then restore EVIE_FIXTURE_MARKER"
    insert = [utterance.index("EVIE_STEP4_TEST"), utterance.index("EVIE_STEP4_TEST") + len("EVIE_STEP4_TEST")]
    restore = [utterance.index("EVIE_FIXTURE_MARKER"), len(utterance)]
    session = _session(worker, utterance, obs)
    result, request = _act(worker, session, obs, {"status": "ACTION", "op": "set_text", "target_index": idx,
                                                  "args": {"text_span": insert, "mode": "insert_at_cursor"}})
    report["action"] = _summ(result, request)
    obs2, win2 = _observe_fixture(worker, doc)
    idx2 = _index(obs2, win2, "AXTextArea")
    cleanup, creq = _act(worker, session, obs2, {"status": "ACTION", "op": "set_text", "target_index": idx2,
                                                 "args": {"text_span": restore, "mode": "replace_all"}})
    report["cleanup"] = _summ(cleanup, creq)
    report["disk_unchanged_immediately"] = doc.read_bytes() == before_disk
    _print("set_text", report)
    assert result is not None and result.status.value == "SUCCESS"
    assert cleanup is not None and cleanup.status.value == "SUCCESS"


# --- C. scroll + reverse ---

def test_live_scroll(worker, tmp_path):
    report = {}
    doc = tmp_path / "evie_scroll.txt"
    doc.write_text("".join(f"EVIE scroll fixture line {i:04d}\n" for i in range(600)))
    _open_background(doc)
    obs, win = _observe_fixture(worker, doc)
    if not _preconditions(obs, report):
        _print("scroll", report)
        pytest.skip("PRECONDITION not met; nothing attempted")
    utterance = "in TextEdit scroll down then scroll up"
    session = _session(worker, utterance, obs)
    down, dreq = _act(worker, session, obs, {"status": "ACTION", "op": "scroll",
                                             "target_index": _index(obs, win, "AXScrollArea"),
                                             "args": {"direction": "down"}})
    report["action"] = _summ(down, dreq)
    obs2, win2 = _observe_fixture(worker, doc)
    up, ureq = _act(worker, session, obs2, {"status": "ACTION", "op": "scroll",
                                            "target_index": _index(obs2, win2, "AXScrollArea"),
                                            "args": {"direction": "up"}})
    report["reverse"] = _summ(up, ureq)
    if down is not None and up is not None:
        report["restoration_measured"] = {"visible_before": down.evidence.get("visible_before"),
                                          "visible_after_reverse": up.evidence.get("visible_after")}
    _print("scroll", report)
    assert down is not None and down.status.value == "SUCCESS"
    assert up is not None and up.status.value == "SUCCESS"


# --- D. press (TextEdit alignment segment only) + reverse ---

def test_live_press_alignment(worker, tmp_path):
    report = {}
    doc = tmp_path / "evie_press.rtf"
    doc.write_text("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Helvetica;}}\\f0\\fs24\\pard\\ql EVIE_PRESS_FIXTURE\\par}\n")
    _open_background(doc)
    obs, win = _observe_fixture(worker, doc)
    if not _preconditions(obs, report):
        _print("press", report)
        pytest.skip("PRECONDITION not met; nothing attempted")
    center, left = _index(obs, win, "AXCheckBox", "align center"), _index(obs, win, "AXCheckBox", "align left")
    report["segments_found"] = {"align center": center, "align left": left}
    if center is None or left is None:
        _print("press", report)
        pytest.skip("PRECONDITION alignment segments not uniquely observable; nothing attempted")
    utterance = "in TextEdit press align center then press align left"
    session = _session(worker, utterance, obs)
    forward, freq = _act(worker, session, obs, {"status": "ACTION", "op": "press", "target_index": center})
    report["action"] = _summ(forward, freq)
    obs2, win2 = _observe_fixture(worker, doc)
    back, breq = _act(worker, session, obs2, {"status": "ACTION", "op": "press",
                                              "target_index": _index(obs2, win2, "AXCheckBox", "align left")})
    report["reverse"] = _summ(back, breq)
    report["note"] = "TextEdit may autosave this scratch RTF later (EVIDENCE.md lesson 8); file is in pytest tmp"
    _print("press", report)
    assert forward is not None and forward.status.value == "SUCCESS"
    assert back is not None and back.status.value == "SUCCESS"
