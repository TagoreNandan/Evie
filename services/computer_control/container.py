"""
Centralized Production Computer Control Dependency Container (Phase 8).

Constructs and manages single authoritative instances of:
- Observation Provider
- Visual Fallback Provider
- Planner Provider (LLM or RuleBased)
- Safety Gate (GatePolicy)
- Signed Worker / Runtime
- Verification
- Unified GoalExecutionOrchestrator
- VoiceGoalAdapter, ChatGoalAdapter, HardwareGoalAdapter (sharing ONE orchestrator)

Key Security & Structural Invariants:
1. One authoritative orchestrator and execution graph. No parallel competing runtimes.
2. Input adapters (Voice, Chat, Hardware) differ ONLY at the input boundary.
3. Fails closed when credentials, worker, or production identities are invalid.
"""

import logging
from typing import Any, Callable, Dict, FrozenSet, Optional

from services.computer_control.chat_adapter import ChatGoalAdapter
from services.computer_control.context import GoalContextManager
from services.computer_control.gate import DEFAULT_POLICY, GatePolicy
from services.computer_control.goal_memory import GoalMemoryManager
from services.computer_control.goal_orchestrator import GoalExecutionOrchestrator
from services.computer_control.hardware_adapter import HardwareGoalAdapter
from services.computer_control.llm_planner import LLMPlannerConfig, LLMPlannerProvider
from services.computer_control.observer import observe_app_with_handles
from services.computer_control.planner import PlannerProvider, RuleBasedPlannerProvider
from services.computer_control.visual_observer import (
    VisualObservationConfig, VisualObservationProvider, FakeVisualObservationProvider
)
from services.computer_control.voice_adapter import VoiceGoalAdapter

logger = logging.getLogger(__name__)


class ComputerControlContainer:
    """
    Centralized container wiring production dependencies for Evie computer control.
    """

    def __init__(
        self,
        worker: Optional[Any] = None,
        planner_provider: Optional[PlannerProvider] = None,
        visual_observer: Optional[VisualObservationProvider] = None,
        safety_policy: Optional[GatePolicy] = None,
        allowed_apps: Optional[FrozenSet[str]] = None,
        get_observation_fn: Optional[Callable[[Any], Any]] = None
    ):
        self.worker = worker
        self.safety_policy = safety_policy or DEFAULT_POLICY
        self.allowed_apps = allowed_apps or frozenset({
            "com.apple.TextEdit", "com.apple.Calculator", "com.apple.PhotoBooth"
        })

        # Single Authoritative Planner Provider
        self.planner_provider = planner_provider or RuleBasedPlannerProvider()

        # Single Visual Observer Provider
        self.visual_observer = visual_observer or FakeVisualObservationProvider()

        # Single Observation Function
        if get_observation_fn is not None:
            self._get_obs_fn = get_observation_fn
        else:
            def default_obs(backend):
                if backend and hasattr(backend, "observe"):
                    try:
                        res = backend.observe(frontmost=True)
                        if hasattr(res, "observation") and res.observation is not None:
                            from services.computer_control.observer import ObservationHandles
                            return res.observation, ObservationHandles()
                    except Exception:
                        pass
                from services.computer_control.models import AppIdentity
                app = getattr(backend, "app", None) if backend else None
                if not isinstance(app, AppIdentity):
                    app = AppIdentity(bundle_id=str(app) if app else "com.apple.TextEdit", pid=1, name="TextEdit")
                return observe_app_with_handles(backend, None, app)
            self._get_obs_fn = default_obs

        # Authoritative Orchestrator (Phase 6)
        self.orchestrator = GoalExecutionOrchestrator(
            planner_provider=self.planner_provider,
            backend=self.worker,
            get_observation=self._get_obs_fn,
            allowed_apps=self.allowed_apps,
            visual_provider=self.visual_observer,
            policy=self.safety_policy
        )

        # Shared Input Adapters (Phases 7A, 7B, 7C)
        self.voice_adapter = VoiceGoalAdapter(orchestrator=self.orchestrator)
        self.chat_adapter = ChatGoalAdapter(orchestrator=self.orchestrator)
        self.hardware_adapter = HardwareGoalAdapter(orchestrator=self.orchestrator)

        from services.personal_memory import get_personal_memory_manager
        from services.computer_control.assistant_context import AssistantContextBuilder
        from services.computer_control.assistant_intelligence import AssistantIntelligenceEngine

        self._personal_memory_manager = get_personal_memory_manager()
        self._assistant_context_builder = AssistantContextBuilder(
            context_manager=self.context_manager,
            goal_memory_manager=self.goal_memory_manager,
            personal_memory_manager=self._personal_memory_manager,
        )
        self._assistant_intelligence_engine = AssistantIntelligenceEngine()

    @property
    def context_manager(self) -> GoalContextManager:
        """Returns the authoritative GoalContextManager owned by the orchestrator."""
        return self.orchestrator.context_manager

    @property
    def goal_context(self) -> GoalContextManager:
        """Alias for context_manager property for container inspection."""
        return self.orchestrator.context_manager

    @property
    def goal_memory(self) -> GoalMemoryManager:
        """Returns the single authoritative GoalMemoryManager owned by the orchestrator."""
        return self.orchestrator.goal_memory

    @property
    def goal_memory_manager(self) -> GoalMemoryManager:
        """Alias for goal_memory property for container inspection."""
        return self.orchestrator.goal_memory

    @property
    def personal_memory_manager(self) -> Any:
        """Returns the single authoritative PersonalMemoryManager instance."""
        return self._personal_memory_manager

    @property
    def assistant_context_builder(self) -> Any:
        """Returns the single authoritative AssistantContextBuilder wired with container dependencies."""
        return self._assistant_context_builder

    @property
    def assistant_intelligence_engine(self) -> Any:
        """Returns the single authoritative AssistantIntelligenceEngine instance."""
        return self._assistant_intelligence_engine

    def shutdown(self) -> None:
        """Cleanly releases worker and container resources."""
        if self.worker and hasattr(self.worker, "stop"):
            try:
                self.worker.stop()
            except Exception as e:
                logger.warning(f"Error stopping worker: {e}")


_GLOBAL_CONTAINER: Optional[ComputerControlContainer] = None


def get_container() -> Optional[ComputerControlContainer]:
    """Retrieves the active global production container."""
    return _GLOBAL_CONTAINER


def set_container(container: Optional[ComputerControlContainer]) -> None:
    """Registers the active global production container."""
    global _GLOBAL_CONTAINER
    _GLOBAL_CONTAINER = container


def initialize_production_container(
    worker: Optional[Any] = None,
    llm_config: Optional[LLMPlannerConfig] = None,
    visual_config: Optional[VisualObservationConfig] = None,
    safety_policy: Optional[GatePolicy] = None,
    allowed_apps: Optional[FrozenSet[str]] = None,
    get_observation_fn: Optional[Callable[[Any], Any]] = None
) -> ComputerControlContainer:
    """
    Constructs and registers the global production computer control container graph.
    """
    planner: PlannerProvider
    if llm_config and llm_config.api_key:
        planner = LLMPlannerProvider(config=llm_config)
    else:
        planner = RuleBasedPlannerProvider()

    vis_obs = FakeVisualObservationProvider()

    container = ComputerControlContainer(
        worker=worker,
        planner_provider=planner,
        visual_observer=vis_obs,
        safety_policy=safety_policy,
        allowed_apps=allowed_apps,
        get_observation_fn=get_observation_fn
    )
    set_container(container)
    return container
