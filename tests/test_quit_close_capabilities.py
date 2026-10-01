"""
Dedicated Unit & Behavioral Tests for Application and Window Closing Capabilities.

Covers:
- quit named application ("Quit TextEdit.", "Quit Safari.", "Quit Calculator.")
- close current application ("Close this app.", "Quit the current application.")
- close current window ("Close this window.")
- close named window ("Close that window.")
- ambiguous close request ("Close App")
- attempt to quit EVIE_SELF ("Quit Evie", "Close Evie", "Quit this app" when Evie is frontmost)
- multi-application quit request ("Quit all applications", "Close everything")
- verification that the application actually exited
- verification that a window actually closed while the application remained running
- capability matrix entries verification
"""

import pytest

from services.computer_control.chooser import AppRef, ChooserInput, build_input
from services.computer_control.fast_path import fast_path as fast_path_select
from services.computer_control.gate import DEFAULT_POLICY, GateContext, GateOutcome, GatePolicy, RiskLevel, evaluate
from services.computer_control.models import (
    APP_ALIASES,
    CAPABILITY_MATRIX,
    ActivateAppArgs,
    AppIdentity,
    CloseWindowArgs,
    NoArgs,
    Observation,
    Op,
    QuitAppArgs,
    SettleInfo,
    SettleStatus,
    WindowKey,
)
from services.computer_control.verification import CloseWindowVerifier, QuitAppVerifier, VerificationStatus


TEXTEDIT = AppRef(name="TextEdit", bundle_id="com.apple.TextEdit")
SAFARI = AppRef(name="Safari", bundle_id="com.apple.Safari")
CALCULATOR = AppRef(name="Calculator", bundle_id="com.apple.calculator")
EVIE_APP = AppRef(name="Evie", bundle_id="com.evie.assistant")


from test_computer_control_fast_path import obs


def _obs(frontmost=TEXTEDIT, running=(TEXTEDIT, SAFARI, CALCULATOR), win_count=1):
    app_id = AppIdentity(bundle_id=frontmost.bundle_id, pid=100, name=frontmost.name) if frontmost else AppIdentity(bundle_id="com.apple.finder", pid=50, name="Finder")
    running_apps = [AppIdentity(bundle_id=a.bundle_id, pid=100 + i, name=a.name) for i, a in enumerate(running)]
    win_key = WindowKey(pid=app_id.pid, title_hash="hash1", is_main=True, role="AXWindow", subrole="AXStandardWindow") if win_count > 0 else None
    windows = [WindowKey(pid=app_id.pid, title_hash=f"hash{i}", is_main=(i == 0), role="AXWindow", subrole="AXStandardWindow") for i in range(win_count)]
    return obs(app=app_id, win=win_key, windows=windows, frontmost=app_id, running=running_apps)


# 1. Capability Matrix Test
def test_capability_matrix_has_required_separate_entries():
    required_entries = {
        "activate application": Op.ACTIVATE_APP,
        "focus application": Op.FOCUS,
        "close window": Op.CLOSE_WINDOW,
        "quit application": Op.QUIT_APP,
        "switch application": Op.SWITCH_APP,
        "switch window": Op.SWITCH_WINDOW,
    }
    for name, op in required_entries.items():
        assert name in CAPABILITY_MATRIX
        assert CAPABILITY_MATRIX[name] == op
    assert len(set(required_entries.values())) == 6


# 2. Quit Named Application
def test_quit_named_application():
    o = _obs(frontmost=TEXTEDIT)
    ci = build_input("Quit TextEdit.", o)
    res = fast_path_select(ci)
    assert res.kind == "DECISION"
    assert res.decision["op"] == "quit_app"
    assert res.decision["args"]["bundle_id"] == "com.apple.TextEdit"

    ci_safari = build_input("Quit Safari.", o)
    res_safari = fast_path_select(ci_safari)
    assert res_safari.kind == "DECISION"
    assert res_safari.decision["op"] == "quit_app"
    assert res_safari.decision["args"]["bundle_id"] == "com.apple.Safari"


# 3. Close Current Application
def test_close_current_application():
    o = _obs(frontmost=TEXTEDIT)
    ci = build_input("Close this app.", o)
    res = fast_path_select(ci)
    assert res.kind == "DECISION"
    assert res.decision["op"] == "quit_app"
    assert res.decision["args"]["bundle_id"] == "com.apple.TextEdit"

    ci2 = build_input("Quit the current application.", o)
    res2 = fast_path_select(ci2)
    assert res2.kind == "DECISION"
    assert res2.decision["op"] == "quit_app"
    assert res2.decision["args"]["bundle_id"] == "com.apple.TextEdit"


