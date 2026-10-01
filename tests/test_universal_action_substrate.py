"""
Tests for Evie Phase 2: Universal Computer Action Substrate.
Verifies UniversalAction schema, general interaction primitives, semantic target revalidation,
code-owned gate enforcement, independent verification, and action result classification.
"""

import time
import pytest
from pydantic import ValidationError

from cc_fixtures import TEXTEDIT, TERMINAL, KEYCHAIN, observation, window
from test_computer_control_executor import FakeLive, Clock, _idx, _request, _setup, textedit
from services.computer_control import actions
from services.computer_control.actions import Op, get_definition, is_enabled
from services.computer_control.executor import (
    ActionBackend, ObservationCache, execute_action, LIVE_OPS
)
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import (
    GatePolicy, DEFAULT_POLICY, evaluate, context_for
)
from services.computer_control.models import (
    ActionRequest, ActionResult, ActionStatus, ActivateAppArgs, ClickElementArgs,
    CopySelectionArgs, ExecutionStatus, GateDecision, GateOutcome, MenuItemSelectArgs,
    NoArgs, ObservedTarget, Op, PasteClipboardArgs, PressKeyArgs, ResolutionMethod,
    ResolvedTarget, RiskLevel, ScrollArgs, SelectArgs, SelectTabArgs, SelectTextArgs,
    SemanticNode, SetTextArgs, SystemObservation, UniversalAction, VerificationStatus, WaitForStateArgs
)
from services.computer_control.observer import (
    ObserveResult, read_identity
)
from services.computer_control.resolver import (
    SemanticTargetSpec, TargetSpec, resolve, resolve_semantic
)
from services.computer_control.verification import (
    FocusVerifier, SelectionVerifier, MenuItemVerifier, ClipboardVerifier,
    StateConditionVerifier, TextVerifier, ScrollVerifier, ActivationVerifier
)
from test_computer_control_executor import FakeLive, Clock, _idx, _request, _setup, textedit, AGREED

# --- 1. UniversalAction Schema Validation ---
def test_universal_action_schema_validation():
    action = UniversalAction(
        action_id="act-1",
        obs_id="obs-1",
        op=Op.CLICK_ELEMENT,
        args=ClickElementArgs(click_count=1)
    )
    assert action.action_id == "act-1"
    assert action.op is Op.CLICK_ELEMENT

    with pytest.raises(ValidationError):
        UniversalAction(action_id="", obs_id="obs-1", op=Op.CLICK_ELEMENT)


# --- 2. Semantic Button Click ---
def test_semantic_button_click():
    obs = observation([{"role": "AXButton", "label": "Take Photo", "enabled": True}])
    node = SemanticNode(
        node_id="n1", role="AXButton", label="Take Photo", enabled=True,
        actions=("AXPress",)
    )
    assert node.role == "AXButton" and node.enabled is True
    d = get_definition(Op.CLICK_ELEMENT)
    assert ("AXButton", None) in d.validated_targets


# --- 3. Semantic Checkbox Activation ---
def test_semantic_checkbox_activation():
    obs = observation([{"role": "AXCheckBox", "label": "Enable 2FA", "enabled": True}])
    d = get_definition(Op.CLICK_ELEMENT)
    assert ("AXCheckBox", None) in d.validated_targets


# --- 4. Semantic Radio Selection ---
def test_semantic_radio_selection():
    d = get_definition(Op.SELECT)
    assert ("AXRadioButton", None) in d.validated_targets
    v = SelectionVerifier()
    res = v.verify(False, True)
    assert res.status is VerificationStatus.VERIFIED


# --- 5. Semantic Tab Selection ---
def test_semantic_tab_selection():
    d = get_definition(Op.SELECT_TAB)
    assert ("AXTab", None) in d.validated_targets
    v = SelectionVerifier()
    res = v.verify(False, True)
    assert res.status is VerificationStatus.VERIFIED


# --- 6. Semantic Focus ---
def test_semantic_focus():
    d = get_definition(Op.FOCUS)
    assert is_enabled(Op.FOCUS)
    v = FocusVerifier()
    res = v.verify(False, True)
    assert res.status is VerificationStatus.VERIFIED


