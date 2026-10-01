"""
Step 16: `focus` stays DISABLED at every production entry point. Its AX mechanism (AXRaise + AXMain + AXFocused)
exists in the executor but has no production-path live evidence, and two identical TextEdit text areas cannot be
told apart by the existing resolver/hardening. Pure tests; no macOS.
"""

from cc_fixtures import TEXT_AREA, TEXTEDIT, FakeClock, observation, window
from services.computer_control.actions import Validation, get_definition, is_enabled
from services.computer_control.chooser import CHOOSER_OPS, build_input
from services.computer_control.executor import LIVE_OPS
from services.computer_control.chooser_boundary import validate_output
from services.computer_control.hardening import harden
from services.computer_control.models import Op, PermissionStatus
from services.computer_control.orchestrator import ORCHESTRATABLE, ActionIntent, Orchestrator, Plan, TargetSelector
from services.computer_control.session import ComputerControlSession, SessionState


def test_focus_is_not_reachable_from_the_chooser_or_the_orchestrator():
    assert Op.FOCUS not in CHOOSER_OPS and Op.FOCUS not in ORCHESTRATABLE


def test_chooser_boundary_rejects_a_focus_decision():
    obs = observation(obs_id="o1")
    v = validate_output({"status": "ACTION", "op": "focus", "target_index": 0, "obs_id": "o1", "args": {}},
                        build_input("focus the document", obs), obs)
    # Step 16A: the registry now disables focus, so the chooser's first check rejects it as DISABLED_OPERATION
    # (the CHOOSER_OPS / UNSUPPORTED_OPERATION layer still sits behind it)
    assert not v.ok and v.error == "UNSUPPORTED_OPERATION: focus"


def test_orchestrator_plan_with_focus_is_blocked_before_any_worker_call():
    utterance = "in TextEdit focus the text"
    s = ComputerControlSession("s", utterance, frozenset({TEXTEDIT.bundle_id}), clock=FakeClock())
    s.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    acts = []
    plan = Plan(utterance=utterance, final_observe_bundle=TEXTEDIT.bundle_id, intents=(
        ActionIntent(op=Op.FOCUS, observe_bundle=TEXTEDIT.bundle_id, target=TargetSelector(role="AXTextArea")),))
    obs = observation(obs_id="o1")
    result = Orchestrator(s, lambda b: type("R", (), {"status": "OK", "observation": obs, "reason": None})(),
                          lambda *a: acts.append(a)).run(plan)
    assert acts == [] and result.final_state is SessionState.BLOCKED
    assert "UNSUPPORTED_OPERATION" in result.stop_reason


def test_registry_marks_focus_validated_and_enabled():
    d = get_definition(Op.FOCUS)
    assert d.validation is Validation.VALIDATED and is_enabled(Op.FOCUS) is True


def test_focus_is_a_live_executor_operation():
    assert Op.FOCUS in LIVE_OPS


def test_two_identical_textedit_text_areas_are_ambiguous_to_resolver_and_hardening():
    w0, w1 = window(doc="doc-a", main=False, focused=False), window(doc="doc-b", main=True, focused=True)
    obs = observation([dict(TEXT_AREA, window_index=0, focused=False), dict(TEXT_AREA, window_index=1)],
                      obs_id="o2", windows=(w0, w1), win=w1)
    r = Orchestrator._resolve(obs, TargetSelector(role="AXTextArea"))
    assert (r.kind, r.reason, r.candidates) == ("ASK", "AMBIGUOUS_TARGET", (0, 1))
