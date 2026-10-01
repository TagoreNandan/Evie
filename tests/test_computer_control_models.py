"""
Computer-control pure core: models, action registry and text provenance (docs/07 Sections 8-10, 12.3).
Unit tests only - no macOS, PyObjC or OS actions.
"""

import pytest
from pydantic import ValidationError

from cc_fixtures import UTTERANCE, observation, window
from services.computer_control import actions
from services.computer_control.models import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    ActivateAppArgs,
    ExecutionStatus,
    GateDecision,
    GateOutcome,
    NoArgs,
    ObservedTarget,
    Op,
    ResolutionMethod,
    ResolvedTarget,
    RiskLevel,
    ScrollArgs,
    Sensitivity,
    SetTextArgs,
    VerificationStatus,
    WindowKey,
    derive_status,
    title_hash,
)
from services.computer_control.provenance import TextSpanError, extract_text, text_for, validate_text_span

ALLOW = GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.LOW, reason="LOW_ALLOWED")


# --- Observation / targets ---

def test_valid_observation_round_trips_without_ax_objects():
    obs = observation()
    assert obs.targets[0].index == 0 and obs.targets[0].obs_id == "obs-1"
    data = obs.model_dump()
    assert "AXUIElement" not in repr(data)
    assert type(obs).model_validate(data) == obs


def test_observation_rejects_targets_from_another_observation():
    obs = observation()
    stray = obs.targets[0].model_copy(update={"obs_id": "obs-2"})
    with pytest.raises(ValidationError):
        type(obs)(**{**obs.model_dump(), "targets": [stray.model_dump()]})


def test_observation_rejects_non_sequential_indexes():
    obs = observation()
    with pytest.raises(ValidationError):
        type(obs)(**{**obs.model_dump(), "targets": [obs.targets[1].model_dump()]})


@pytest.mark.parametrize("model_kwargs", [
    {"cg_window_id": 42},              # CG window ids are unvalidated and deliberately absent
])
def test_window_key_has_no_cg_window_id(model_kwargs):
    with pytest.raises(ValidationError):
        WindowKey(pid=1, role="AXWindow", **model_kwargs)


def test_window_identity_excludes_main_and_focus_state():
    assert window(main=True).identity() == window(main=False, focused=False).identity()


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        ObservedTarget(index=0, obs_id="o", role="AXButton", ax_element="0xdeadbeef")
    with pytest.raises(ValidationError):
        observation(raw_tree={"x": 1})


def test_secure_role_is_flagged_and_never_carries_a_value():
    t = ObservedTarget(index=0, obs_id="o", role="AXTextField", subrole="AXSecureTextField")
    assert t.is_secure and Sensitivity.SECURE_FIELD in t.sensitivity
    with pytest.raises(ValidationError):
        ObservedTarget(index=0, obs_id="o", role="AXSecureTextField", value_summary="hunter2")
    with pytest.raises(ValidationError):
        ObservedTarget(index=0, obs_id="o", role="AXTextField", sensitivity={Sensitivity.SECURE_FIELD},
                       value_summary="x")


def test_frame_is_metadata_only():
    t = ObservedTarget(index=0, obs_id="o", role="AXButton", frame={"x": 1, "y": 2, "width": 3, "height": 4})
    assert t.stable_key() == ObservedTarget(index=5, obs_id="p", role="AXButton").stable_key()


def test_title_hash_is_normalised_and_not_the_title():
    assert title_hash("Evie  Dashboard") == title_hash("evie dashboard")
    assert "evie" not in title_hash("Evie Dashboard")


# --- Arguments ---

@pytest.mark.parametrize("span", [(-1, 3), (5, 2)])
def test_set_text_rejects_invalid_spans(span):
    with pytest.raises(ValidationError):
        SetTextArgs(text_span=span)


def test_set_text_has_no_free_text_field():
    with pytest.raises(ValidationError):
        SetTextArgs(text_span=(0, 4), text="rm -rf /")


