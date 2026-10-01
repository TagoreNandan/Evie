"""
LIVE: the CC-1a Section 18 benchmark under Evie.app (mode cc1a_benchmark). Run explicitly, alone, ONCE:
    python -m pytest tests/test_computer_control_live_cc1a_benchmark.py -m live -s -q
Records the result whatever the pass rate (the acceptance criterion is a RECORDED success rate). No retries.
"""

import json
import subprocess
from pathlib import Path

import pytest

from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT

pytestmark = pytest.mark.live


def _pgrep(*args):
    return [int(p) for p in subprocess.run(["/usr/bin/pgrep", *args], capture_output=True, text=True).stdout.split()]


def test_live_cc1a_benchmark(tmp_path):
    app = Path(CONTRACT.bundle_path)
    if not (app / "Contents/Info.plist").exists():
        pytest.skip("PRECONDITION Evie.app not built")
    if _pgrep("-x", "TextEdit"):
        pytest.skip("PRECONDITION TextEdit already running; nothing launched")
    for name in ("Finder", "Google Chrome"):
        if not _pgrep("-x", name):
            pytest.skip(f"PRECONDITION {name} not running (nothing is launched)")
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    subprocess.run(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(err), str(app),
                    "--args", "cc1a_benchmark"], shell=False, timeout=1800)
    text = out.read_text() if out.exists() else ""
    print("\nEVIE_APP_STDERR " + (err.read_text()[-3000:] if err.exists() else "<none>"))
    assert "CC1A_BENCHMARK" in text, text[-2000:]
    report = json.JSONDecoder().raw_decode(text[text.index("{", text.index("CC1A_BENCHMARK")):])[0]
    print("CC1A_BENCHMARK " + json.dumps(report, indent=1))
    assert report["total"] == 13 and len(report["tasks"]) == 13 and report["worker_stopped"]
    assert report["unexpected_operations"] == 0 and report["audit"]["sensitive_probe_hits"] == []
