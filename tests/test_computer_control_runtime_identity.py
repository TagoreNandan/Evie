"""
CC-1a Steps 12-13: Evie runtime identity rule (runtime.assess_production_identity), unit level.
Process and signature metadata are faked; nothing here reads the real machine. The rule must fail closed.
"""

import json

import pytest
from pydantic import ValidationError

from services.computer_control import runtime as R
from services.computer_control.runtime import (
    AccessibilityProbe, AccessibilityStatus as A, FunctionalProbe, FunctionalStatus as F, ProcessInfo,
    ProductionIdentityContract, RuntimeIdentity, RuntimeProbeResult, SignatureInfo, assess_production_identity,
)

TEAM = "ABCDE12345"
APP = "/Users/u/Applications/Evie.app/"
CONTRACT = ProductionIdentityContract(bundle_id="com.evie-assistant.evie", bundle_path=APP, team_id=TEAM,
                                      worker_identifier="com.evie-assistant.evie.python")


def sig(identifier, team=TEAM, valid=True, checked=True):
    return SignatureInfo(checked=checked, valid=valid, status=0 if valid else -67050, identifier=identifier,
                         team_id=team)


# The signed Evie identity (hypothetical values, same shape as the real one).
EVIE = ProcessInfo(pid=700, name="Evie", executable=APP + "Contents/MacOS/Evie", bundle_id="com.evie-assistant.evie",
                   signature=sig("com.evie-assistant.evie"))
CHECK = ProcessInfo(pid=701, name="evie-python", executable=APP + "Contents/MacOS/evie-python")
WORKER = ProcessInfo(pid=702, name="evie-python", executable=APP + "Contents/MacOS/evie-python",
                     signature=sig("com.evie-assistant.evie.python"))
LAUNCHD = ProcessInfo(pid=1, name="launchd", executable="/sbin/launchd")
EVIE_IDENTITY = RuntimeIdentity(worker=WORKER, parent=CHECK, responsible=EVIE, ancestry=(CHECK, EVIE),
                                responsible_lookup="responsibility_get_pid_responsible_for_pid")

# The development identity actually measured on 2026-09-29 (docs/07 Section 33).
CONDA = "/opt/miniconda3/bin/python3.13"
IDE = ProcessInfo(pid=2494, name="Electron", executable="/Applications/Antigravity IDE.app/Contents/MacOS/Electron",
                  bundle_id="com.google.antigravity-ide", signature=sig("com.google.antigravity-ide", "EQHXZ8M8AV"))
TERMINAL = ProcessInfo(pid=9, name="Terminal", bundle_id="com.apple.Terminal",
                       executable="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
                       signature=sig("com.apple.Terminal", None))
DEV_WORKER = ProcessInfo(pid=28646, name="python3.13", executable=CONDA,
                         signature=sig("python3-5555494467b536457ffd", None, valid=False))
DEV_IDENTITY = RuntimeIdentity(
    worker=DEV_WORKER, parent=ProcessInfo(pid=28644, name="python3.13", executable=CONDA), responsible=IDE,
    ancestry=(ProcessInfo(pid=28644, name="python3.13", executable=CONDA),
              ProcessInfo(pid=11278, name="claude.exe"), IDE), responsible_lookup="x")

OK_FN = FunctionalProbe(status=F.OK, target_bundle_id="com.apple.finder", target_pid=600, app_role_ax_error=0,
                        app_role="AXApplication")
FAILED_FN = FunctionalProbe(status=F.FAILED, target_bundle_id="com.apple.finder", target_pid=600,
                            app_role_ax_error=-25211, error="AXRole read returned AX error -25211")


def result(identity=EVIE_IDENTITY, ax=A.TRUSTED, fn=OK_FN, started=True):
    return RuntimeProbeResult(probed_at=1.0, worker_started=started, identity=identity,
                              accessibility=AccessibilityProbe(status=ax), functional=fn)


def assess(identity=EVIE_IDENTITY, contract=CONTRACT, **kw):
    return assess_production_identity(result(identity, **kw), contract)


def with_(p, **changes):
    return p.model_copy(update=changes)


def evie(**changes):
    return with_(EVIE_IDENTITY, **changes)


# --- A / K. a valid signed Evie identity ---

def test_a_valid_signed_evie_bundle_identity_is_accepted():
    a = assess()
    assert a.verified and a.reasons == ()


