"""
LIVE (CC-1a Step 14A): Evie-owned live harness, PRE-MUTATION. Run explicitly and alone, after
`packaging/macos/build_evie_app.sh`:
    python -m pytest tests/test_computer_control_live_evie_harness.py -m live -s -q

Launches ONLY Evie.app (LaunchServices, `open -W ... --args live_harness`). Evie runs
services.computer_control.live_harness: worker start + identity/Accessibility probe, fail closed unless
production_identity_verified, Step 11 session/orchestrator construction with a refusing act port, ONE read-only
observation of Finder, then stop. No choice, gate, action or executor call happens.
While it runs, this test independently records the Evie processes (parent, executable, responsible process)
from outside using the existing read-only libproc/responsibility helpers.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from services.computer_control.macos_probe import _LibProc, _responsible_pid
from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT

pytestmark = pytest.mark.live
PATTERN = "Applications/Evie.app/Contents/MacOS/"


def _evie_pids():
    out = subprocess.run(["/usr/bin/pgrep", "-f", PATTERN], capture_output=True, text=True, shell=False)
    return [int(p) for p in out.stdout.split()]


def _snapshot(lp, pids):
    rows = {}
    for pid in pids:
        rpid, _ = _responsible_pid(pid)
        rows[pid] = {"ppid": lp.ppid(pid), "exe": lp.path(pid), "responsible_pid": rpid,
                     "responsible_exe": lp.path(rpid) if rpid else None}
    return rows


def test_live_evie_harness(tmp_path):
    app = Path(CONTRACT.bundle_path)
    if not (app / "Contents/Info.plist").exists():
        pytest.skip(f"PRECONDITION {app} not built; nothing launched")
    assert _evie_pids() == [], "an Evie process is already running; not starting another"
    launcher_exe = CONTRACT.bundle_path + CONTRACT.launcher_executable
    worker_exe = CONTRACT.bundle_path + CONTRACT.worker_executable
    out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    proc = subprocess.Popen(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(err), str(app),
                             "--args", "live_harness"], shell=False)
    lp, seen, deadline = _LibProc(), {}, time.monotonic() + 180
    while proc.poll() is None and time.monotonic() < deadline:
        for pid, row in _snapshot(lp, _evie_pids()).items():
            if row["exe"] and row["responsible_pid"]:
                seen.setdefault(pid, row)
        time.sleep(0.02)
    assert proc.wait(timeout=max(1, deadline - time.monotonic())) == 0
    text = out.read_text() if out.exists() else ""
    print("\nEVIE_APP_STDERR " + (err.read_text()[-4000:] if err.exists() else "<none>"))
    print("INDEPENDENT_PROCESS_OBSERVATION " + json.dumps(seen, indent=1))
    assert "STEP14A_HARNESS" in text, text[-1000:]
    report = json.JSONDecoder().raw_decode(text[text.index("{", text.index("STEP14A_HARNESS")):])[0]
    print("STEP14A_HARNESS " + json.dumps(report, indent=1))

    # The harness's own verdict.
    assert report["harness_state"] == "READY", (report["failed_stage"], report["failure_reason"])
    assert report["production_identity_verified"] is True and report["production_identity_reasons"] == []
    assert report["worker_ready"] and report["observer_ready"] and report["observation_created"]
    assert report["mutation_attempted"] is False and report["mutation_count"] == 0 and report["worker_stopped"]
    assert report["responsible_bundle_id"] == CONTRACT.bundle_id and report["team_id"] == CONTRACT.team_id

    # Independent process observation: launcher -> harness -> worker, all attributed to the Evie launcher.
    evie = report["evie_pid"]
    assert seen.get(evie, {}).get("exe") == launcher_exe and seen[evie]["responsible_pid"] == evie
    harness, worker = report["harness_pid"], report["worker_pid"]
    for pid, parent in ((harness, evie), (worker, harness)):
        if pid in seen:                                      # a process may exit before the next poll
            assert seen[pid]["ppid"] == parent and seen[pid]["exe"] == worker_exe
    assert harness in seen, "harness process was never observed independently"
    for row in seen.values():
        assert row["responsible_pid"] == evie and row["responsible_exe"] == launcher_exe, row
        assert "antigravity" not in (row["responsible_exe"] or "").lower()
    print(f"INDEPENDENTLY_OBSERVED harness={harness in seen} worker={worker in seen}")

    for _ in range(50):                                      # no Evie process remains
        if not _evie_pids():
            break
        time.sleep(0.1)
    assert _evie_pids() == []
