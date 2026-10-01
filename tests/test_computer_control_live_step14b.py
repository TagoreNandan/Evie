"""
LIVE (CC-1a Step 14B): the ONE authorised Evie-owned live mutation run. Run explicitly, alone, ONCE:
    python -m pytest tests/test_computer_control_live_step14b.py -m live -s -q

Preconditions are checked BEFORE Evie.app is launched (a failed precondition consumes nothing): the app is
built, TextEdit is NOT running (so no user document can be touched), Finder is running. Then Evie.app is
launched once in the closed mode `live_validation_14b`; all mutations happen inside Evie
(services.cc_live_validation.step14b through the existing orchestrator/session/gate/worker). This test only
launches the app, independently records the Evie process chain, and reads the JSON report. It never retries.
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


def test_live_step14b(tmp_path):
    app = Path(CONTRACT.bundle_path)
    if not (app / "Contents/Info.plist").exists():
        pytest.skip("PRECONDITION Evie.app not built; nothing launched")
    if len(_pgrep("-x", "TextEdit")) > 1:
        pytest.skip("PRECONDITION FIXTURE_NOT_READY: more than one TextEdit process; nothing launched")
    # One running TextEdit is allowed ONLY as the leftover text fixture: the harness verifies that read-only
    # (exactly one window = our unchanged text fixture) before retiring it, else it stops with FIXTURE_NOT_READY.
    if not _pgrep("-x", "Finder"):
        pytest.skip("PRECONDITION ACTIVATION_FIXTURE_NOT_READY: Finder not running; nothing launched")
    assert _pgrep("-f", EVIE_PATTERN) == [], "an Evie process is already running"

    out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    proc = subprocess.Popen(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(err), str(app),
                             "--args", "live_validation_14b"], shell=False)
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
    assert code == 0 and "STEP14B_HARNESS" in text, (code, text[-2000:])
    report = json.JSONDecoder().raw_decode(text[text.index("{", text.index("STEP14B_HARNESS")):])[0]
    print("STEP14B_HARNESS " + json.dumps(report, indent=1))

    launcher = CONTRACT.bundle_path + CONTRACT.launcher_executable
    for row in seen.values():                                  # everything Evie ran was attributed to Evie
        assert row["responsible_exe"] == launcher, row
    assert report["unexpected_computer_control_operations"] == 0 and report["retries"] == 0
    assert report["result"] == "PASS", (report["stopped_at"], report["stop_reason"])
    assert report["computer_control_mutation_count"] == 14 and report["worker_stopped"]
    assert all(c["ok"] for c in report["identity_checks"]) and len(report["identity_checks"]) == 21
    assert report["fixtures_unchanged_on_disk"] is True
    assert report["textedit_running_at_end"] == []