def test_k_full_identity_trusted_and_functional_is_true():
    assert assess(ax=A.TRUSTED, fn=OK_FN).verified


def test_worker_may_be_the_responsible_process_itself():
    helper = with_(WORKER, pid=700)
    assess_ = assess(RuntimeIdentity(worker=helper, parent=LAUNCHD, responsible=with_(EVIE, pid=700), ancestry=(),
                                     responsible_lookup="x"))
    assert "WORKER_NOT_UNDER_RESPONSIBLE_PROCESS" not in assess_.reasons


# --- B. wrong bundle id ---

@pytest.mark.parametrize("change, reason", [
    (dict(bundle_id="com.example.not-evie"), "RESPONSIBLE_BUNDLE_ID_MISMATCH"),
    (dict(bundle_id=None), "RESPONSIBLE_BUNDLE_ID_UNKNOWN"),
    (dict(signature=sig("com.example.not-evie")), "RESPONSIBLE_SIGNING_IDENTIFIER_MISMATCH"),
])
def test_b_wrong_bundle_identity(change, reason):
    a = assess(evie(responsible=with_(EVIE, **change)))
    assert not a.verified and reason in a.reasons


# --- C. wrong Team ID ---

def test_c_wrong_team_id_on_launcher():
    a = assess(evie(responsible=with_(EVIE, signature=sig("com.evie-assistant.evie", "ZZZZZ99999"))))
    assert a.reasons == ("RESPONSIBLE_TEAM_ID_MISMATCH",)


def test_c_wrong_team_id_on_worker():
    a = assess(evie(worker=with_(WORKER, signature=sig("com.evie-assistant.evie.python", "ZZZZZ99999"))))
    assert a.reasons == ("WORKER_TEAM_ID_MISMATCH",)


# --- D. wrong executable ---

@pytest.mark.parametrize("who, exe, reason", [
    ("responsible", APP + "Contents/MacOS/Other", "RESPONSIBLE_EXECUTABLE_MISMATCH"),
    ("responsible", "/tmp/Evie.app/Contents/MacOS/Evie", "RESPONSIBLE_EXECUTABLE_MISMATCH"),
    ("worker", APP + "Contents/MacOS/Evie", "WORKER_EXECUTABLE_MISMATCH"),
    ("worker", "/Users/u/Evie.app/Contents/MacOS/evie-python", "WORKER_EXECUTABLE_MISMATCH"),
])
def test_d_wrong_executable(who, exe, reason):
    base = EVIE if who == "responsible" else WORKER
    a = assess(evie(**{who: with_(base, executable=exe)}))
    assert not a.verified and reason in a.reasons


# --- E / F. development hosts ---

def test_e_antigravity_responsible_process():
    a = assess(evie(responsible=IDE, ancestry=(CHECK, IDE)))
    assert not a.verified and {"RESPONSIBLE_APP_IS_DEVELOPMENT_HOST", "DEVELOPMENT_HOST_IN_ANCESTRY",
                               "RESPONSIBLE_BUNDLE_ID_MISMATCH", "RESPONSIBLE_TEAM_ID_MISMATCH"} <= set(a.reasons)


def test_e_measured_development_identity_is_rejected():
    a = assess(DEV_IDENTITY)
    assert not a.verified and {"RESPONSIBLE_APP_IS_DEVELOPMENT_HOST", "WORKER_IS_GENERIC_INTERPRETER",
                               "WORKER_EXECUTABLE_MISMATCH", "WORKER_SIGNATURE_INVALID"} <= set(a.reasons)


def test_f_terminal_responsible_process():
    a = assess(evie(responsible=TERMINAL, ancestry=(CHECK, TERMINAL)))
    assert not a.verified and "RESPONSIBLE_APP_IS_DEVELOPMENT_HOST" in a.reasons


def test_f_development_host_anywhere_in_ancestry():
    a = assess(evie(ancestry=(CHECK, EVIE, IDE)))
    assert a.reasons == ("DEVELOPMENT_HOST_IN_ANCESTRY",)


# --- G. generic python ---

@pytest.mark.parametrize("exe", [CONDA, "/usr/bin/python3", "/opt/homebrew/bin/python3.12"])
def test_g_generic_python_worker(exe):
    a = assess(evie(worker=with_(WORKER, executable=exe)))
    assert not a.verified and {"WORKER_IS_GENERIC_INTERPRETER", "WORKER_EXECUTABLE_MISMATCH"} <= set(a.reasons)


