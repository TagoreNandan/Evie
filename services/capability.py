"""
Unified Capability Architecture Foundation for Evie (Step 17).

Defines a clean, semantic, bounded capability layer that plugs into Evie's
Assistant Intelligence + Context architecture.

Security Invariants:
1. Pure METADATA & CONTROLLED RESOLUTION — NO dynamic execution, eval(), exec(),
   subprocess, or shell commands.
2. The LLM receives ONLY semantic capability definitions and returns closed decisions.
   It NEVER receives or creates UniversalAction, coordinates, AX pointers, selectors,
   shell commands, AppleScript, or safety bypass flags.
3. Computer Control is registered ONCE as an existing capability; desktop operations
   retain their authoritative Observe -> Plan -> Safety Gate -> Signed Worker -> Verify pipeline.
4. Personal Memory & Goal Memory remain UNTRUSTED CONTEXT ONLY — never execution authority.
5. Fails closed to CANNOT_PROCEED or UNAVAILABLE on invalid or unauthorized capability requests.
"""

from enum import Enum
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.tool_registry import TOOL_SPECS, ToolSpec, get_executable_tool, get_registered_rows

logger = logging.getLogger(__name__)


class CapabilityCategory(str, Enum):
    COMPUTER_CONTROL = "COMPUTER_CONTROL"
    PERSONAL_MEMORY = "PERSONAL_MEMORY"
    SECURITY_MONITORING = "SECURITY_MONITORING"
    EMAIL = "EMAIL"
    CALENDAR = "CALENDAR"
    RESEARCH = "RESEARCH"
    SYSTEM = "SYSTEM"


class CapabilityOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    ASK = "ASK"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANNOT_PROCEED = "CANNOT_PROCEED"
    BUSY = "BUSY"


