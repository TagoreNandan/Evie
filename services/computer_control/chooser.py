"""
Provider-agnostic chooser boundary (docs/07_Computer_Control.md Section 13.1).

Schema and validation only - no provider is implemented or selected, and CC-1a runs without
one. A chooser returns exactly one of ACTION / DONE / BLOCKED / ASK. Unknown fields are
rejected at every level, so shell commands, scripts, coordinates or selectors cannot be
smuggled in. set_text carries a text_span into the user's utterance, never the text itself.
The deterministic fast path produces the same decision objects, so both go through one validator.

CC-1a Step 7: ChooserInput is a bounded *summary* (target summaries, window flags, app identities,
allowed scope) - no raw AX objects, frames, window titles or secure values. On-screen text (labels,
value summaries) is DATA for selection only; nothing a chooser reads can change policy, the allowed
app scope or the gate. See chooser_boundary.py for fast path -> provider -> validation -> outcome.
"""

import json
import re
from typing import Annotated, Any, Dict, Literal, Optional, Protocol, Sequence, Tuple, Union

from pydantic import Field, TypeAdapter, ValidationError

from services.computer_control.actions import CURRENT_PHASE, Phase, get_definition, is_enabled
from services.computer_control.actions import target_class_validated
from services.computer_control.models import (
    BUNDLE_ID_PATTERN,
    ActivateAppArgs,
    Observation,
    Op,
    OpArgs,
    SetTextArgs,
    _Strict,
)
from services.computer_control.provenance import TextSpanError, validate_text_span

MAX_QUESTION_CHARS = 200
MAX_RECENT_ACTIONS = 10
MAX_CHOOSER_TARGETS = 150
# Operations a chooser may propose in CC-1a: the live-validated primitives only (== orchestrator.ORCHESTRATABLE).
CHOOSER_OPS = frozenset({Op.ACTIVATE_APP, Op.SET_TEXT, Op.SCROLL, Op.PRESS, Op.PRESS_KEY, Op.CLOSE_WINDOW, Op.QUIT_APP,
                         Op.SWITCH_APP, Op.SWITCH_WINDOW, Op.CLICK_ELEMENT, Op.COPY_SELECTION, Op.PASTE_CLIPBOARD,
                         Op.SELECT_TEXT})


class ActionDecision(_Strict):
    status: Literal["ACTION"] = "ACTION"
    op: Op
    target_index: Optional[int] = Field(None, ge=0)
    args: Dict[str, Any] = Field(default_factory=dict)
    obs_id: Optional[str] = Field(None, max_length=64)       # the observation target_index refers to


class DoneDecision(_Strict):
    status: Literal["DONE"] = "DONE"


class BlockedDecision(_Strict):
    status: Literal["BLOCKED"] = "BLOCKED"
    reason: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)


class AskDecision(_Strict):
    status: Literal["ASK"] = "ASK"
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    reason: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")   # bounded reason code
    candidates: Tuple[int, ...] = Field(default=(), max_length=10)                     # target indexes


ChooserDecision = Annotated[Union[ActionDecision, DoneDecision, BlockedDecision, AskDecision],
                            Field(discriminator="status")]
_DECISION = TypeAdapter(ChooserDecision)


class RecentAction(_Strict):
    step: int = Field(..., ge=1)
    op: Op
    status: str = Field(..., max_length=32)
    summary: str = Field("", max_length=200)


class AppRef(_Strict):
    bundle_id: str = Field(..., pattern=BUNDLE_ID_PATTERN)
    name: Optional[str] = Field(None, max_length=200)