def test_g_python_as_its_own_responsible_process():
    """The Step 12 disclaimed-spawn observation: python responsible for itself, not trusted."""
    py = ProcessInfo(pid=28683, name="python3.13", executable=CONDA)
    a = assess(RuntimeIdentity(worker=py, responsible=py, responsible_lookup="x"), ax=A.NOT_TRUSTED)
    assert not a.verified and {"WORKER_IS_GENERIC_INTERPRETER", "RESPONSIBLE_BUNDLE_ID_UNKNOWN",
                               "ACCESSIBILITY_NOT_TRUSTED", "RESPONSIBLE_SIGNATURE_NOT_CHECKED"} <= set(a.reasons)


# --- H. invalid / missing signature ---

@pytest.mark.parametrize("who, signature, reason", [
    ("responsible", sig("com.evie-assistant.evie", valid=False), "RESPONSIBLE_SIGNATURE_INVALID"),
    ("responsible", SignatureInfo(checked=False, error="BLOCKED_UNDER_TEST"), "RESPONSIBLE_SIGNATURE_NOT_CHECKED"),
    ("responsible", None, "RESPONSIBLE_SIGNATURE_NOT_CHECKED"),
    ("worker", sig("com.evie-assistant.evie.python", valid=False), "WORKER_SIGNATURE_INVALID"),
    ("worker", None, "WORKER_SIGNATURE_NOT_CHECKED"),
    ("worker", sig("com.evie-assistant.evie.python", team=None, valid=False), "WORKER_SIGNATURE_INVALID"),
])
def test_h_invalid_signature(who, signature, reason):
    base = EVIE if who == "responsible" else WORKER
    a = assess(evie(**{who: with_(base, signature=signature)}))
    assert not a.verified and reason in a.reasons


def test_h_an_invalid_signature_is_not_rescued_by_matching_claims():
    """A process claiming the right identifier/team but failing validity (e.g. ad-hoc, tampered) is rejected."""
    forged = SignatureInfo(checked=True, valid=False, identifier="com.evie-assistant.evie", team_id=TEAM)
    a = assess(evie(responsible=with_(EVIE, signature=forged)))
    assert a.reasons == ("RESPONSIBLE_SIGNATURE_INVALID",)


# --- I / J. Accessibility ---

@pytest.mark.parametrize("ax", [A.NOT_TRUSTED, A.UNAVAILABLE])
def test_i_accessibility_not_trusted(ax):
    a = assess(ax=ax)
    assert not a.verified and {"ACCESSIBILITY_NOT_TRUSTED", "RUNTIME_PROBE_NOT_READY"} <= set(a.reasons)


@pytest.mark.parametrize("fn", [FAILED_FN, FunctionalProbe(status=F.UNAVAILABLE, error="NO_SAFE_TARGET_RUNNING")])
def test_j_trusted_but_functional_read_fails(fn):
    a = assess(ax=A.TRUSTED, fn=fn)
    assert not a.verified and "FUNCTIONAL_AX_READ_FAILED" in a.reasons


def test_worker_not_started():
    assert not assess(started=False).verified


# --- L. forged input ---

def test_l_forged_production_identity_verified_is_rejected():
    data = json.loads(result(DEV_IDENTITY).model_dump_json())
    assert data["production_identity_verified"] is False
    with pytest.raises(ValidationError):
        RuntimeProbeResult.model_validate({**data, "production_identity_verified": True})
    forged = {**data, "production_identity_reasons": []}
    assert RuntimeProbeResult.model_validate(forged).production_identity_reasons != ()


def test_l_forged_signature_cannot_be_supplied_as_a_computed_flag():
    with pytest.raises(ValidationError):
        SignatureInfo(checked=True, valid=True, trusted=True)


# --- M. worker not belonging to the identity ---

def test_m_worker_not_under_the_responsible_process():
    a = assess(evie(ancestry=(CHECK, LAUNCHD)))
    assert a.reasons == ("WORKER_NOT_UNDER_RESPONSIBLE_PROCESS",)


def test_m_worker_signed_with_another_identifier():
    a = assess(evie(worker=with_(WORKER, signature=sig("com.example.helper"))))
    assert a.reasons == ("WORKER_SIGNING_IDENTIFIER_MISMATCH",)


# --- the declared contract ---

