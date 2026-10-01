"""
Computer-control data models (docs/07_Computer_Control.md Sections 6-10).

Pure and platform-independent: no AppKit, PyObjC, Accessibility, subprocess, or network.
Every model rejects unknown fields (the project's ToolParams convention) and is frozen, so
a value handed between the session, resolver, gate and (later) the worker cannot change.

Deliberately absent:
- raw AX element references (they exist only inside the future worker process);
- CG window ids (bridging AX <-> CG window ids is unvalidated, EVIDENCE.md Section 4);
- the text to type (set_text carries offsets into the user's utterance; see provenance.py).
"""

import hashlib
import re
from enum import Enum
from typing import Any, Dict, FrozenSet, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Enumerations ---

class Op(str, Enum):
    """Closed operation vocabulary for universal computer control."""
    ACTIVATE_APP = "activate_app"
    FOCUS = "focus"
    CLICK_ELEMENT = "click_element"
    SET_TEXT = "set_text"
    SELECT_TEXT = "select_text"
    PRESS = "press"
    SCROLL = "scroll"
    SELECT = "select"
    SELECT_TAB = "select_tab"
    MENU_ITEM_SELECT = "menu_item_select"
    COPY_SELECTION = "copy_selection"
    PASTE_CLIPBOARD = "paste_clipboard"
    WAIT_FOR_STATE = "wait_for_state"
    # CC-1b candidates: names reserved for schema compatibility; disabled, no parameters defined.
    LAUNCH_APP = "launch_app"
    PRESS_KEY = "press_key"
    OPEN_URL = "open_url"
    SCROLL_INTO_VIEW = "scroll_into_view"
    NAVIGATE_BACK = "navigate_back"
    NAVIGATE_FORWARD = "navigate_forward"
    CLOSE_WINDOW = "close_window"
    CLOSE_TAB = "close_tab"
    QUIT_APP = "quit_app"
    SWITCH_APP = "switch_app"
    SWITCH_WINDOW = "switch_window"
    NEW_WINDOW = "new_window"
    NEW_TAB = "new_tab"


class ActionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


class ExecutionStatus(str, Enum):
    NOT_EXECUTED = "NOT_EXECUTED"
    EXECUTED = "EXECUTED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    VERIFIED_NO_CHANGE = "VERIFIED_NO_CHANGE"
    CHANGED_UNSPECIFIED = "CHANGED_UNSPECIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    FORBIDDEN = "FORBIDDEN"


class GateOutcome(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    BLOCK = "BLOCK"


class SettleStatus(str, Enum):
    SETTLED = "settled"
    GROWING = "growing"
    SHRINKING = "shrinking"
    OSCILLATING = "oscillating"
    UNABLE_TO_DETERMINE = "unable_to_determine"


class EvieSelfStatus(str, Enum):
    """Whether the observed app/windows were checked against Evie's own UI/process."""
    EXCLUDED = "excluded"          # Evie's own UI/process was found and left out
    NOT_EXCLUDED = "not_excluded"  # every window was checked and none is Evie
    UNKNOWN = "unknown"            # a check could not be completed; do not treat as safe to control


class ResolutionMethod(str, Enum):
    IDENTIFIER = "identifier"
    EXACT = "exact"
    ORDINAL = "ordinal"


class StateLayer(str, Enum):
    UI = "ui"
    DOCUMENT = "document"
    FILESYSTEM = "filesystem"


class Sensitivity(str, Enum):
    SECURE_FIELD = "secure_field"
    EVIE_SELF = "evie_self"
    SENSITIVE_APP = "sensitive_app"


# --- Helpers ---

SECURE_ROLES = frozenset({"AXSecureTextField"})
_WS = re.compile(r"\s+")
BUNDLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.\-_]{0,254}$"


def normalize(text: Optional[str]) -> Optional[str]:
    """Exact-match normalisation: case-fold and collapse whitespace. No fuzzy matching."""
    if text is None:
        return None
    return _WS.sub(" ", text).strip().casefold()


