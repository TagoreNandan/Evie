"""
Computer-control runtime identity + permission probe (docs/07 Section 14), unit level.
Platform interactions are faked; AX/AppKit is blocked under pytest and never imported here.
"""

import json

import pytest
from pydantic import ValidationError

from services.computer_control import macos_probe
from services.computer_control.runtime import (
    AccessibilityProbe, AccessibilityStatus as A, FunctionalProbe, FunctionalStatus as F, ProcessInfo,
    RuntimeIdentity, RuntimeProbeResult,
)

WORKER = ProcessInfo(pid=4242, name="python3.13", executable="/opt/miniconda3/bin/python3.13")
IDE = ProcessInfo(pid=2494, name="Electron", executable="/Applications/Antigravity IDE.app/Contents/MacOS/Electron",
                  bundle_id="com.google.antigravity-ide")
IDENTITY = RuntimeIdentity(worker=WORKER, parent=IDE, responsible=IDE, ancestry=(IDE,),
                           responsible_lookup="responsibility_get_pid_responsible_for_pid")
OK_FN = FunctionalProbe(status=F.OK, target_bundle_id="com.apple.finder", target_pid=600, app_role_ax_error=0,
                        app_role="AXApplication", window_role_ax_error=-25212)


def _result(ax=A.TRUSTED, fn=OK_FN, identity=IDENTITY, started=True):
    return RuntimeProbeResult(probed_at=1.0, worker_started=started, identity=identity,
                              accessibility=AccessibilityProbe(status=ax), functional=fn)


# --- models / readiness ---

def test_trusted_and_functional_is_ready():
    r = _result()
    assert r.ready and r.to_permission_status().ok
    assert r.identity_scope == "worker_runtime" and r.production_identity_verified is False


@pytest.mark.parametrize("kw", [
    {"ax": A.NOT_TRUSTED},
    {"ax": A.UNAVAILABLE},
    {"fn": FunctionalProbe(status=F.FAILED, app_role_ax_error=-25211)},
    {"fn": FunctionalProbe(status=F.UNAVAILABLE, error="NO_SAFE_TARGET_RUNNING")},
    {"started": False},
    {"identity": RuntimeIdentity(worker=WORKER)},                                   # responsible unknown
    {"identity": RuntimeIdentity(worker=ProcessInfo(pid=1), responsible=IDE)},     # executable unknown
])
def test_anything_less_is_not_ready(kw):
    r = _result(**kw)
    assert not r.ready and not r.to_permission_status().ok


def test_permission_status_matches_readiness_exactly():
    for ax in A:
        for fn in (OK_FN, FunctionalProbe(status=F.FAILED)):
            r = _result(ax=ax, fn=fn)
            assert r.to_permission_status().ok == r.ready


def test_production_identity_can_never_be_claimed():
    with pytest.raises(ValidationError):
        RuntimeProbeResult(**{**_result().model_dump(), "production_identity_verified": True})


def test_forged_ready_flag_is_ignored_on_parse():
    data = json.loads(_result(ax=A.NOT_TRUSTED).model_dump_json())
    data["ready"] = True
    data["identity"]["sufficient"] = True
    parsed = RuntimeProbeResult.model_validate(data)
    assert parsed.ready is False


def test_development_host_label_needs_a_known_bundle():
    assert IDENTITY.responsible_is_development_host is True
    unknown = RuntimeIdentity(worker=WORKER, responsible=ProcessInfo(pid=2494, name="Electron"))
    assert unknown.responsible_is_development_host is None     # bundle id never inferred from a name/path


def test_diagnostics_serialise_and_round_trip():
    r = RuntimeProbeResult(**{**_result().model_dump(), "diagnostics": ("RESPONSIBLE_APP_IS_DEVELOPMENT_HOST",)})
    text = r.model_dump_json()
    assert json.loads(text)["ready"] is True
    assert RuntimeProbeResult.model_validate_json(text) == r


def test_no_raw_ax_objects_can_be_carried():
    with pytest.raises(ValidationError):
        FunctionalProbe(status=F.OK, ax_element="<AXUIElement 0x1>")
    with pytest.raises(ValidationError):
        FunctionalProbe(status=F.OK, app_role=object())
    blob = _result().model_dump_json()
    assert "AXUIElement" not in blob