def test_declared_contract_matches_the_built_app():
    c = R.PRODUCTION_IDENTITY_CONTRACT
    assert c is not None and (c.bundle_id, c.team_id, c.worker_identifier) == (
        "com.evie-assistant.evie", "ZB8SA28XVY", "com.evie-assistant.evie.python")
    assert c.bundle_path.endswith("/Applications/Evie.app/")
    assert (c.launcher_executable, c.worker_executable) == ("Contents/MacOS/Evie", "Contents/MacOS/evie-python")


def test_no_contract_means_never_verified():
    a = assess(contract=None)
    assert a.reasons == ("NO_PRODUCTION_IDENTITY_CONTRACT",)


def test_contract_requires_real_values():
    with pytest.raises(ValidationError):
        ProductionIdentityContract(bundle_id="x.y", bundle_path="/usr/bin/", team_id=TEAM, worker_identifier="x.y.z")
    with pytest.raises(ValidationError):
        ProductionIdentityContract(bundle_id="x.y", bundle_path=APP, team_id="x", worker_identifier="x.y.z")
    with pytest.raises(ValidationError):
        ProductionIdentityContract(bundle_id="x.y", bundle_path=APP, team_id=TEAM, worker_identifier="x.y.z",
                                   worker_executable="../../bin/python")


def test_under_pytest_the_real_contract_is_never_satisfied_by_the_dev_chain():
    r = result(DEV_IDENTITY)
    assert r.ready and r.production_identity_verified is False


# --- Step 13 boundary pieces (no live machine access) ---

def test_signature_check_is_blocked_under_pytest(monkeypatch):
    from services.computer_control import macos_signature
    monkeypatch.delenv("EVIE_LIVE_MACOS", raising=False)
    s = macos_signature.process_signature(1)
    assert s.checked is False and s.valid is False and s.error == "BLOCKED_UNDER_TEST"


def test_collect_identity_attaches_signatures_from_the_injected_checker():
    from services.computer_control import macos_probe
    seen = []

    def checker(pid):
        seen.append(pid)
        return sig("x.y")
    ident = macos_probe.collect_identity(None, signature=checker)
    assert ident.worker.signature == sig("x.y") and seen[0] == ident.worker.pid
    if ident.responsible is not None:
        assert ident.responsible.signature == sig("x.y")


def test_identity_check_never_observes_or_acts():
    import ast
    from pathlib import Path
    from services.computer_control import identity_check
    tree = ast.parse(Path(identity_check.__file__).read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"observe", "act", "run", "Popen", "system"}
    assert {"start", "stop"} <= calls