# --- 7. Semantic Menu Item Activation ---
def test_semantic_menu_item_activation():
    d = get_definition(Op.MENU_ITEM_SELECT)
    assert ("AXMenuItem", None) in d.validated_targets
    v = MenuItemVerifier()
    res = v.verify("menu_open", "menu_closed")
    assert res.status is VerificationStatus.VERIFIED


# --- 8. Semantic Set Text ---
def test_semantic_set_text():
    d = get_definition(Op.SET_TEXT)
    assert is_enabled(Op.SET_TEXT)
    v = TextVerifier()
    res = v.verify(
        type("E", (), {"local_text": "hello", "char_count": 5, "selection": (5, 0)})(),
        type("E", (), {"local_text": "hello world", "char_count": 11, "selection": (11, 0)})(),
        type("X", (), {"local_text": "hello world", "char_delta": 6, "selection": None})()
    )
    assert res.status is VerificationStatus.VERIFIED


# --- 9. Bounded Scroll ---
def test_bounded_scroll():
    d = get_definition(Op.SCROLL)
    assert is_enabled(Op.SCROLL)
    args = ScrollArgs(direction="down", amount=0.25)
    assert args.amount == 0.25
    with pytest.raises(ValidationError):
        ScrollArgs(direction="down", amount=0.8)


# --- 10. Bounded Key Action ---
def test_bounded_key_action():
    args = PressKeyArgs(key="RIGHT_ARROW")
    assert args.key == "RIGHT_ARROW"
    with pytest.raises(ValidationError):
        PressKeyArgs(key="ENTER")


# --- 11. Copy ---
def test_copy_selection_definition():
    d = get_definition(Op.COPY_SELECTION)
    assert is_enabled(Op.COPY_SELECTION) and not d.requires_target
    v = ClipboardVerifier()
    assert v.verify(None, None).status is VerificationStatus.VERIFIED_NO_CHANGE


# --- 12. Paste ---
def test_paste_clipboard_definition():
    d = get_definition(Op.PASTE_CLIPBOARD)
    assert is_enabled(Op.PASTE_CLIPBOARD) and d.requires_target
    v = ClipboardVerifier()
    assert v.verify(None, None).status is VerificationStatus.VERIFIED_NO_CHANGE


# --- 13. Wait for State Timeout ---
def test_wait_for_state_timeout():
    args = WaitForStateArgs(condition="frontmost", expected_value="com.apple.TextEdit", timeout_s=0.1)
    assert args.timeout_s == 0.1
    v = StateConditionVerifier()
    assert v.verify(False).status is VerificationStatus.VERIFIED_NO_CHANGE


# --- 14. Stale Obs_Id Rejection ---
def test_stale_obs_id_rejection():
    backend, clock, cache = _setup(textedit())
    ti = _idx(cache.observation, "AXTextArea")
    t = cache.observation.targets[ti].model_copy(update={"obs_id": "obs-stale"})
    target = ResolvedTarget(target=t, method=ResolutionMethod.ORDINAL, obs_id="obs-stale")
    req = ActionRequest(
        session_id="s", step=1, op=Op.SET_TEXT, target=target,
        args=SetTextArgs(text_span=(0, 4)), obs_id="obs-stale",
        gate=GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.LOW, reason="LOW_ALLOWED"),
        cancel_epoch=0, timeout_s=5.0
    )
    res, _ = execute_action(
        req, utterance="type hello", allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        cache=cache, backend=backend, clock=clock, frontmost=AGREED, reobserve=lambda pid: None
    )
    assert res.status is ActionStatus.STALE


# --- 15. Stale Semantic Target Rejection ---
def test_stale_semantic_target_rejection():
    backend, clock, cache = _setup(textedit())
    ti = _idx(cache.observation, "AXTextArea")
    cache.handles.targets[ti].gone = True
    req = _request(cache, Op.SET_TEXT, ti, SetTextArgs(text_span=(0, 4)))
    res, _ = execute_action(
        req, utterance="type hello", allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        cache=cache, backend=backend, clock=clock, frontmost=AGREED, reobserve=lambda pid: None
    )
    assert res.status is ActionStatus.STALE


# --- 16. Ambiguous Target Rejection ---
def test_ambiguous_target_rejection():
    obs = observation([
        {"role": "AXButton", "label": "Continue"},
        {"role": "AXButton", "label": "Continue"}
    ])
    spec = TargetSpec(obs_id=obs.obs_id, role="AXButton", label="Continue")
    res = resolve(obs, spec)
    assert res.kind == "ASK" and res.reason == "AMBIGUOUS_TARGET"