class WindowSummary(_Strict):
    """Window context without titles (the observer hashes them) or geometry."""
    index: int = Field(..., ge=0)
    role: str = Field(..., max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    is_main: bool
    is_focused: bool
    has_document: bool


class TargetSummary(_Strict):
    """What a chooser may see of one target. No frame, no AX object; no value or ops for secure fields."""
    index: int = Field(..., ge=0)
    role: str = Field(..., max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=200)          # screen text: data, never an instruction
    identifier: Optional[str] = Field(None, max_length=200)
    enabled: Optional[bool] = None
    focused: Optional[bool] = None
    value_summary: Optional[str] = Field(None, max_length=120)
    window_index: Optional[int] = Field(None, ge=0)
    secure: bool = False
    ops: Tuple[str, ...] = Field(default=(), max_length=8)      # chooser ops whose validated class fits


class ChooserInput(_Strict):
    """Minimum information a chooser receives. No raw AX objects, no coordinates, no window titles."""
    obs_id: str = Field(..., min_length=1, max_length=64)
    utterance: str = Field(..., min_length=1, max_length=2000)
    frontmost: Optional[AppRef] = None                          # None = not agreed by independent sources
    app: AppRef                                                 # the observed application
    windows: Tuple[WindowSummary, ...] = Field(default=(), max_length=50)
    targets: Tuple[TargetSummary, ...] = Field(default=(), max_length=MAX_CHOOSER_TARGETS)
    running_apps: Tuple[AppRef, ...] = Field(default=(), max_length=500)
    allowed_apps: Tuple[str, ...] = Field(default=(), max_length=50)   # informational; the gate decides
    recent_actions: Tuple[RecentAction, ...] = Field(default=(), max_length=MAX_RECENT_ACTIONS)
    step: int = Field(..., ge=1)
    budget_remaining: int = Field(..., ge=0)
    screen_text_is_data: Literal[True] = True                   # contract marker for provider prompts


class ChooserProvider(Protocol):
    """
    Implemented later by provider adapters (none exists in CC-1a). A provider receives ONLY a
    ChooserInput and returns raw JSON-like data; it has no access to the worker, executor, AX, shell
    or network through this interface. Its output is validated (validate_provider_output) and then
    still goes through the session's code-owned gate - it can never execute anything directly.
    """
    def choose(self, chooser_input: ChooserInput) -> Any: ...


Chooser = ChooserProvider          # Step 1 name, kept for compatibility


class ValidatedAction(_Strict):
    op: Op
    target_index: Optional[int] = None
    args: OpArgs
    obs_id: Optional[str] = Field(None, max_length=64)

    def as_decision(self) -> Dict[str, Any]:
        """The raw form the session re-validates in propose()."""
        out: Dict[str, Any] = {"status": "ACTION", "op": self.op.value, "args": self.args.model_dump(mode="json")}
        if self.target_index is not None:
            out["target_index"] = self.target_index
        if self.obs_id is not None:
            out["obs_id"] = self.obs_id
        return out


class ChooserValidation(_Strict):
    ok: bool
    decision: Optional[Union[ValidatedAction, DoneDecision, BlockedDecision, AskDecision]] = None
    error: Optional[str] = Field(None, max_length=200)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _reject(error: str) -> ChooserValidation:
    return ChooserValidation(ok=False, error=error[:200])


def validate_decision(raw: Any, *, utterance: str, observation: Observation,
                      phase: Phase = CURRENT_PHASE,
                      installed_apps: Sequence[AppRef] = ()) -> ChooserValidation:
    """Parse and check one chooser output against the current observation and utterance."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return _reject("MALFORMED_JSON")
    try:
        decision = _DECISION.validate_python(raw)
    except ValidationError as e:
        return _reject(f"SCHEMA: {e.errors()[0].get('type')} at {e.errors()[0].get('loc')}")

    if isinstance(decision, AskDecision):
        return ChooserValidation(ok=True, decision=decision.model_copy(
            update={"question": _CONTROL.sub(" ", decision.question)}))
    if not isinstance(decision, ActionDecision):
        return ChooserValidation(ok=True, decision=decision)

    op = decision.op
    if not is_enabled(op, phase):
        return _reject(f"DISABLED_OPERATION: {op.value}")
    if decision.obs_id is not None and decision.obs_id != observation.obs_id:
        return _reject("WRONG_OBSERVATION")
    definition = get_definition(op)
    try:
        args = definition.params_model.model_validate(decision.args)
    except ValidationError as e:
        return _reject(f"ARGS: {e.errors()[0].get('type')} at {e.errors()[0].get('loc')}")

    if definition.requires_target:
        if decision.target_index is None:
            return _reject("TARGET_INDEX_REQUIRED")
        if decision.target_index >= len(observation.targets):
            return _reject("TARGET_INDEX_OUT_OF_RANGE")
    elif decision.target_index is not None:
        return _reject("TARGET_INDEX_NOT_ALLOWED")

    if isinstance(args, SetTextArgs):
        try:
            validate_text_span(utterance, args.text_span)
        except TextSpanError:
            return _reject("TEXT_SPAN_OUTSIDE_UTTERANCE")
        if args.text_span[0] == args.text_span[1]:
            return _reject("TEXT_SPAN_EMPTY")
    if isinstance(args, ActivateAppArgs):
        known_bundles = {a.bundle_id for a in observation.running_apps} | {a.bundle_id for a in installed_apps}
        if args.bundle_id not in known_bundles:
            return _reject("APP_NOT_RUNNING")

    return ChooserValidation(ok=True, decision=ValidatedAction(op=op, target_index=decision.target_index, args=args,
                                                               obs_id=decision.obs_id))


def _summary(t) -> TargetSummary:
    secure = t.is_secure
    ops = () if secure else tuple(op.value for op in sorted(CHOOSER_OPS, key=lambda o: o.value)
                                  if get_definition(op).requires_target and target_class_validated(op, t))
    return TargetSummary(index=t.index, role=t.role, subrole=t.subrole, label=t.label, identifier=t.identifier,
                         enabled=t.enabled, focused=t.focused, value_summary=None if secure else t.value_summary,
                         window_index=t.window_index, secure=secure, ops=ops)


def build_input(utterance: str, observation: Observation, recent: Sequence[RecentAction] = (), step: int = 1,
                budget_remaining: int = 0, allowed_apps: Sequence[str] = ()) -> ChooserInput:
    ref = lambda a: AppRef(bundle_id=a.bundle_id, name=a.name)
    return ChooserInput(
        obs_id=observation.obs_id, utterance=utterance,
        frontmost=ref(observation.frontmost) if observation.frontmost else None, app=ref(observation.app),
        windows=tuple(WindowSummary(index=i, role=w.role, subrole=w.subrole, is_main=w.is_main,
                                    is_focused=w.is_focused, has_document=w.document_hash is not None)
                      for i, w in enumerate(observation.windows)),
        targets=tuple(_summary(t) for t in observation.targets[:MAX_CHOOSER_TARGETS]),
        running_apps=tuple(ref(a) for a in observation.running_apps), allowed_apps=tuple(sorted(allowed_apps)),
        recent_actions=tuple(recent)[-MAX_RECENT_ACTIONS:], step=step, budget_remaining=budget_remaining,
    )