def test_scroll_amount_is_bounded_and_args_are_closed():
    with pytest.raises(ValidationError):
        ScrollArgs(direction="down", amount=0.9)
    with pytest.raises(ValidationError):
        ScrollArgs(direction="left")
    with pytest.raises(ValidationError):
        NoArgs(x=10, y=20)


def test_invalid_operation_rejected():
    with pytest.raises(ValueError):
        Op("shell")
    assert not {"shell", "run_command", "terminal", "applescript", "click_at"} & {o.value for o in Op}


# --- ActionRequest ---

def _resolved(obs, i=0):
    return ResolvedTarget(target=obs.targets[i], method=ResolutionMethod.ORDINAL, obs_id=obs.obs_id)


def _request(**over):
    obs = observation()
    base = dict(session_id="s", step=1, op=Op.SET_TEXT, target=_resolved(obs), args=SetTextArgs(text_span=(5, 16)),
                obs_id=obs.obs_id, gate=ALLOW, cancel_epoch=0, timeout_s=10.0)
    base.update(over)
    return ActionRequest(**base)


def test_action_request_valid():
    assert _request().op is Op.SET_TEXT


@pytest.mark.parametrize("over", [
    {"args": ScrollArgs(direction="down")},                     # wrong args model for op
    {"target": None},                                           # target required
    {"gate": GateDecision(outcome=GateOutcome.BLOCK, risk=RiskLevel.HIGH, reason="X")},
    {"obs_id": "obs-9"},                                        # target from another observation
    {"timeout_s": 2.0},                                         # below AX timeout + settle bound
    {"op": Op.OPEN_URL, "target": None, "args": NoArgs()},      # no schema: disabled op cannot form a request
])
def test_action_request_rejects_inconsistent_requests(over):
    with pytest.raises(ValidationError):
        _request(**over)


def test_activate_app_takes_no_target():
    obs = observation()
    with pytest.raises(ValidationError):
        _request(op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.TextEdit"), target=_resolved(obs))
    assert _request(op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id="com.apple.TextEdit"), target=None)


def test_gate_decision_only_allows_low():
    with pytest.raises(ValidationError):
        GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.MEDIUM, reason="X")


# --- ActionResult: API success is never SUCCESS by itself ---

def test_derive_status_requires_verification_for_success():
    E, V = ExecutionStatus, VerificationStatus
    assert derive_status(E.EXECUTED, V.VERIFIED) is ActionStatus.SUCCESS
    assert derive_status(E.EXECUTED, V.NOT_APPLICABLE) is ActionStatus.NOT_VERIFIABLE
    assert derive_status(E.EXECUTED, V.CHANGED_UNSPECIFIED) is ActionStatus.NOT_VERIFIABLE
    assert derive_status(E.EXECUTED, V.VERIFIED_NO_CHANGE) is ActionStatus.FAILED
    assert derive_status(E.ERROR, V.NOT_APPLICABLE) is ActionStatus.FAILED
    assert derive_status(E.TIMEOUT, V.INCONCLUSIVE) is ActionStatus.TIMEOUT
    with pytest.raises(ValueError):
        derive_status(E.NOT_EXECUTED, V.NOT_APPLICABLE)


def _result(**kw):
    base = dict(session_id="s", step=1, op=Op.PRESS, requested_at=1.0)
    base.update(kw)
    return ActionResult(**base)


@pytest.mark.parametrize("kw", [
    {"status": ActionStatus.SUCCESS, "execution": ExecutionStatus.EXECUTED, "verification": VerificationStatus.INCONCLUSIVE},
    {"status": ActionStatus.SUCCESS, "execution": ExecutionStatus.EXECUTED, "verification": VerificationStatus.NOT_APPLICABLE},
    {"status": ActionStatus.BLOCKED, "execution": ExecutionStatus.EXECUTED, "verification": VerificationStatus.NOT_APPLICABLE},
    {"status": ActionStatus.FAILED, "execution": ExecutionStatus.NOT_EXECUTED, "verification": VerificationStatus.NOT_APPLICABLE},
    {"status": ActionStatus.FAILED, "execution": ExecutionStatus.ERROR, "verification": VerificationStatus.NOT_APPLICABLE,
     "retry_permitted": True},
    {"status": ActionStatus.SUCCESS, "execution": ExecutionStatus.EXECUTED, "verification": VerificationStatus.VERIFIED,
     "noop": True},
])
def test_action_result_rejects_inconsistent_status(kw):
    with pytest.raises(ValidationError):
        _result(**kw)