def title_hash(title: str) -> str:
    """Stable, non-reversible window-title key, so observations never need to store titles."""
    return hashlib.sha256((normalize(title) or "").encode("utf-8")).hexdigest()[:16]


# --- Identity ---

class AppIdentity(_Strict):
    bundle_id: str = Field(..., pattern=BUNDLE_ID_PATTERN)
    pid: int = Field(..., ge=0)
    name: Optional[str] = Field(None, max_length=200)


class WindowKey(_Strict):
    """
    Window identity per docs/07 Section 7: pid + AXDocument (or title) hash + role/subrole,
    plus main/focused flags. The flags are state, not identity (see identity()).
    """
    pid: int = Field(..., ge=0)
    document_hash: Optional[str] = Field(None, max_length=64)
    title_hash: Optional[str] = Field(None, max_length=64)
    role: str = Field(..., min_length=1, max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    is_main: bool = False
    is_focused: bool = False

    def identity(self) -> Tuple[int, Optional[str], Optional[str], str, Optional[str]]:
        return (self.pid, self.document_hash, self.title_hash, self.role, self.subrole)


class Frame(_Strict):
    """Screen geometry. Metadata for ordering and staleness sanity only - never an action target."""
    x: float
    y: float
    width: float = Field(..., ge=0)
    height: float = Field(..., ge=0)


class SettleInfo(_Strict):
    status: SettleStatus
    elapsed_ms: int = Field(..., ge=0)


# --- Targets ---

class ObservedTarget(_Strict):
    """The only target form a chooser ever sees. Serialisable; no AX references."""
    index: int = Field(..., ge=0)
    obs_id: str = Field(..., min_length=1, max_length=64)
    role: str = Field(..., min_length=1, max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=200)
    identifier: Optional[str] = Field(None, max_length=200)
    context: Tuple[str, ...] = Field(default=(), max_length=20)   # ancestor role path, root first
    enabled: Optional[bool] = None    # None = attribute unsupported (e.g. TextEdit's AXTextArea)
    focused: Optional[bool] = None
    value_summary: Optional[str] = Field(None, max_length=120)
    actions: Tuple[str, ...] = Field(default=(), max_length=20)
    settable: Tuple[str, ...] = Field(default=(), max_length=20)  # settable AX attributes, e.g. AXSelectedText
    frame: Optional[Frame] = None
    sensitivity: FrozenSet[Sensitivity] = frozenset()
    window_index: Optional[int] = Field(None, ge=0)   # index into Observation.windows

    @model_validator(mode="before")
    @classmethod
    def _flag_secure_roles(cls, data):
        if isinstance(data, dict) and (data.get("role") in SECURE_ROLES or data.get("subrole") in SECURE_ROLES):
            flags = set(data.get("sensitivity") or ())
            flags.add(Sensitivity.SECURE_FIELD)
            data = {**data, "sensitivity": frozenset(flags)}
        return data

    @model_validator(mode="after")
    def _no_secure_values(self):
        if Sensitivity.SECURE_FIELD in self.sensitivity and self.value_summary is not None:
            raise ValueError("secure-field targets must never carry a value summary")
        return self

    @property
    def is_secure(self) -> bool:
        return Sensitivity.SECURE_FIELD in self.sensitivity

    def stable_key(self) -> Tuple:
        """Identity independent of observation, index and geometry (used for repeat detection)."""
        return (self.role, self.subrole, normalize(self.label), self.identifier, self.context)


class AXErrorRecord(_Strict):
    attribute: str = Field(..., min_length=1, max_length=64)   # attribute or operation, e.g. AXChildren, ActionNames
    role: Optional[str] = Field(None, max_length=64)            # role of the element, when known
    code: int                                                   # AXError code
    count: int = Field(..., ge=1)


class ObservationCounts(_Strict):
    """Kept separate on purpose (EVIDENCE.md Section 4): never collapse into one number."""
    total_nodes: int = Field(0, ge=0)
    actionable_nodes: int = Field(0, ge=0)
    on_screen_nodes: int = Field(0, ge=0)      # frame overlaps a display; NOT proof of visibility (Spaces)
    excluded_nodes: int = Field(0, ge=0)
    secure_nodes: int = Field(0, ge=0)
    ax_errors: int = Field(0, ge=0)
    windows: int = Field(0, ge=0)
    targets: int = Field(0, ge=0)
    targets_truncated: int = Field(0, ge=0)   # useful targets left out of the bounded target list


class SemanticNode(_Strict):
    """
    Canonical bounded semantic UI tree node for universal computer understanding.
    Preserves hierarchy, parent/child links, structural paths, safe value summaries,
    state (enabled/focused/selected), and redaction invariants.
    """
    node_id: str = Field(..., min_length=1, max_length=64)
    role: str = Field(..., min_length=1, max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=200)
    safe_value_summary: Optional[str] = Field(None, max_length=120)
    identifier: Optional[str] = Field(None, max_length=200)
    enabled: Optional[bool] = None
    focused: Optional[bool] = None
    selected: Optional[bool] = None
    path: Tuple[str, ...] = Field(default=(), max_length=100)
    parent_id: Optional[str] = Field(None, max_length=64)
    child_ids: Tuple[str, ...] = Field(default=(), max_length=500)
    app_identity: Optional[AppIdentity] = None
    window_identity: Optional[WindowKey] = None
    actions: Tuple[str, ...] = Field(default=(), max_length=20)
    settable: Tuple[str, ...] = Field(default=(), max_length=20)
    frame: Optional[Frame] = None
    sensitivity: FrozenSet[Sensitivity] = frozenset()
    window_index: Optional[int] = Field(None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _flag_secure_roles(cls, data):
        if isinstance(data, dict) and (data.get("role") in SECURE_ROLES or data.get("subrole") in SECURE_ROLES):
            flags = set(data.get("sensitivity") or ())
            flags.add(Sensitivity.SECURE_FIELD)
            data = {**data, "sensitivity": frozenset(flags)}
        return data

    @model_validator(mode="after")
    def _no_secure_values(self):
        if Sensitivity.SECURE_FIELD in self.sensitivity and self.safe_value_summary is not None:
            raise ValueError("secure-field nodes must never carry a safe value summary")
        return self

    @property
    def is_secure(self) -> bool:
        return Sensitivity.SECURE_FIELD in self.sensitivity

    def stable_key(self) -> Tuple:
        return (self.role, self.subrole, normalize(self.label), self.identifier, self.path)


class Observation(_Strict):
    obs_id: str = Field(..., min_length=1, max_length=64)
    taken_at: float = Field(..., ge=0)          # seconds on the session's injected clock
    frontmost: Optional[AppIdentity] = None      # set ONLY when independent readings agree (frontmost.py)
    frontmost_sources: Tuple[str, ...] = Field(default=(), max_length=8)
    frontmost_diagnostic: Optional[str] = Field(None, max_length=200)
    app: AppIdentity                            # the observed application
    window: Optional[WindowKey] = None          # primary root window (focused, else main), None if the app has none
    windows: Tuple[WindowKey, ...] = Field(default=(), max_length=50)   # every observed root window
    targets: Tuple[ObservedTarget, ...] = Field(default=(), max_length=500)
    running_apps: Tuple[AppIdentity, ...] = Field(default=(), max_length=500)
    installed_apps: Tuple[AppIdentity, ...] = Field(default=(), max_length=1000)
    root_id: Optional[str] = Field(None, max_length=64)
    nodes: Dict[str, SemanticNode] = Field(default_factory=dict)
    settle: SettleInfo
    counts: Optional[ObservationCounts] = None
    excluded: Dict[str, int] = Field(default_factory=dict)   # counts by reason: secure, sensitive_app, evie_self, menu_bar
    ax_errors: Tuple[AXErrorRecord, ...] = Field(default=(), max_length=200)
    caps_hit: Tuple[str, ...] = ()               # depth / nodes / time / children: the tree is NOT complete
    menu_bar_pruned: bool = False
    evie_self: EvieSelfStatus = EvieSelfStatus.UNKNOWN
    restricted: Optional[str] = Field(None, max_length=64)   # set when the app may not be observed at all
    web_area_present: bool = False
    duration_ms: Optional[float] = Field(None, ge=0)
    fingerprint: str = Field(..., min_length=1, max_length=128)

    @property
    def timestamp(self) -> float:
        return self.taken_at

    @property
    def bounded_node_count(self) -> int:
        return len(self.nodes)

    @model_validator(mode="after")
    def _consistent_targets(self):
        for position, t in enumerate(self.targets):
            if t.obs_id != self.obs_id:
                raise ValueError(f"target {t.index} belongs to observation {t.obs_id}, not {self.obs_id}")
            if t.index != position:
                raise ValueError("target indexes must be 0..n-1 in observation order")
            if t.window_index is not None and t.window_index >= len(self.windows):
                raise ValueError(f"target {t.index} references window {t.window_index}, which was not observed")
        return self

    @property
    def complete(self) -> bool:
        return not self.caps_hit and self.restricted is None

    def window_for(self, target: "ObservedTarget") -> Optional[WindowKey]:
        if target.window_index is not None and target.window_index < len(self.windows):
            return self.windows[target.window_index]
        return self.window

    @property
    def degraded(self) -> bool:
        return bool(self.caps_hit)


SystemObservation = Observation


class ResolvedTarget(_Strict):
    target: ObservedTarget
    method: ResolutionMethod
    obs_id: str = Field(..., min_length=1, max_length=64)

    @model_validator(mode="after")
    def _same_observation(self):
        if self.target.obs_id != self.obs_id:
            raise ValueError("resolved target must reference the observation it was resolved against")
        return self


# --- Operation arguments (closed, one model per operation) ---

# The ONE closed, code-owned app-alias table (CC-1b.1). Used by the fast-path app resolver AND the allowed-app
# scope, so a name the user said resolves identically in both. Only apps already running are ever matched.
APP_ALIASES = {"chrome": "com.google.Chrome"}


class NoArgs(_Strict):
    pass


class ActivateAppArgs(_Strict):
    bundle_id: str = Field(..., pattern=BUNDLE_ID_PATTERN)


TextMode = Literal["insert_at_cursor", "replace_selection", "replace_all"]


class SetTextArgs(_Strict):
    """Text comes only from offsets into the user's verbatim utterance - never free text."""
    text_span: Tuple[int, int]
    mode: TextMode = "insert_at_cursor"

    @field_validator("text_span")
    @classmethod
    def _ordered(cls, span):
        start, end = span
        if start < 0 or end < start:
            raise ValueError("text_span must satisfy 0 <= start <= end")
        return span


class SelectTextArgs(_Strict):
    location: int = Field(..., ge=0)
    length: int = Field(..., ge=0, le=100_000)


class ScrollArgs(_Strict):
    direction: Literal["up", "down"]
    amount: float = Field(0.25, gt=0, le=0.25)   # fraction of the scroll range (validated bound)


PressKeyName = Literal["RIGHT_ARROW", "LEFT_ARROW"]          # Step 15C: the ONLY keys; no modifiers, no text


class PressKeyArgs(_Strict):
    """One bounded caret key. Unknown fields (keycodes, modifiers, strings) are rejected by _Strict."""
    key: PressKeyName


class ClickElementArgs(_Strict):
    click_count: int = Field(1, ge=1, le=2)


class SelectArgs(_Strict):
    value: Optional[str] = Field(None, max_length=200)


class SelectTabArgs(_Strict):
    tab_index: Optional[int] = Field(None, ge=0)


WaitStateCondition = Literal[
    "frontmost", "window_appeared", "control_appeared", "control_enabled",
    "selected_changed", "value_changed", "settled"
]


class WaitForStateArgs(_Strict):
    condition: WaitStateCondition
    expected_value: Optional[str] = Field(None, max_length=200)
    timeout_s: float = Field(5.0, gt=0, le=30.0)


FocusElementArgs = NoArgs
MenuItemSelectArgs = NoArgs
CopySelectionArgs = NoArgs
PasteClipboardArgs = NoArgs


class QuitAppArgs(_Strict):
    bundle_id: Optional[str] = Field(None, pattern=BUNDLE_ID_PATTERN)


class CloseWindowArgs(_Strict):
    pass


SwitchAppArgs = ActivateAppArgs
SwitchWindowArgs = NoArgs

CAPABILITY_MATRIX: Dict[str, Op] = {
    "activate application": Op.ACTIVATE_APP,
    "focus application": Op.FOCUS,
    "close window": Op.CLOSE_WINDOW,
    "quit application": Op.QUIT_APP,
    "switch application": Op.SWITCH_APP,
    "switch window": Op.SWITCH_WINDOW,
}


OpArgs = Union[
    NoArgs, ActivateAppArgs, SetTextArgs, SelectTextArgs, ScrollArgs, PressKeyArgs,
    ClickElementArgs, SelectArgs, SelectTabArgs, WaitForStateArgs, QuitAppArgs, CloseWindowArgs
]


class UniversalAction(_Strict):
    """
    General-purpose computer interaction contract built on semantic target resolution.
    Attributable to an observation ID and resolved semantically before execution.
    """
    action_id: str = Field(..., min_length=1, max_length=64)
    obs_id: str = Field(..., min_length=1, max_length=64)
    op: Op
    args: OpArgs = Field(default_factory=NoArgs)
    target_spec: Optional[Any] = None
    created_at: float = Field(0.0, ge=0)


# --- Gate, request, result ---

class GateDecision(_Strict):
    outcome: GateOutcome
    risk: RiskLevel
    reason: str = Field(..., min_length=1, max_length=64)

    @property
    def allowed(self) -> bool:
        return self.outcome is GateOutcome.ALLOW

    @model_validator(mode="after")
    def _only_low_is_allowed(self):
        if self.outcome is GateOutcome.ALLOW and self.risk is not RiskLevel.LOW:
            raise ValueError("only LOW risk can be allowed in CC-1a")
        return self


# Must exceed the 1 s AX messaging timeout plus the ~3 s settle bound (docs/07 Section 6).
MIN_ACTION_TIMEOUT_S = 4.0


class ActionRequest(_Strict):
    """An approved action, as it will cross the IPC boundary to the worker."""
    session_id: str = Field(..., min_length=1, max_length=64)
    step: int = Field(..., ge=1)
    op: Op
    target: Optional[ResolvedTarget] = None
    args: OpArgs
    obs_id: str = Field(..., min_length=1, max_length=64)
    gate: GateDecision
    cancel_epoch: int = Field(..., ge=0)
    timeout_s: float = Field(..., gt=MIN_ACTION_TIMEOUT_S, le=60)

    @model_validator(mode="before")
    @classmethod
    def _args_by_op(cls, data):
        # Across the IPC boundary args arrive as a plain dict, and several argument models share a shape
        # (e.g. {"bundle_id"} is both ActivateAppArgs and QuitAppArgs): parse them with THE op's own schema
        # instead of the first union member that happens to fit.
        if isinstance(data, dict) and isinstance(data.get("args"), dict) and data.get("op") is not None:
            from services.computer_control.actions import get_definition
            try:
                model = get_definition(Op(data["op"])).params_model
            except ValueError:
                return data
            if model is not None:
                data = {**data, "args": model.model_validate(data["args"])}
        return data

    @model_validator(mode="after")
    def _consistent(self):
        from services.computer_control.actions import get_definition
        definition = get_definition(self.op)
        if definition.params_model is None or not isinstance(self.args, definition.params_model):
            raise ValueError(f"arguments do not match the schema for {self.op.value}")
        if definition.requires_target and self.target is None:
            raise ValueError(f"{self.op.value} requires a resolved target")
        if not definition.requires_target and self.target is not None:
            raise ValueError(f"{self.op.value} does not take a target")
        if self.target is not None and self.target.obs_id != self.obs_id:
            raise ValueError("target must be resolved against the request's observation")
        if not self.gate.allowed:
            raise ValueError("an ActionRequest exists only for gate-approved actions")
        return self


EvidenceValue = Union[str, int, float, bool, None]

_NOT_EXECUTED_STATUSES = {ActionStatus.BLOCKED, ActionStatus.STALE, ActionStatus.CANCELLED}


def derive_status(execution: ExecutionStatus, verification: VerificationStatus) -> ActionStatus:
    """
    Map an executed attempt to its status. An API-level success (EXECUTED) never becomes
    SUCCESS on its own: only operation-specific verification (VERIFIED) does.
    """
    if execution is ExecutionStatus.NOT_EXECUTED:
        raise ValueError("NOT_EXECUTED attempts are BLOCKED, STALE or CANCELLED; set status explicitly")
    if execution is ExecutionStatus.ERROR:
        return ActionStatus.FAILED
    if execution is ExecutionStatus.TIMEOUT:
        return ActionStatus.TIMEOUT
    if verification is VerificationStatus.VERIFIED:
        return ActionStatus.SUCCESS
    if verification is VerificationStatus.VERIFIED_NO_CHANGE:
        return ActionStatus.FAILED
    return ActionStatus.NOT_VERIFIABLE   # CHANGED_UNSPECIFIED, INCONCLUSIVE, or no verifier


class ActionResult(_Strict):
    session_id: str = Field(..., min_length=1, max_length=64)
    step: int = Field(..., ge=1)
    op: Op
    status: ActionStatus
    execution: ExecutionStatus
    verification: VerificationStatus
    mechanism: Optional[str] = Field(None, max_length=64)
    requested_at: float = Field(..., ge=0)
    executed_start: Optional[float] = Field(None, ge=0)
    executed_end: Optional[float] = Field(None, ge=0)
    verified_at: Optional[float] = Field(None, ge=0)
    latency_ms: Optional[float] = Field(None, ge=0)
    ax_error: Optional[int] = None
    evidence: Dict[str, EvidenceValue] = Field(default_factory=dict, max_length=20)
    state_layers: FrozenSet[StateLayer] = frozenset()
    retry_permitted: bool = False
    noop: bool = False
    reason: Optional[str] = Field(None, max_length=200)

    @model_validator(mode="after")
    def _status_is_consistent(self):
        s, e, v = self.status, self.execution, self.verification
        if s in _NOT_EXECUTED_STATUSES:
            if e is not ExecutionStatus.NOT_EXECUTED:
                raise ValueError(f"{s.value} means nothing was executed")
        elif e is ExecutionStatus.NOT_EXECUTED:
            raise ValueError(f"{s.value} requires an execution attempt")
        elif s is not derive_status(e, v):
            raise ValueError(f"status {s.value} is inconsistent with execution {e.value} / verification {v.value}")
        if self.retry_permitted and s is not ActionStatus.STALE:
            raise ValueError("only STALE results may be retried (through a fresh observation and choice)")
        if self.noop and s not in (ActionStatus.FAILED, ActionStatus.TIMEOUT, ActionStatus.NOT_VERIFIABLE):
            raise ValueError("only FAILED, TIMEOUT or NOT_VERIFIABLE results can be no-ops")
        return self

    @classmethod
    def executed(cls, *, execution: ExecutionStatus, verification: VerificationStatus,
                 fingerprint_changed: bool, **fields) -> "ActionResult":
        """Build the result of an executed attempt; status and no-op flag are derived, never asserted."""
        status = derive_status(execution, verification)
        noop = status in (ActionStatus.FAILED, ActionStatus.TIMEOUT) or (
            status is ActionStatus.NOT_VERIFIABLE and not fingerprint_changed)
        return cls(status=status, execution=execution, verification=verification, noop=noop, **fields)


class PermissionStatus(_Strict):
    """What the worker's PermissionProbe reports (docs/07 Section 14). Both flags must be true."""
    ax_trusted: bool
    ax_functional: bool
    worker_pid: Optional[int] = Field(None, ge=0)
    responsible_bundle: Optional[str] = Field(None, max_length=255)

    @property
    def ok(self) -> bool:
        return self.ax_trusted and self.ax_functional