# --- probe logic with a fake platform API ---

class FakeApi:
    def __init__(self, trusted=True, running=None, app_role=(0, "AXApplication"), window=(0, "AXWindow"),
                 raise_on=None):
        self.trusted, self.running = trusted, running if running is not None else {"com.apple.finder": 600}
        self._app_role, self._window, self.raise_on = app_role, window, raise_on or set()
        self.calls = []

    def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self.raise_on:
            raise RuntimeError(f"{name} failed")

    def is_trusted(self):
        self._maybe_raise("is_trusted")
        return self.trusted

    def bundle_for_pid(self, pid):
        return "com.google.antigravity-ide" if pid == 2494 else None

    def main_bundle_id(self):
        return None

    def running_pid(self, bundle):
        self._maybe_raise("running_pid")
        return self.running.get(bundle)

    def app_role(self, pid):
        self._maybe_raise("app_role")
        return self._app_role

    def focused_window_role(self, pid):
        self._maybe_raise("focused_window_role")
        return self._window


@pytest.fixture
def live_ax_env(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")   # lift the pytest block; the fake API does the "AX" work


def test_probe_is_blocked_under_pytest_by_default():
    r = macos_probe.probe_runtime(FakeApi())
    assert not r.ready and r.accessibility.status is A.UNAVAILABLE and "AX_BLOCKED_UNDER_TEST" in r.diagnostics


def test_trusted_functional_probe(live_ax_env):
    api = FakeApi()
    r = macos_probe.probe_runtime(api)
    assert r.accessibility.status is A.TRUSTED and r.functional.status is F.OK
    assert r.functional.target_bundle_id == "com.apple.finder" and r.functional.window_role == "AXWindow"
    assert r.identity.worker.pid > 0 and r.identity.worker.executable
    # read-only: only lookups and attribute reads were made
    assert set(api.calls) <= {"is_trusted", "running_pid", "app_role", "focused_window_role"}


def test_untrusted_result(live_ax_env):
    r = macos_probe.probe_runtime(FakeApi(trusted=False, app_role=(-25211, None)))   # kAXErrorAPIDisabled
    assert r.accessibility.status is A.NOT_TRUSTED and r.functional.status is F.FAILED and not r.ready
    assert r.functional.app_role_ax_error == -25211


def test_trust_probe_error_is_unavailable(live_ax_env):
    r = macos_probe.probe_runtime(FakeApi(raise_on={"is_trusted"}))
    assert r.accessibility.status is A.UNAVAILABLE and "RuntimeError" in r.accessibility.error and not r.ready


def test_functional_probe_error_fails(live_ax_env):
    r = macos_probe.probe_runtime(FakeApi(raise_on={"app_role"}))
    assert r.functional.status is F.FAILED and not r.ready


def test_no_safe_target_running_is_unavailable_and_nothing_is_launched(live_ax_env):
    api = FakeApi(running={})
    r = macos_probe.probe_runtime(api)
    assert r.functional.status is F.UNAVAILABLE and not r.ready
    assert "NO_SAFE_TARGET_RUNNING" in r.functional.error
    assert "app_role" not in api.calls


def test_textedit_used_only_when_finder_is_absent(live_ax_env):
    r = macos_probe.probe_runtime(FakeApi(running={"com.apple.TextEdit": 700}))
    assert r.functional.target_bundle_id == "com.apple.TextEdit"


def test_pyobjc_unavailable_fails_closed(live_ax_env):
    def missing():
        raise macos_probe.PyObjCUnavailable("ModuleNotFoundError: No module named 'objc'")
    r = macos_probe.probe_runtime(api_factory=missing)
    assert not r.ready and r.accessibility.status is A.UNAVAILABLE and "PYOBJC_UNAVAILABLE" in r.diagnostics


def test_identity_reports_this_process_and_its_responsible_app():
    ident = macos_probe.collect_identity(None)
    import os, sys
    assert ident.worker.pid == os.getpid()
    assert ident.worker.executable == os.path.realpath(sys.executable)
    assert ident.worker.bundle_id is None                  # no AppKit lookup without the API
    if ident.responsible is not None:
        assert ident.responsible_lookup == "responsibility_get_pid_responsible_for_pid"
    else:
        assert ident.responsible_lookup != "responsibility_get_pid_responsible_for_pid"