def test_launcher_runs_only_code_owned_programs():
    """Step 14A: the program is chosen from a closed mode set (see test_computer_control_live_harness.py)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "packaging/macos/EvieLauncher.swift").read_text()
    assert 'let program = ["-s", "-B", "-m", modes[mode]!]' in src and 'mode = "identity_check"' in src
    assert "/bin/sh" not in src and "bash" not in src
    assert src.count("child.arguments = program") == 1 and "child.environment = [" in src


def test_build_script_refuses_ad_hoc_signing():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "packaging/macos/build_evie_app.sh").read_text()
    assert 'ad-hoc signing is not an identity; refusing' in src and "--deep" not in src.replace("never --deep", "")


# --- Step 13 debug regression: a legitimately verified result must cross the worker boundary ---

def _real_contract_identity():
    c = R.PRODUCTION_IDENTITY_CONTRACT
    evie_ = ProcessInfo(pid=700, name="Evie", executable=c.bundle_path + c.launcher_executable,
                        bundle_id=c.bundle_id, signature=sig(c.bundle_id, c.team_id),
                        bundle_lookup=R.BundleLookup(status="FOUND"))
    worker = ProcessInfo(pid=702, name="evie-python", executable=c.bundle_path + c.worker_executable,
                         signature=sig(c.worker_identifier, c.team_id))
    check = ProcessInfo(pid=701, name="evie-python", executable=c.bundle_path + c.worker_executable)
    return RuntimeIdentity(worker=worker, parent=check, responsible=evie_, ancestry=(check, evie_),
                           responsible_lookup="responsibility_get_pid_responsible_for_pid")


def test_verified_result_round_trips_through_the_worker_protocol():
    """Root cause of the Step 13 live failure: the worker serialised production_identity_verified=true and the
    parent rejected it as 'forged' (PROTOCOL_ERROR). A value equal to the recomputation must be accepted."""
    from services.computer_control import worker as W
    r = result(_real_contract_identity())
    assert r.production_identity_verified is True and r.production_identity_reasons == ()
    wire = json.loads(json.dumps({"type": "probe_result", "result": json.loads(r.model_dump_json())}))
    assert wire["result"]["production_identity_verified"] is True
    msg = W._FROM_WORKER.validate_python(wire)
    assert msg.result.production_identity_verified is True


def test_claims_that_differ_from_the_evidence_are_rejected():
    good = json.loads(result(_real_contract_identity()).model_dump_json())
    bad = json.loads(result(_real_contract_identity(), ax=A.NOT_TRUSTED).model_dump_json())
    with pytest.raises(ValidationError):
        RuntimeProbeResult.model_validate({**bad, "production_identity_verified": True})
    with pytest.raises(ValidationError):
        RuntimeProbeResult.model_validate({**good, "production_identity_verified": False})
    assert RuntimeProbeResult.model_validate({**good, "production_identity_reasons": ["X"]}).production_identity_reasons == ()


# --- Step 14B debug: the single failing source (read-only incident reproduction, pure) ---

def test_step14b_incident_evidence_fails_closed_on_the_bundle_id_alone():
    """The live identity row before 'activate Finder': everything valid except the NSRunningApplication bundle id.
    The rule must report exactly that one reason and refuse; no other source may stand in for it."""
    ident = _real_contract_identity()
    missing = ident.model_copy(update={"responsible": ident.responsible.model_copy(update={"bundle_id": None})})
    a = assess(missing, contract=R.PRODUCTION_IDENTITY_CONTRACT)
    assert a.verified is False and a.reasons == ("RESPONSIBLE_BUNDLE_ID_UNKNOWN",)


# --- Step 14B debug-1: bundle lookup diagnostics (fail closed in every non-FOUND state) ---

from services.computer_control import macos_probe as MP  # noqa: E402
from services.computer_control.runtime import BundleLookup  # noqa: E402


class _App:
    def __init__(self, bid):
        self.bid = bid

    def bundleIdentifier(self):
        if isinstance(self.bid, Exception):
            raise self.bid
        return self.bid


def _api(result):
    """_PyObjCApi with a fake AppKit (no PyObjC): result is an _App, None, or an exception to raise."""
    class NSRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid):
            if isinstance(result, Exception):
                raise result
            return result
    api = object.__new__(MP._PyObjCApi)
    api._appkit = type("AppKit", (), {"NSRunningApplication": NSRunningApplication})
    return api


@pytest.mark.parametrize("result, status, bundle", [
    (_App("com.evie-assistant.evie"), "FOUND", "com.evie-assistant.evie"),               # D
    (None, "NO_RUNNING_APPLICATION", None),                                              # A (object missing)
    (_App(None), "BUNDLE_ID_ABSENT", None),                                              # B
    (_App(""), "BUNDLE_ID_ABSENT", None),
    (RuntimeError("objc lookup failed"), "LOOKUP_EXCEPTION", None),                     # C
    (_App(ValueError("bad bundle")), "LOOKUP_EXCEPTION", None),
])
def test_bundle_lookup_states(result, status, bundle):
    bid, lookup = _api(result).bundle_lookup(700)
    assert (bid, lookup.status) == (bundle, status)
    assert _api(result).bundle_for_pid(700) == bundle                  # the old API still returns id-or-None


def test_lookup_exception_records_type_and_a_sanitised_message():
    err = RuntimeError("NSInternalInconsistency: <NSRunningApplication: 0x6000 pid=56802 secret>\nline two\x07")
    _, lookup = _api(err).bundle_lookup(700)
    assert lookup.error_type == "RuntimeError"
    assert lookup.error_message == "NSInternalInconsistency: <object omitted>"
    assert "0x6000" not in lookup.error_message and "line two" not in lookup.error_message
    long = RuntimeError("x" * 500)
    assert len(_api(long).bundle_lookup(700)[1].error_message) == 120


class _Lib:
    def __init__(self, exists=True):
        self.exists = exists

    def name(self, pid):
        return "Evie" if self.exists else None

    def path(self, pid):
        return "/x/Evie.app/Contents/MacOS/Evie" if self.exists else None

    def ppid(self, pid):
        return None


@pytest.mark.parametrize("result, exists, status", [
    (None, False, "PROCESS_NOT_FOUND"),              # A: no NSRunningApplication AND libproc knows no such process
    (None, True, "NO_RUNNING_APPLICATION"),          # the process exists; AppKit returned no object
    (_App(None), True, "BUNDLE_ID_ABSENT"),           # B
    (RuntimeError("boom"), True, "LOOKUP_EXCEPTION"),  # C
])
def test_collect_identity_records_why_the_responsible_bundle_id_is_missing(result, exists, status):
    api = _api(result)
    api.main_bundle_id = lambda: None
    ident = MP.collect_identity(api, libproc=_Lib(exists), signature=lambda pid: sig("x"))
    assert ident.responsible is not None
    assert ident.responsible.bundle_id is None and ident.responsible.bundle_lookup.status == status
    assert ident.responsible.signature == sig("x")                      # other evidence still collected


def test_collect_identity_found_is_unchanged():
    api = _api(_App("com.evie-assistant.evie"))
    api.main_bundle_id = lambda: None
    ident = MP.collect_identity(api, libproc=_Lib(), signature=lambda pid: sig("x"))
    assert ident.responsible.bundle_id == "com.evie-assistant.evie"
    assert ident.responsible.bundle_lookup == BundleLookup(status="FOUND")


def _with_lookup(lookup, bundle_id=None):
    ident = _real_contract_identity()
    return ident.model_copy(update={"responsible": ident.responsible.model_copy(
        update={"bundle_id": bundle_id, "bundle_lookup": lookup})})


@pytest.mark.parametrize("lookup, detail", [
    (BundleLookup(status="PROCESS_NOT_FOUND"), "RESPONSIBLE_PROCESS_NOT_FOUND"),
    (BundleLookup(status="NO_RUNNING_APPLICATION"), "RESPONSIBLE_NO_RUNNING_APPLICATION"),
    (BundleLookup(status="BUNDLE_ID_ABSENT"), "RESPONSIBLE_BUNDLE_ID_ABSENT"),
    (BundleLookup(status="LOOKUP_EXCEPTION", error_type="RuntimeError", error_message="x"),
     "RESPONSIBLE_BUNDLE_LOOKUP_EXCEPTION"),
    (None, None),                                                     # not recorded: the old single reason
])
def test_e_every_failure_state_still_fails_closed(lookup, detail):
    a = assess(_with_lookup(lookup), contract=R.PRODUCTION_IDENTITY_CONTRACT)
    assert a.verified is False
    assert a.reasons == (("RESPONSIBLE_BUNDLE_ID_UNKNOWN", detail) if detail else ("RESPONSIBLE_BUNDLE_ID_UNKNOWN",))
    r = result(_with_lookup(lookup))
    assert r.production_identity_verified is False


def test_found_with_the_right_bundle_id_still_verifies():
    assert assess(_with_lookup(BundleLookup(status="FOUND"), "com.evie-assistant.evie"),
                  contract=R.PRODUCTION_IDENTITY_CONTRACT).verified


def test_no_signature_fallback_for_a_missing_bundle_id():
    """The signature identifier equals the contract bundle id here, and still does not stand in for it."""
    ident = _with_lookup(BundleLookup(status="NO_RUNNING_APPLICATION"))
    assert ident.responsible.signature.identifier == R.PRODUCTION_IDENTITY_CONTRACT.bundle_id
    assert not assess(ident, contract=R.PRODUCTION_IDENTITY_CONTRACT).verified


def test_f_forged_true_with_a_failing_lookup_is_rejected_and_diagnostics_cross_the_worker_boundary():
    from services.computer_control import worker as W
    lookup = BundleLookup(status="LOOKUP_EXCEPTION", error_type="RuntimeError", error_message="boom")
    data = json.loads(result(_with_lookup(lookup)).model_dump_json())
    assert data["identity"]["responsible"]["bundle_lookup"]["status"] == "LOOKUP_EXCEPTION"
    msg = W._FROM_WORKER.validate_python({"type": "probe_result", "result": data})
    assert msg.result.identity.responsible.bundle_lookup == lookup
    assert "RESPONSIBLE_BUNDLE_LOOKUP_EXCEPTION" in msg.result.production_identity_reasons
    with pytest.raises(ValidationError):
        RuntimeProbeResult.model_validate({**data, "production_identity_verified": True})
