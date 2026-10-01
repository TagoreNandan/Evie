"""
LIVE (CC-1a Step 13): read-only Evie runtime identity diagnostic.
Run explicitly and alone, after `packaging/macos/build_evie_app.sh`:
    python -m pytest tests/test_computer_control_live_identity.py -m live -s -q

Launches ONLY Evie.app itself through LaunchServices (`open -W`), so the runtime starts outside the IDE's
process chain. Evie.app runs its one fixed program (services.computer_control.identity_check), which starts
the worker, reads identity / code signatures / Accessibility trust (prompt off) / one AXRole read, prints a
JSON report and stops the worker. No observe/act command is ever sent: nothing is activated, focused,
clicked, typed or scrolled, and no permission is requested or granted.
"""

import json
import subprocess
from pathlib import Path

import pytest

from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT

pytestmark = pytest.mark.live
IDENTITY_REASONS_ALLOWED_WHILE_PENDING = {"RUNTIME_PROBE_NOT_READY", "ACCESSIBILITY_NOT_TRUSTED",
                                          "FUNCTIONAL_AX_READ_FAILED"}


def test_live_evie_identity(tmp_path):
    app = Path(CONTRACT.bundle_path)
    if not (app / "Contents/Info.plist").exists():
        pytest.skip(f"PRECONDITION {app} not built (packaging/macos/build_evie_app.sh); nothing launched")
    out = tmp_path / "stdout.txt"
    done = subprocess.run(["/usr/bin/open", "-W", "--stdout", str(out), "--stderr", str(tmp_path / "stderr.txt"),
                           str(app)], shell=False, timeout=180, check=False)
    text = out.read_text() if out.exists() else ""
    err = tmp_path / "stderr.txt"
    print("\nEVIE_APP_STDERR " + (err.read_text()[-4000:] if err.exists() else "<none>"))
    assert done.returncode == 0 and "EVIE_IDENTITY_CHECK" in text, (done.returncode, text[-500:])
    start = text.index("{", text.index("EVIE_IDENTITY_CHECK"))
    report = json.JSONDecoder().raw_decode(text[start:])[0]
    print("\nEVIE_IDENTITY_DIAGNOSTIC " + json.dumps(report, indent=1))
    assert "WORKER_STOPPED state=STOPPED" in text

    # Identity: signed Evie is the responsible process, the worker is the signed interpreter under it.
    assert report["RESPONSIBLE_BUNDLE_ID"] == CONTRACT.bundle_id
    assert report["RESPONSIBLE_APP"] == CONTRACT.bundle_path + CONTRACT.launcher_executable
    assert report["SIGNATURE_VALID"] is True and report["TEAM_ID"] == CONTRACT.team_id
    assert report["SIGNING_IDENTITY"] == CONTRACT.bundle_id
    assert report["WORKER_SIGNATURE_VALID"] is True and report["WORKER_TEAM_ID"] == CONTRACT.team_id
    assert report["WORKER_EXECUTABLE"] == CONTRACT.bundle_path + CONTRACT.worker_executable
    assert str(report["EVIE_PID"]) in " ".join(report["ANCESTRY"])
    assert not any("antigravity" in a.lower() or "terminal" in a.lower() for a in report["ANCESTRY"])

    if not report["PRODUCTION_IDENTITY_VERIFIED"]:
        assert set(report["PRODUCTION_IDENTITY_REASONS"]) <= IDENTITY_REASONS_ALLOWED_WHILE_PENDING, \
            report["PRODUCTION_IDENTITY_REASONS"]
        pytest.skip("Identity established, Accessibility permission pending: System Settings > Privacy & "
                    f"Security > Accessibility > add {CONTRACT.bundle_path.rstrip('/')}")
    assert report["ACCESSIBILITY_TRUST"] == "TRUSTED" and report["FUNCTIONAL_AX_READ"]["status"] == "OK"