def test_action_result_executed_derives_status_and_noop():
    r = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=VerificationStatus.CHANGED_UNSPECIFIED,
                              fingerprint_changed=False, session_id="s", step=1, op=Op.PRESS, requested_at=1.0)
    assert r.status is ActionStatus.NOT_VERIFIABLE and r.noop
    r = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=VerificationStatus.CHANGED_UNSPECIFIED,
                              fingerprint_changed=True, session_id="s", step=1, op=Op.PRESS, requested_at=1.0)
    assert r.status is ActionStatus.NOT_VERIFIABLE and not r.noop
    r = ActionResult.executed(execution=ExecutionStatus.EXECUTED, verification=VerificationStatus.VERIFIED,
                              fingerprint_changed=True, session_id="s", step=1, op=Op.PRESS, requested_at=1.0)
    assert r.status is ActionStatus.SUCCESS and not r.noop


def test_stale_result_may_be_retried_only_via_new_observation():
    r = _result(status=ActionStatus.STALE, execution=ExecutionStatus.NOT_EXECUTED,
                verification=VerificationStatus.NOT_APPLICABLE, retry_permitted=True)
    assert r.retry_permitted


# --- Registry ---

def test_only_validated_cc1a_primitives_are_enabled():
    assert set(actions.enabled_ops(actions.Phase.CC_1A)) == {Op.ACTIVATE_APP, Op.SET_TEXT, Op.SELECT_TEXT, Op.PRESS, Op.SCROLL,
                                                            Op.PRESS_KEY}


@pytest.mark.parametrize("op", [Op.OPEN_URL, Op.CLOSE_TAB,
                                Op.NEW_WINDOW, Op.NEW_TAB, Op.LAUNCH_APP, Op.NAVIGATE_BACK, Op.SCROLL_INTO_VIEW])
def test_deferred_operations_are_metadata_only(op):
    d = actions.get_definition(op)
    assert d.params_model is None and d.mechanism is None and not actions.is_enabled(op)
    assert not actions.is_enabled(op, actions.Phase.CC_1B)   # a schema/phase alone never enables


def test_press_is_validated_only_for_known_state_segments():
    obs = observation()
    segment, button = obs.targets[3], obs.targets[4]
    assert actions.target_class_validated(Op.PRESS, segment)
    assert not actions.target_class_validated(Op.PRESS, button)
    assert ("AXCheckBox", None) in actions.get_definition(Op.PRESS).pending_targets
    assert actions.get_definition(Op.PRESS).validation is actions.Validation.PARTIALLY_VALIDATED


def test_every_definition_declares_its_metadata():
    for op, d in actions.ACTION_DEFINITIONS.items():
        assert d.op is op and d.risk in RiskLevel and d.enabled_phase in actions.Phase
        if actions.is_enabled(op):
            assert d.mechanism and d.verifier and d.evidence


# --- Text provenance ---

def test_text_span_bounds():
    validate_text_span(UTTERANCE, (0, len(UTTERANCE)))
    validate_text_span(UTTERANCE, (3, 3))
    for bad in [(-1, 2), (4, 2), (0, len(UTTERANCE) + 1), (True, 2)]:
        with pytest.raises(TextSpanError):
            validate_text_span(UTTERANCE, bad)


def test_text_comes_only_from_the_utterance():
    assert extract_text(UTTERANCE, (5, 16)) == "hello world"
    assert text_for(UTTERANCE, SetTextArgs(text_span=(5, 10))) == "hello"

