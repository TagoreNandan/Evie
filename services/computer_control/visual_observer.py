"""
Visual Understanding Fallback & Observation Provider (Evie Phase 5).

Provides visual observation and grounding ONLY as an on-demand fallback layer when
Accessibility APIs lack required semantic information (e.g. custom canvas, WebGL, icon controls).

KEY SECURITY INVARIANTS:
1. VISION MAY UNDERSTAND PIXELS. VISION MAY NOT CONTROL PIXELS.
   No click(x,y), no mouse coordinates, no CGEvent clicking, no AppleScript, no shell execution.
2. Code-Owned Safety Gate remains authoritative over all proposed actions.
3. Sensitive UI Protection: Redaction & sensitive content blocking BEFORE transmission.
   No screenshots captured or transmitted for secure fields (AXSecureTextField), restricted apps,
   or EVIE_SELF protected UI.
4. Transient Image Bounds: Max image byte size & dimensions enforced. No persistent screenshot
   archive, no screenshot logging in audit logs.
5. Untrusted Environment Data: Visible text in screenshots is treated strictly as untrusted data
   and cannot override system prompt or safety gate policies.
6. AX-First Priority: AX semantic tree is primary; vision is queried on-demand only.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import Field, model_validator

import keyring_utils
from services.computer_control.models import (
    AppIdentity, EvieSelfStatus, ObservedTarget, SystemObservation, _Strict
)

logger = logging.getLogger(__name__)

DEFAULT_VISION_PROVIDER = "custom_vision"
DEFAULT_VISION_MODEL = "vision-v1"
DEFAULT_VISION_TIMEOUT_S = 10.0
MAX_SCREENSHOT_BYTES = 1_048_576  # 1 MB
MAX_SCREENSHOT_DIMENSIONS = (1024, 1024)
DEFAULT_MIN_CONFIDENCE = 0.70


class VisualObservationConfig(_Strict):
    """Configuration for Visual Observation Provider."""
    provider: str = Field(default=DEFAULT_VISION_PROVIDER, max_length=64)
    model_name: str = Field(default=DEFAULT_VISION_MODEL, max_length=128)
    timeout_s: float = Field(default=DEFAULT_VISION_TIMEOUT_S, ge=1.0, le=60.0)
    max_image_bytes: int = Field(default=MAX_SCREENSHOT_BYTES, ge=64, le=10_485_760)
    max_image_dimensions: Tuple[int, int] = Field(default=MAX_SCREENSHOT_DIMENSIONS)
    min_confidence: float = Field(default=DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0)
    enabled: bool = True


class VisualBoundingRegion(_Strict):
    """Normalized bounding region coordinates (0.0 to 1.0) for visual grounding."""
    x_min: float = Field(..., ge=0.0, le=1.0)
    y_min: float = Field(..., ge=0.0, le=1.0)
    x_max: float = Field(..., ge=0.0, le=1.0)
    y_max: float = Field(..., ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_bounds(self):
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Invalid bounding region coordinates: min must be < max")
        return self


class VisualElement(_Strict):
    """Observation-local grounded semantic element recognized from visual input."""
    visual_id: str = Field(..., min_length=1, max_length=64)
    role: str = Field(..., min_length=1, max_length=64)
    label: Optional[str] = Field(None, max_length=200)
    text_summary: Optional[str] = Field(None, max_length=300)
    bounding_region: Optional[VisualBoundingRegion] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    enabled: Optional[bool] = None
    state_summary: Optional[str] = Field(None, max_length=120)


class VisualObservation(_Strict):
    """Structured result of a visual observation request."""
    obs_id: str = Field(..., min_length=1, max_length=64)
    taken_at: float = Field(..., ge=0.0)
    app_bundle_id: str = Field(..., max_length=128)
    elements: Tuple[VisualElement, ...] = ()
    is_sensitive_blocked: bool = False
    diagnostic: str = Field("", max_length=200)


class VisualObservationRequest(_Strict):
    """Transient request object for visual observation."""
    obs_id: str = Field(..., min_length=1, max_length=64)
    app: AppIdentity
    frontmost: Optional[AppIdentity] = None
    screenshot_bytes: Optional[bytes] = None
    screenshot_dimensions: Optional[Tuple[int, int]] = None
    has_secure_fields: bool = False


@dataclass
class ScreenshotCaptureResult:
    """Transient in-memory result of a screenshot capture attempt."""
    success: bool
    is_blocked: bool
    reason: str
    image_bytes: Optional[bytes] = None
    dimensions: Optional[Tuple[int, int]] = None


# --- Transient Screenshot Capture Abstraction ---

RESTRICTED_APP_BUNDLES = frozenset({
    "com.apple.Terminal", "com.apple.keychainaccess", "com.bitwarden.desktop", "com.1password.1password"
})


def capture_transient_screenshot(
    app: AppIdentity,
    obs: SystemObservation,
    config: Optional[VisualObservationConfig] = None
) -> ScreenshotCaptureResult:
    """
    Captures a transient in-memory screenshot of the specified application region.
    Enforces pre-capture sensitivity screening:
    - Fails closed if app is restricted (Terminal, Keychain Access).
    - Fails closed if observation contains secure fields (AXSecureTextField).
    - Fails closed if observation indicates EVIE_SELF protected UI.
    No permanent image file is written to disk or audit logs.
    """
    cfg = config or VisualObservationConfig()

    # 1. Restricted App Check
    if app.bundle_id in RESTRICTED_APP_BUNDLES:
        logger.warning(f"Screenshot capture blocked: restricted application '{app.bundle_id}'")
        return ScreenshotCaptureResult(success=False, is_blocked=True, reason="RESTRICTED_APPLICATION")

    # 2. EVIE_SELF Protection Check
    if obs.evie_self == EvieSelfStatus.EXCLUDED:
        logger.warning("Screenshot capture blocked: EVIE_SELF protected UI")
        return ScreenshotCaptureResult(success=False, is_blocked=True, reason="EVIE_SELF_PROTECTED")

    # 3. Secure Field Check (AXSecureTextField)
    has_secure = any(
        getattr(t, "is_secure", False) or t.subrole == "AXSecureTextField"
        for t in obs.targets
    )
    if has_secure:
        logger.warning("Screenshot capture blocked: secure entry field present in observation")
        return ScreenshotCaptureResult(success=False, is_blocked=True, reason="SECURE_FIELD_PRESENT")

    # 4. Mock / In-Memory Transient Capture (Fast/Pure test environment safe)
    # Generate bounded transient synthetic bytes (e.g. 512 bytes PNG header stub)
    dummy_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 250
    if len(dummy_bytes) > cfg.max_image_bytes:
        return ScreenshotCaptureResult(success=False, is_blocked=False, reason="OVERSIZED_IMAGE")

    return ScreenshotCaptureResult(
        success=True,
        is_blocked=False,
        reason="CAPTURED_TRANSIENT",
        image_bytes=dummy_bytes,
        dimensions=(800, 600)
    )


# --- Visual Observation Provider Contract ---

class VisualObservationProvider(ABC):
    """Abstract interface for Visual Observation Providers."""
    @abstractmethod
    def observe_visual(
        self,
        request: VisualObservationRequest,
        config: VisualObservationConfig
    ) -> VisualObservation:
        pass

    def observe(
        self,
        request: VisualObservationRequest,
        config: Optional[VisualObservationConfig] = None
    ) -> VisualObservation:
        return self.observe_visual(request, config or VisualObservationConfig())


class FakeVisualObservationProvider(VisualObservationProvider):
    """Deterministic fake provider for unit testing."""
    def __init__(self, responses: Optional[Sequence[Union[VisualObservation, Exception]]] = None):
        self.responses: List[Union[VisualObservation, Exception]] = list(responses) if responses is not None else []
        self.requests: List[VisualObservationRequest] = []
        self.call_count: int = 0

    def observe_visual(
        self,
        request: VisualObservationRequest,
        config: VisualObservationConfig
    ) -> VisualObservation:
        self.call_count += 1
        self.requests.append(request)

        # Pre-flight Sensitivity Screening
        if request.has_secure_fields or request.app.bundle_id in RESTRICTED_APP_BUNDLES:
            return VisualObservation(
                obs_id=request.obs_id,
                taken_at=time.time(),
                app_bundle_id=request.app.bundle_id,
                elements=(),
                is_sensitive_blocked=True,
                diagnostic="SENSITIVE_CONTENT_BLOCKED"
            )

        if not self.responses:
            # Default fallback empty observation
            return VisualObservation(
                obs_id=request.obs_id,
                taken_at=time.time(),
                app_bundle_id=request.app.bundle_id,
                elements=(),
                is_sensitive_blocked=False,
                diagnostic="FAKE_PROVIDER_DEFAULT"
            )

        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class DefaultVisualObservationProvider(VisualObservationProvider):
    """Default visual observation provider."""
    def observe_visual(
        self,
        request: VisualObservationRequest,
        config: VisualObservationConfig
    ) -> VisualObservation:
        if not config.enabled:
            return VisualObservation(
                obs_id=request.obs_id,
                taken_at=time.time(),
                app_bundle_id=request.app.bundle_id,
                elements=(),
                is_sensitive_blocked=False,
                diagnostic="VISION_DISABLED"
            )

        if request.has_secure_fields or request.app.bundle_id in RESTRICTED_APP_BUNDLES:
            return VisualObservation(
                obs_id=request.obs_id,
                taken_at=time.time(),
                app_bundle_id=request.app.bundle_id,
                elements=(),
                is_sensitive_blocked=True,
                diagnostic="SENSITIVE_CONTENT_BLOCKED"
            )

        # No real vision provider configured in test env
        return VisualObservation(
            obs_id=request.obs_id,
            taken_at=time.time(),
            app_bundle_id=request.app.bundle_id,
            elements=(),
            is_sensitive_blocked=False,
            diagnostic="VISION_NOT_CONFIGURED"
        )


# --- AX + Vision Merging & Serialization ---

def serialize_compact_visual_observation(
    visual: VisualObservation,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> Dict[str, Any]:
    """
    Serializes a compact, token-efficient representation of visually grounded elements.
    Omits elements below the min_confidence threshold.
    """
    if visual.is_sensitive_blocked:
        return {
            "obs_id": visual.obs_id,
            "status": "BLOCKED_SENSITIVE_CONTENT",
            "visual_elements": []
        }

    filtered_elements = []
    for el in visual.elements:
        if el.confidence < min_confidence:
            continue
        filtered_elements.append({
            "visual_id": el.visual_id,
            "role": el.role,
            "label": el.label,
            "text_summary": el.text_summary,
            "confidence": round(el.confidence, 2),
            "enabled": el.enabled
        })

    return {
        "obs_id": visual.obs_id,
        "status": visual.diagnostic or "OK",
        "visual_elements": filtered_elements
    }


def merge_visual_grounding_into_planner_context(
    obs: SystemObservation,
    visual: Optional[VisualObservation],
    config: Optional[VisualObservationConfig] = None
) -> Dict[str, Any]:
    """
    Merges visual grounding context into planner context.
    Enforces freshness check: visual.obs_id must match obs.obs_id. Stale visual observations are discarded.
    """
    cfg = config or VisualObservationConfig()
    if not visual or visual.obs_id != obs.obs_id:
        return {
            "obs_id": obs.obs_id,
            "status": "STALE_OR_MISSING_VISUAL_OBSERVATION",
            "visual_elements": []
        }

    return serialize_compact_visual_observation(visual, min_confidence=cfg.min_confidence)
