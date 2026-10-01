"""
LIVE (Step 14B): real-Evie activation-boundary diagnostic. Run explicitly, alone, ONCE:
    packaging/macos/diagnostics/build_scratch_activation_target.sh
    python -m pytest tests/test_computer_control_live_activation_boundary.py -m live -s -q

Test infrastructure launches ONLY the dedicated scratch target (background, `open -g -F`), then Evie.app in the
closed mode `activation_boundary_check`. Inside Evie, a process attributed to Evie activates that scratch app once
(never a user app, never the executor, no AX mutation) and samples Evie's bundle lookup for >= 16 s.
Afterwards test infrastructure ends the scratch target (SIGTERM). No retries.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from services.cc_live_validation import activation_boundary as AB
from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT

pytestmark = pytest.mark.live


def _pgrep(pattern):
    out = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True, shell=False)
    return [int(p) for p in out.stdout.split()]


def test_live_activation_boundary(tmp_path):
    app, target_app = Path(CONTRACT.bundle_path), Path(AB.TARGET_EXECUTABLE).parents[2]
    if not (app / "Contents/Info.plist").exists() or not Path(AB.TARGET_EXECUTABLE).exists():
        pytest.skip("PRECONDITION Evie.app or the scratch target is not built; nothing launched")
    assert _pgrep("Applications/Evie.app/Contents/MacOS/") == [] and _pgrep(AB.TARGET_EXECUTABLE) == []
    subprocess.run(["/usr/bin/open", "-g", "-F", str(target_app)], shell=False, check=True, timeout=30)
    try:
        for _ in range(50):
            if _pgrep(AB.TARGET_EXECUTABLE):
                break
            time.sleep(0.1)
        out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
        done = subprocess.run(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(err), str(app),
                               "--args", "activation_boundary_check"], shell=False, timeout=180)
        text = out.read_text() if out.exists() else ""
        print("\nEVIE_APP_STDERR " + (err.read_text()[-3000:] if err.exists() else "<none>"))
        assert "ACTIVATION_BOUNDARY_CHECK" in text, (done.returncode, text[-1500:])
        report = json.JSONDecoder().raw_decode(text[text.index("{", text.index("ACTIVATION_BOUNDARY_CHECK")):])[0]
        print("ACTIVATION_BOUNDARY_CHECK " + json.dumps(report, indent=1))
    finally:
        for pid in _pgrep(AB.TARGET_EXECUTABLE):                  # scratch cleanup (test infrastructure only)
            os.kill(pid, signal.SIGTERM)
    assert _pgrep("Applications/Evie.app/Contents/MacOS/") == []
    assert report["worker_stopped"] and report["production_mutations"] == 0 and report["ax_mutations"] == 0
    assert report["status"] == "ESTABLISHED", report.get("reason")
