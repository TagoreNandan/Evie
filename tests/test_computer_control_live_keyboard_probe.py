"""
LIVE (Step 15B): bounded press_key probe under Evie.app. Run explicitly, alone, ONCE:
    python -m pytest tests/test_computer_control_live_keyboard_probe.py -m live -s -q
Launches only Evie.app (mode keyboard_probe_15b). A leftover fixture TextEdit is verified read-only by the harness before it is retired.
No retries.
"""

import json
import subprocess
from pathlib import Path

import pytest

from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT

pytestmark = pytest.mark.live


def _pgrep(*args):
    return [int(p) for p in subprocess.run(["/usr/bin/pgrep", *args], capture_output=True, text=True).stdout.split()]


def test_live_keyboard_probe(tmp_path):
    app = Path(CONTRACT.bundle_path)
    if not (app / "Contents/Info.plist").exists():
        pytest.skip("PRECONDITION Evie.app not built; nothing launched")
    if len(_pgrep("-x", "TextEdit")) > 1:
        pytest.skip("PRECONDITION FIXTURE_NOT_READY: more than one TextEdit; nothing launched")
    # a single running TextEdit is retired by the harness ONLY if it is exactly our unchanged text fixture
    assert _pgrep("-f", "Applications/Evie.app/Contents/MacOS/") == []
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    subprocess.run(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(err), str(app),
                    "--args", "keyboard_probe_15b"], shell=False, timeout=300)
    text = out.read_text() if out.exists() else ""
    print("\nEVIE_APP_STDERR " + (err.read_text()[-3000:] if err.exists() else "<none>"))
    assert "KEYBOARD_PROBE_15B" in text, text[-1500:]
    report = json.JSONDecoder().raw_decode(text[text.index("{", text.index("KEYBOARD_PROBE_15B")):])[0]
    print("KEYBOARD_PROBE_15B " + json.dumps(report, indent=1))
    assert report["unexpected_computer_control_operations"] == 0 and report["retries"] == 0
    assert report["status"] == "PRESS_KEY_READY_FOR_REVIEW", (report["stopped_at"], report["stop_reason"])
    assert report["keyboard_events_posted"] == 4 and report["worker_stopped"]
