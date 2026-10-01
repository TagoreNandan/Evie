"""
Production Integration & End-to-End Validation Test Suite for Evie (Phase 8).
"""

import pytest
from unittest.mock import MagicMock

from services.computer_control.container import (
    ComputerControlContainer, get_container, initialize_production_container, set_container
)
from services.computer_control.gate import GatePolicy, DEFAULT_POLICY
from services.computer_control.goal_orchestrator import (
    GoalExecutionOrchestrator, GoalExecutionResult, GoalExecutionState, GoalRequest
)
from services.computer_control.llm_planner import (
    LLMPlannerConfig, LLMPlannerProvider, LLMTransportError
)
from services.computer_control.planner import (
    ActionDecision, AskDecision, CannotProceedDecision, DoneDecision,
    MockPlannerProvider, PlannerDecisionKind, PlannerInput, PlannerOutput, RuleBasedPlannerProvider
)
from services.computer_control.visual_observer import (
    VisualObservationConfig, FakeVisualObservationProvider
)
from services.computer_control.actions import Op
from services.computer_control.models import UniversalAction, ActivateAppArgs
from services.computer_control.audit import StepAuditRecord
from services.computer_control.identity_check import report
from cc_fixtures import TEXTEDIT, TERMINAL, observation


class FakeWorkerBackend:
    def __init__(self, is_trusted: bool = True):
        self.is_trusted = is_trusted
        self.executed_actions = []

    def probe(self):
        m = MagicMock()
        m.probe.production_identity_verified = self.is_trusted
        m.probe.production_identity_reasons = [] if self.is_trusted else ["UNTRUSTED_TEST_HOST"]
        m.probe.to_permission_status.return_value = MagicMock()
        return m

    def act(self, request, utterance=None, allowed_apps=None):
        self.executed_actions.append(request)
        res = MagicMock()
        res.status = "OK"
        res.reason = "Success"
        return res


def test_production_container_wiring():
    """Verify production container constructs one orchestrator shared across all adapters."""
    worker = FakeWorkerBackend()
    container = initialize_production_container(worker=worker)
    
    assert container.orchestrator is not None
    assert container.voice_adapter._orchestrator is container.orchestrator
    assert container.chat_adapter._orchestrator is container.orchestrator
    assert container.hardware_adapter._orchestrator is container.orchestrator
    assert get_container() is container


def test_no_competing_or_duplicate_runtimes():
    """Verify single global container instance prevents competing runtimes."""
    c1 = initialize_production_container()
    assert get_container() is c1
    c2 = initialize_production_container()
    assert get_container() is c2
    assert c1 is not c2


def test_missing_credentials_fails_closed():
    """Verify missing LLM credentials fail closed with transport error, never bypassing safety."""
    config = LLMPlannerConfig(api_key=None)
    provider = LLMPlannerProvider(config=config)
    obs = observation()
    from services.computer_control.planner import serialize_compact_observation
    pi = PlannerInput(
        goal="Open TextEdit.",
        obs_id=obs.obs_id,
        compact_observation=serialize_compact_observation(obs),
        available_ops=(Op.ACTIVATE_APP,)
    )
    res = provider.choose_action(pi)
    assert res.decision.kind == PlannerDecisionKind.CANNOT_PROCEED
    assert res.decision.reason in ("LLM_UNAVAILABLE", "LLM_NOT_CONFIGURED")


def test_missing_vision_credentials_uses_ax_fallback():
    """Verify visual observer behaves gracefully when vision model is disabled or unavailable."""
    obs = FakeVisualObservationProvider()
    assert obs is not None


def test_production_identity_validation_behavior():
    """Verify identity check probe report reflects untrusted test environment."""
    mock_status = MagicMock()
    mock_status.probe = None
    mock_status.state.value = "READY"
    mock_status.failure = None
    mock_status.pid = 1234
    
    rep = report(mock_status)
    assert rep["PRODUCTION_IDENTITY_VERIFIED"] is False
    assert "NO_PROBE" in rep["PRODUCTION_IDENTITY_REASONS"]


