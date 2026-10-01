"""
LIVE (CC-1a Step 14B FINAL continuation, option B): the LAST authorised live attempt. Run explicitly, alone, ONCE:
    python -m pytest tests/test_computer_control_live_step14b_final.py -m live -s -q

Preconditions are checked BEFORE Evie.app is launched (a failed precondition consumes nothing): the app is
built, exactly one TextEdit process is running (left open by the first run with the scratch text fixture),
Finder is running, and the fixture file is exactly EVIE_FIXTURE_MARKER. Evie.app is launched once in the
closed mode `live_validation_14b_final` (services.cc_live_validation.step14b_final): setup activate TextEdit
-> Finder -> TextEdit -> 4-action multi-step. No fixture lifecycle action. This test never retries.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from services.computer_control.macos_probe import _LibProc, _responsible_pid
from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT

pytestmark = pytest.mark.live
EVIE_PATTERN = "Applications/Evie.app/Contents/MacOS/"


def _pgrep(*args):
    out = subprocess.run(["/usr/bin/pgrep", *args], capture_output=True, text=True, shell=False)
    return [int(p) for p in out.stdout.split()]


def test_live_step14b_final(tmp_path):
    app = Path(CONTRACT.bundle_path)
    if not (app / "Contents/Info.plist").exists():
        pytest.skip("PRECONDITION Evie.app not built; nothing launched")
    if len(_pgrep("-x", "TextEdit")) != 1:
        pytest.skip("PRECONDITION FIXTURE_NOT_READY: expected exactly one TextEdit process; nothing launched")
    if not _pgrep("-x", "Finder"):
        pytest.skip("PRECONDITION ACTIVATION_FIXTURE_NOT_READY: Finder not running; nothing launched")
    from services.cc_live_validation import fixtures as F
    if not F.TEXT.path.exists() or F.TEXT.path.read_text() != F.TEXT_MARKER:
        pytest.skip("PRECONDITION FIXTURE_NOT_READY: scratch text fixture changed or missing; nothing launched")
    assert _pgrep("-f", EVIE_PATTERN) == [], "an Evie process is already running"

    out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    proc = subprocess.Popen(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(err), str(app),
                             "--args", "live_validation_14b_final"], shell=False)
    lp, seen, deadline = _LibProc(), {}, time.monotonic() + 600
    while proc.poll() is None and time.monotonic() < deadline:
        for pid in _pgrep("-f", EVIE_PATTERN):
            if pid not in seen:
                rpid, _ = _responsible_pid(pid)
                if rpid:
                    seen[pid] = {"ppid": lp.ppid(pid), "exe": lp.path(pid), "responsible_pid": rpid,
                                 "responsible_exe": lp.path(rpid)}
        time.sleep(0.05)
    code = proc.wait(timeout=max(1, deadline - time.monotonic()))
    text = out.read_text() if out.exists() else ""
    print("\nEVIE_APP_STDERR " + (err.read_text()[-4000:] if err.exists() else "<none>"))
    print("INDEPENDENT_PROCESS_OBSERVATION " + json.dumps(seen, indent=1))
    assert "STEP14B_FINAL" in text, (code, text[-2000:])
    report = json.JSONDecoder().raw_decode(text[text.index("{", text.index("STEP14B_FINAL")):])[0]
    print("STEP14B_FINAL " + json.dumps(report, indent=1))

    launcher = CONTRACT.bundle_path + CONTRACT.launcher_executable
    for row in seen.values():                                  # everything Evie ran was attributed to Evie
        assert row["responsible_exe"] == launcher, row
    assert report["unexpected_computer_control_operations"] == 0 and report["retries"] == 0
    assert report["result"] == "PASS", (report["stopped_at"], report["stop_reason"])
    assert code == 0
    assert report["computer_control_mutation_count"] == 7 and report["worker_stopped"]
    assert report["fixture_process_lifecycle_actions"] == 0
    assert all(c["ok"] for c in report["identity_checks"]) and len(report["identity_checks"]) == 8