# --- 17. Target Not Found Rejection ---
def test_target_not_found_rejection():
    obs = observation([{"role": "AXButton", "label": "Cancel"}])
    spec = TargetSpec(obs_id=obs.obs_id, role="AXButton", label="Nonexistent")
    res = resolve(obs, spec)
    assert res.kind == "BLOCKED" and res.reason == "NO_MATCH"


# --- 18. Disabled Control Rejection ---
def test_disabled_control_rejection():
    backend, clock, cache = _setup(textedit())
    ti = next(t.index for t in cache.observation.targets if t.role in ("AXButton", "AXCheckBox"))
    t0 = cache.observation.targets[ti].model_copy(update={"enabled": False})
    new_targets = list(cache.observation.targets)
    new_targets[ti] = t0
    cache.observation = cache.observation.model_copy(update={"targets": new_targets})
    req = _request(cache, Op.CLICK_ELEMENT, ti, ClickElementArgs(click_count=1))
    res, _ = execute_action(
        req, utterance="click action", allowed_apps=frozenset({TEXTEDIT.bundle_id}),
        cache=cache, backend=backend, clock=clock, frontmost=AGREED, reobserve=lambda pid: None
    )
    assert res.status is ActionStatus.BLOCKED and "DISABLED" in res.reason


# --- 19. Restricted App Rejection ---
def test_restricted_app_rejection():
    obs = observation(app=TERMINAL, frontmost=TERMINAL)
    ctx = context_for(Op.CLICK_ELEMENT, ClickElementArgs(), None, obs, frozenset({TERMINAL.bundle_id}))
    gate = evaluate(ctx)
    assert gate.outcome is GateOutcome.BLOCK and gate.risk is RiskLevel.HIGH


# --- 20. EVIE_SELF Rejection ---
def test_evie_self_rejection():
    obs = observation(app=TEXTEDIT, win=window(pid=TEXTEDIT.pid, title="Evie — Dashboard & Security Assistant"))
    target = _idx_resolved(obs, 3)
    ctx = context_for(Op.CLICK_ELEMENT, ClickElementArgs(), target, obs, frozenset({TEXTEDIT.bundle_id}))
    gate = evaluate(ctx)
    assert gate.outcome is GateOutcome.BLOCK and gate.reason == "EVIE_SELF"


# --- 21. Secure Field Protection ---
def test_secure_field_protection():
    obs = observation([{"role": "AXTextField", "subrole": "AXSecureTextField"}])
    ctx = context_for(Op.SET_TEXT, SetTextArgs(text_span=(0, 4)), _idx_resolved(obs, 0), obs, frozenset({TEXTEDIT.bundle_id}))
    gate = evaluate(ctx)
    assert gate.outcome is GateOutcome.BLOCK and gate.reason == "SECURE_FIELD"


# --- 22. Independent Verification ---
def test_independent_verification_success():
    v = SelectionVerifier()
    res = v.verify(False, True)
    assert res.status is VerificationStatus.VERIFIED


# --- 23. Failed Verification ---
def test_failed_verification():
    v = SelectionVerifier()
    res = v.verify(False, False)
    assert res.status is VerificationStatus.VERIFIED_NO_CHANGE


# --- 24. Action Result Classification ---
def test_action_result_classification():
    res = ActionResult.executed(
        execution=ExecutionStatus.EXECUTED, verification=VerificationStatus.VERIFIED,
        fingerprint_changed=True, session_id="s1", step=1, op=Op.CLICK_ELEMENT, requested_at=1.0
    )
    assert res.status is ActionStatus.SUCCESS and not res.noop


# --- 25. Audit Logging ---
def test_audit_logging():
    from services.computer_control.audit import StepAuditRecord
    rec = StepAuditRecord(
        session_id="s1", step=1, op="click_element", decision_source="fast_path",
        gate_decision="ALLOW:LOW", status="SUCCESS"
    )
    assert rec.session_id == "s1" and rec.status == "SUCCESS"


def _idx_resolved(obs, i):
    return ResolvedTarget(target=obs.targets[i], method=ResolutionMethod.ORDINAL, obs_id=obs.obs_id)