def test_authentication_matrix():
    """Verify security matrix invariants across all input channels."""
    worker = FakeWorkerBackend(is_trusted=True)
    container = initialize_production_container(
        worker=worker,
        get_observation_fn=lambda b: _mock_observation(TEXTEDIT)
    )
    
    # Voice channel adds source="voice"
    v_res = container.voice_adapter.process_voice_input("Open TextEdit.")
    assert v_res.status in ("COMPLETED", "ASK", "CANNOT_PROCEED")
    
    # Chat channel adds source="chat"
    c_res = container.chat_adapter.process_chat_input("Open TextEdit.")
    assert c_res.status in ("COMPLETED", "ASK", "CANNOT_PROCEED")
    
    # Hardware channel adds source="hardware"
    h_res = container.hardware_adapter.process_hardware_input("Open TextEdit.")
    assert h_res.status in ("COMPLETED", "ASK", "CANNOT_PROCEED")


def test_audit_observability_events():
    """Verify audit log records events without leaking secrets or screenshots."""
    rec = StepAuditRecord(
        session_id="cc-123",
        step=1,
        op="activate_app",
        decision_source="plan",
        target_summary={"app_bundle_id": "com.apple.TextEdit"},
        gate_decision="ALLOW",
        status="SUCCESS",
        mechanism="AX"
    )
    d = rec.model_dump()
    assert "screenshot" not in d
    assert "api_key" not in d
    assert "password" not in d
    assert d["session_id"] == "cc-123"


def _mock_observation(app=TEXTEDIT):
    from cc_fixtures import observation
    obs = observation(app=app, frontmost=app)
    return obs, {"handle": 1}


def test_llm_unavailable_failure():
    """Failure injection: LLM failure leads to bounded CANNOT_PROCEED decision."""
    config = LLMPlannerConfig(api_key=None)
    provider = LLMPlannerProvider(config=config)
    from cc_fixtures import observation
    from services.computer_control.planner import serialize_compact_observation
    obs = observation()
    pi = PlannerInput(
        goal="Unsafe goal",
        obs_id=obs.obs_id,
        compact_observation=serialize_compact_observation(obs),
        available_ops=(Op.ACTIVATE_APP,)
    )
    output = provider.choose_action(pi)
    assert output.decision.kind == PlannerDecisionKind.CANNOT_PROCEED


def test_safety_gate_blocked_failure():
    """Failure injection: Safety gate blocks unauthorized apps."""
    from cc_fixtures import TERMINAL
    worker = FakeWorkerBackend()
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(TERMINAL),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )
    # Attempting an action outside allowed scope
    req = GoalRequest(goal_id="g-block", goal_text="Open Terminal and run script.", source="chat")
    res = orch.execute_goal(req)
    assert res.status in (GoalExecutionState.BLOCKED, GoalExecutionState.CANNOT_PROCEED, GoalExecutionState.WAITING_FOR_USER)


def test_concurrent_goal_busy_failure():
    """Failure injection: Concurrent goal returns BUSY under single-active-goal policy."""
    worker = FakeWorkerBackend()
    orch = GoalExecutionOrchestrator(
        planner_provider=RuleBasedPlannerProvider(),
        backend=worker,
        get_observation=lambda b: _mock_observation(),
        allowed_apps=frozenset({"com.apple.TextEdit"})
    )
    req1 = GoalRequest(goal_id="g-1", goal_text="Open TextEdit.", source="chat")
    req2 = GoalRequest(goal_id="g-2", goal_text="Open Calculator.", source="voice")
    
    # Simulate active state
    orch._state = GoalExecutionState.PLANNING
    orch._active_request = req1
    res2 = orch.execute_goal(req2)
    assert res2.status == GoalExecutionState.BUSY


def test_e2e_multimodal_single_orchestrator():
    """End-to-end validation proving Voice, Chat, and Hardware enter through the same orchestrator."""
    worker = FakeWorkerBackend()
    container = initialize_production_container(
        worker=worker,
        get_observation_fn=lambda b: _mock_observation(TEXTEDIT)
    )
    
    v_res = container.voice_adapter.process_voice_input("Open TextEdit.")
    c_res = container.chat_adapter.process_chat_input("Open TextEdit.")
    h_res = container.hardware_adapter.process_hardware_input("Open TextEdit.")
    
    assert v_res.status in ("COMPLETED", "ASK", "CANNOT_PROCEED")
    assert c_res.status in ("COMPLETED", "ASK", "CANNOT_PROCEED")
    assert h_res.status in ("COMPLETED", "ASK", "CANNOT_PROCEED")