class CapabilitySpec(BaseModel):
    """Strict, typed representation of a registered assistant capability."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1, max_length=500)
    category: CapabilityCategory
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    risk_classification: str = Field("LOW", max_length=20)
    is_sensitive: bool = False
    requires_confirmation: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def validate_capability_security(self) -> "CapabilitySpec":
        text = f"{self.capability_id} {self.name}".lower()
        forbidden_terms = (
            "universalaction", "actionrequest", "axuielement", "applescript",
            "subprocess", "exec(", "eval(", "op.", "coords=", "selector=",
            "rm -rf", "click(", "tap(", "executable_path", "gate_override",
            "bypass_safety", "approved_bypass"
        )
        for term in forbidden_terms:
            if term in text:
                raise ValueError(f"Forbidden execution primitive in capability specification: {term}")
        return self


class CapabilityRequest(BaseModel):
    """Closed, bounded capability invocation request model."""
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(..., min_length=1, max_length=64)
    arguments: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request_security(self) -> "CapabilityRequest":
        """Strict security validator preventing execution primitive injection in arguments."""
        if not self.capability_id or not self.capability_id.strip():
            raise ValueError("Capability request must specify a valid capability_id.")

        arg_str = str(self.arguments).lower()
        forbidden_terms = (
            "universalaction", "actionrequest", "axuielement", "applescript",
            "subprocess", "shell", "exec(", "eval(", "op.", "coords=", "selector=",
            "script", "rm -rf", "click(", "tap(", "executable_path", "gate_override",
            "bypass_safety", "approved_bypass"
        )
        for term in forbidden_terms:
            if term in arg_str:
                raise ValueError(f"Forbidden execution primitive in capability request arguments: {term}")
        return self


class CapabilityRegistry:
    """
    Centralized metadata registry for Evie assistant capabilities.
    Does NOT execute capabilities directly and contains NO business logic.
    """

    def __init__(self):
        self._capabilities: Dict[str, CapabilitySpec] = {}

    def register_capability(self, spec: CapabilitySpec) -> None:
        """Registers a capability specification."""
        self._capabilities[spec.capability_id] = spec

    def get_capability(self, capability_id: str) -> Optional[CapabilitySpec]:
        """Retrieves metadata for a registered capability."""
        return self._capabilities.get(capability_id)

    def list_capabilities(self, category: Optional[CapabilityCategory] = None) -> List[CapabilitySpec]:
        """Lists registered capabilities, optionally filtered by category."""
        if category is None:
            return list(self._capabilities.values())
        return [c for c in self._capabilities.values() if c.category == category]

    def is_available(self, capability_id: str, db_path: Optional[str] = None) -> bool:
        """Checks if a capability is registered and enabled."""
        spec = self.get_capability(capability_id)
        if spec is None or not spec.enabled:
            return False
        if db_path is not None:
            executable = get_executable_tool(capability_id, db_path)
            return executable is not None
        return True

    def validate_request(self, request_data: Dict[str, Any]) -> Tuple[bool, Optional[CapabilityRequest], str]:
        """
        Validates an incoming request against registered capabilities.
        Fails closed on unknown capability, invalid arguments, or primitive injection.
        """
        try:
            req = CapabilityRequest(**request_data)
            spec = self.get_capability(req.capability_id)
            if spec is None or not spec.enabled:
                return False, None, f"UNAVAILABLE_CAPABILITY: {req.capability_id}"
            return True, req, "VALID"
        except Exception as e:
            return False, None, f"INVALID_CAPABILITY_REQUEST: {str(e)[:150]}"


_GLOBAL_CAPABILITY_REGISTRY: Optional[CapabilityRegistry] = None


def _map_tool_category(tool_name: str) -> CapabilityCategory:
    """Maps existing tool names onto capability categories."""
    if tool_name == "computer_control" or tool_name == "open_app":
        return CapabilityCategory.COMPUTER_CONTROL
    elif tool_name == "search_memory":
        return CapabilityCategory.PERSONAL_MEMORY
    elif tool_name.startswith("get_") and ("email" in tool_name or "gmail" in tool_name or "promotional" in tool_name or "phishing" in tool_name):
        return CapabilityCategory.EMAIL
    elif tool_name == "propose_gmail_cleanup":
        return CapabilityCategory.EMAIL
    elif tool_name == "propose_calendar_event":
        return CapabilityCategory.CALENDAR
    elif tool_name in ("get_security_score", "list_open_findings", "get_exposure_findings", "get_breach_status", "get_twofa_status", "get_remediation_guide", "get_dashboard_state", "get_secret_findings", "get_secret_findings_by_repository", "get_unreviewed_prs", "get_github_repositories", "get_github_activity", "get_activity_briefing", "get_daily_briefing"):
        return CapabilityCategory.SECURITY_MONITORING
    else:
        return CapabilityCategory.SYSTEM


def build_capability_registry() -> CapabilityRegistry:
    """
    Constructs the single authoritative CapabilityRegistry derived from code tool definitions.
    """
    registry = CapabilityRegistry()
    for name, tool_spec in TOOL_SPECS.items():
        cat = _map_tool_category(name)
        risk = "HIGH" if tool_spec.is_sensitive else ("MEDIUM" if tool_spec.requires_confirmation else "LOW")
        spec = CapabilitySpec(
            capability_id=tool_spec.name,
            name=tool_spec.name,
            description=tool_spec.description,
            category=cat,
            input_schema=tool_spec.parameters_schema(),
            risk_classification=risk,
            is_sensitive=tool_spec.is_sensitive,
            requires_confirmation=tool_spec.requires_confirmation,
            enabled=True,
        )
        registry.register_capability(spec)
    return registry


def get_capability_registry() -> CapabilityRegistry:
    """Retrieves the global singleton CapabilityRegistry."""
    global _GLOBAL_CAPABILITY_REGISTRY
    if _GLOBAL_CAPABILITY_REGISTRY is None:
        _GLOBAL_CAPABILITY_REGISTRY = build_capability_registry()
    return _GLOBAL_CAPABILITY_REGISTRY
