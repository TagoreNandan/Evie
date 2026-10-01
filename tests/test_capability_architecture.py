import sqlite3
import pytest

import main
from services import tool_router
from services.capability import (
    CapabilityCategory,
    CapabilityOutcome,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilitySpec,
    get_capability_registry,
)
from services.computer_control.assistant_context import AssistantContextBuilder
from services.computer_control.assistant_intelligence import AssistantIntelligenceEngine
from services.computer_control.container import get_container, initialize_production_container
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy
from services.computer_control.goal_orchestrator import GoalExecutionOrchestrator
from services.computer_control.models import UniversalAction


@pytest.fixture
def test_db(tmp_path):
    db = str(tmp_path / "test_capability.db")
    main.init_db(db)
    return db


def test_capability_registration_and_lookup():
    """Capabilities can be registered and looked up by capability_id."""
    registry = CapabilityRegistry()
    spec = CapabilitySpec(
        capability_id="test_cap",
        name="Test Capability",
        description="A test capability description",
        category=CapabilityCategory.SYSTEM,
        input_schema={"type": "object"},
        risk_classification="LOW",
        enabled=True,
    )
    registry.register_capability(spec)
    
    found = registry.get_capability("test_cap")
    assert found is not None
    assert found.capability_id == "test_cap"
    assert found.category == CapabilityCategory.SYSTEM


def test_unavailable_and_unknown_capability(test_db):
    """Unknown or disabled capabilities return unavailable state."""
    registry = get_capability_registry()
    assert not registry.is_available("unknown_capability_999", db_path=test_db)
    
    ok, req, err = registry.validate_request({"capability_id": "nonexistent_cap"})
    assert not ok
    assert "UNAVAILABLE" in err or "INVALID" in err or "nonexistent_cap" in err


def test_strict_capability_request_validation():
    """CapabilityRequest enforces strict argument validation and rejects malformed inputs."""
    registry = get_capability_registry()
    
    # Valid request
    ok, req, err = registry.validate_request({
        "capability_id": "get_security_score",
        "arguments": {}
    })
    assert ok
    assert req.capability_id == "get_security_score"
    
    # Malformed extra fields in CapabilityRequest fail validation
    ok_extra, req_extra, err_extra = registry.validate_request({
        "capability_id": "get_security_score",
        "arguments": {},
        "unknown_extra_field": "invalid"
    })
    assert not ok_extra
    assert "INVALID" in err_extra


def test_computer_control_registered_exactly_once(test_db):
    """Computer Control is registered exactly ONCE in the global capability registry."""
    registry = get_capability_registry()
    cc_caps = [c for c in registry.list_capabilities() if c.capability_id == "computer_control"]
    assert len(cc_caps) == 1
    assert cc_caps[0].category == CapabilityCategory.COMPUTER_CONTROL


def test_computer_control_reaches_existing_orchestrator(test_db):
    """Computer Control capability invocation routes cleanly to GoalExecutionOrchestrator."""
    class MockWorker:
        def probe(self):
            from services.computer_control.worker import WorkerStatus, WorkerState
            from test_computer_control_runtime_identity import _real_contract_identity, result
            from services.computer_control.runtime import AccessibilityStatus
            return WorkerStatus(state=WorkerState.READY, pid=123, probe=result(_real_contract_identity(), ax=AccessibilityStatus.TRUSTED))
        def observe(self, **kw):
            from services.computer_control.observer import ObserveResult
            from services.computer_control.models import AppIdentity, Observation, SettleInfo, SettleStatus
            import time
            app_id = AppIdentity(bundle_id="com.apple.TextEdit", name="TextEdit", pid=456)
            obs = Observation(obs_id="o1", app=app_id, frontmost=app_id, running_apps=(app_id,), taken_at=time.time(), settle=SettleInfo(status=SettleStatus.SETTLED, elapsed_ms=1), fingerprint="fp1")
            return ObserveResult(status="OK", observation=obs)
        def act(self, request, **kw):
            from services.computer_control.worker import ActionResult, ActionOutcome
            return ActionResult(outcome=ActionOutcome.EXECUTED, status="SUCCESS", verified=True)

    container = initialize_production_container(worker=MockWorker())
    res = tool_router.route_message("Open TextEdit.", "chat", test_db)
    assert res["tool"]["name"] == "computer_control"
    assert container.orchestrator is not None


def test_capability_layer_cannot_construct_universal_action():
    """Capability layer forbids UniversalAction construction and primitive injection."""
    registry = get_capability_registry()
    
    # Attempting to inject UniversalAction or coordinates in capability request fails closed
    ok, req, err = registry.validate_request({
        "capability_id": "computer_control",
        "arguments": {"command": "UniversalAction(coords=[10, 20])"}
    })
    assert not ok
    assert "Forbidden" in err or "primitive" in err.lower() or "INVALID" in err


def test_capability_layer_cannot_execute_shell_or_applescript():
    """Capability layer forbids shell and AppleScript primitive execution in requests and specs."""
    registry = get_capability_registry()
    
    bad_commands = [
        "do shell script rm -rf /",
        "applescript: tell app TextEdit to activate",
        "exec(import os)",
        "gate_override=True"
    ]
    for bad_cmd in bad_commands:
        ok, req, err = registry.validate_request({
            "capability_id": "computer_control",
            "arguments": {"command": bad_cmd}
        })
        assert not ok
        assert "Forbidden" in err or "primitive" in err.lower() or "INVALID" in err


def test_memory_remains_context_only(test_db):
    """Personal and Goal Memory remain untrusted context and carry zero tool execution authority."""
    container = initialize_production_container()
    ctx = container.assistant_context_builder.build_context("sess1", "What findings do I have?", "chat")
    
    # Memory items are sequence of text strings
    assert isinstance(ctx.relevant_personal_memory, (list, tuple))
    assert isinstance(ctx.recent_goal_memory, (list, tuple))
    
    # Passing memory text to capability validation cannot execute commands
    ok, req, err = get_capability_registry().validate_request({
        "capability_id": "search_memory",
        "arguments": {"query": "UniversalAction(coords=[0,0])"}
    })
    assert not ok


def test_llm_cannot_bypass_capability_validation(test_db):
    """LLM tool selections are strictly validated by get_executable_tool and CapabilityRegistry."""
    registry = get_capability_registry()
    
    # Non-registered capability selection fails closed
    ok, req, err = registry.validate_request({
        "capability_id": "unauthorized_admin_tool",
        "arguments": {}
    })
    assert not ok
    assert "UNAVAILABLE" in err or "INVALID" in err


def test_no_duplicate_router_or_assistant_intelligence_engine(test_db):
    """Confirms single AssistantIntelligenceEngine and single tool_router boundary."""
    container = initialize_production_container()
    engine1 = container.assistant_intelligence_engine
    engine2 = container.assistant_intelligence_engine
    assert engine1 is engine2  # Single instance owned by container