# 4. Close Current Window
def test_close_current_window():
    o = _obs(frontmost=TEXTEDIT)
    ci = build_input("Close this window.", o)
    res = fast_path_select(ci)
    assert res.kind == "DECISION"
    assert res.decision["op"] == "close_window"


# 5. Close Named Window
def test_close_named_window():
    o = _obs(frontmost=TEXTEDIT)
    ci = build_input("Close that window.", o)
    res = fast_path_select(ci)
    assert res.kind == "DECISION"
    assert res.decision["op"] == "close_window"


# 6. Ambiguous Close Request
def test_ambiguous_close_request():
    app1 = AppRef(name="Editor", bundle_id="com.example.editor1")
    app2 = AppRef(name="Editor Pro", bundle_id="com.example.editor2")
    o = _obs(running=(app1, app2))
    ci = build_input("Close Editor", o)
    res = fast_path_select(ci)
    assert res.kind == "DECISION"
    assert res.decision["status"] == "ASK"
    assert res.decision["reason"] in ("AMBIGUOUS_APP", "AMBIGUOUS_CLOSE_TARGET")


# 7. Attempt to Quit EVIE_SELF Protection
def test_attempt_to_quit_evie_self():
    o_evie = _obs(frontmost=EVIE_APP, running=(EVIE_APP, TEXTEDIT))
    ci1 = build_input("Quit Evie.", o_evie)
    res1 = fast_path_select(ci1)
    assert res1.kind == "DECISION"
    assert res1.decision["status"] == "BLOCKED"
    assert res1.decision["reason"] == "EVIE_SELF_PROTECTION"

    ci2 = build_input("Close Evie.", o_evie)
    res2 = fast_path_select(ci2)
    assert res2.kind == "DECISION"
    assert res2.decision["status"] == "BLOCKED"
    assert res2.decision["reason"] == "EVIE_SELF_PROTECTION"

    ci3 = build_input("Quit this app.", o_evie)
    res3 = fast_path_select(ci3)
    assert res3.kind == "DECISION"
    assert res3.decision["status"] == "BLOCKED"
    assert res3.decision["reason"] == "EVIE_SELF_PROTECTION"

    # Gate policy check for EVIE_SELF
    policy = GatePolicy(evie_bundle_ids=frozenset({"com.evie.assistant"}), evie_pids=frozenset({100}))
    ctx = GateContext(op=Op.QUIT_APP, args=QuitAppArgs(bundle_id="com.evie.assistant"),
                      app=AppIdentity(bundle_id="com.evie.assistant", pid=100),
                      window=None, allowed_apps=frozenset({"com.evie.assistant"}))
    gate_decision = evaluate(ctx, policy)
    assert gate_decision.outcome == GateOutcome.BLOCK
    assert gate_decision.risk == RiskLevel.FORBIDDEN
    assert gate_decision.reason == "EVIE_SELF"


# 8. Multi-Application Quit Request
def test_multi_application_quit_request():
    o = _obs(frontmost=TEXTEDIT)
    ci1 = build_input("Quit all applications", o)
    res1 = fast_path_select(ci1)
    assert res1.kind == "DECISION"
    assert res1.decision["status"] == "ASK"
    assert res1.decision["reason"] == "CONFIRMATION_REQUIRED"

    ci2 = build_input("Close everything", o)
    res2 = fast_path_select(ci2)
    assert res2.kind == "DECISION"
    assert res2.decision["status"] == "ASK"
    assert res2.decision["reason"] == "CONFIRMATION_REQUIRED"


# 9. Verification: Application Exited
def test_verification_application_exited():
    verifier = QuitAppVerifier()
    res_exited = verifier.verify(is_running_after=False)
    assert res_exited.status == VerificationStatus.VERIFIED
    assert res_exited.reason == "APP_TERMINATED"

    res_still_running = verifier.verify(is_running_after=True)
    assert res_still_running.status == VerificationStatus.INCONCLUSIVE
    assert res_still_running.reason == "APP_STILL_RUNNING"


# 10. Verification: Window Closed while Application Remained Running
def test_verification_window_closed_app_remained_running():
    verifier = CloseWindowVerifier()
    res_success = verifier.verify(window_closed=True, app_running=True)
    assert res_success.status == VerificationStatus.VERIFIED
    assert res_success.reason == "WINDOW_CLOSED_APP_RUNNING"

    res_not_closed = verifier.verify(window_closed=False, app_running=True)
    assert res_not_closed.status == VerificationStatus.VERIFIED_NO_CHANGE
    assert res_not_closed.reason == "WINDOW_STILL_PRESENT"

    res_app_terminated = verifier.verify(window_closed=True, app_running=False)
    assert res_app_terminated.status == VerificationStatus.INCONCLUSIVE
    assert res_app_terminated.reason == "APP_TERMINATED_INSTEAD_OF_WINDOW"
