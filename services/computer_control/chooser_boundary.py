"""
Chooser boundary (docs/07_Computer_Control.md Section 13; CC-1a Step 7) - pure.

    utterance + fresh Observation
        -> ChooserInput (bounded summary; screen text is data)
        -> deterministic fast path            (always first)
        -> provider.choose(input)             (ONLY on NO_MATCH, and only if one is configured - none in CC-1a)
        -> strict validation                  (schema + chooser op allowlist + observation/obs_id + text span)
        -> deterministic hardening            (hardening.py: freshness, grammar precedence, ambiguity, exact text)
        -> ONE ChooserOutcome                 (ACTION / DONE / BLOCKED / ASK, or STOP)
        -> orchestrator/session, whose code-owned gate still decides

The boundary produces data only. It has no access to the worker, executor, AX, subprocesses, files
or network; a provider sees only the ChooserInput. Anything invalid fails closed to BLOCKED; anything
unrecognised with no provider fails closed to ASK.
"""

from typing import Any, Literal, Optional, Sequence, Tuple, Union

from pydantic import Field

from services.computer_control.chooser import (
    CHOOSER_OPS,
    AppRef,
    AskDecision,
    BlockedDecision,
    ChooserInput,
    ChooserProvider,
    ChooserValidation,
    DoneDecision,
    RecentAction,
    ValidatedAction,
    build_input,
    validate_decision,
)
from services.computer_control.fast_path import fast_path
from services.computer_control.hardening import Assessment, harden
from services.computer_control.models import Observation, Op, _Strict

Decision = Union[ValidatedAction, DoneDecision, BlockedDecision, AskDecision]


class ChooserOutcome(_Strict):
    source: Literal["fast_path", "provider", "boundary"]
    stop: bool = False                                  # session-level cancellation (not an action)
    decision: Optional[Decision] = None
    rule: Optional[str] = Field(None, max_length=32)
    error: Optional[str] = Field(None, max_length=200)  # why an output was rejected (then decision is BLOCKED)
    assessment: Optional[Assessment] = None            # what hardening concluded about the chooser's own output
    findings: Tuple[str, ...] = ()

    def as_decision(self) -> Optional[dict]:
        """Raw form for ComputerControlSession.propose (which validates and gates again)."""
        if self.decision is None:
            return None
        if isinstance(self.decision, ValidatedAction):
            return self.decision.as_decision()
        return self.decision.model_dump(mode="json")


def _check_input(chooser_input: ChooserInput, observation: Observation) -> Optional[str]:
    if chooser_input.obs_id != observation.obs_id:
        return "INPUT_OBSERVATION_MISMATCH"
    return None


def validate_output(raw: Any, chooser_input: ChooserInput, observation: Observation,
                    require_obs_id: bool = True,
                    installed_apps: Sequence[AppRef] = ()) -> ChooserValidation:
    """Strict validation of ANY chooser output (fast path or provider). Never executes anything."""
    mismatch = _check_input(chooser_input, observation)
    if mismatch:
        return ChooserValidation(ok=False, error=mismatch)
    checked = validate_decision(raw, utterance=chooser_input.utterance, observation=observation, installed_apps=installed_apps)
    if not checked.ok or not isinstance(checked.decision, ValidatedAction):
        return checked
    action = checked.decision
    if action.op not in CHOOSER_OPS:
        return ChooserValidation(ok=False, error=f"UNSUPPORTED_OPERATION: {action.op.value}")
    if action.target_index is not None:
        if require_obs_id and action.obs_id is None:
            return ChooserValidation(ok=False, error="OBS_ID_REQUIRED_FOR_TARGET")
        if observation.targets[action.target_index].obs_id != observation.obs_id:
            return ChooserValidation(ok=False, error="TARGET_NOT_IN_OBSERVATION")
    return checked


def validate_provider_output(raw: Any, chooser_input: ChooserInput, observation: Observation,
                             installed_apps: Sequence[AppRef] = ()) -> ChooserValidation:
    """Provider outputs must name the observation any target index refers to."""
    return validate_output(raw, chooser_input, observation, require_obs_id=True, installed_apps=installed_apps)


def _blocked(source: str, error: str, rule: Optional[str] = None) -> ChooserOutcome:
    return ChooserOutcome(source=source, rule=rule, error=error[:200],
                          decision=BlockedDecision(reason=f"INVALID_CHOOSER_OUTPUT: {error}"[:200]))


def _hardened(source: str, chooser_input: ChooserInput, decision, rule: Optional[str] = None,
              installed_apps: Sequence[AppRef] = ()) -> ChooserOutcome:
    h = harden(chooser_input, decision, installed_apps)
    if h.outcome == "REJECTED":
        out = _blocked(source, ",".join(h.findings) or "REJECTED", rule)
        return out.model_copy(update={"assessment": h.assessment, "findings": h.findings})
    return ChooserOutcome(source=source, decision=h.decision, rule=rule, assessment=h.assessment, findings=h.findings)


def _checked(source: str, raw: Any, chooser_input: ChooserInput, observation: Observation,
             rule: Optional[str], installed_apps: Sequence[AppRef] = ()) -> ChooserOutcome:
    checked = validate_output(raw, chooser_input, observation, installed_apps=installed_apps)
    if not checked.ok:
        return _blocked(source, checked.error or "INVALID", rule)
    return _hardened(source, chooser_input, checked.decision, rule, installed_apps)


def choose(utterance: str, observation: Observation, *, allowed_apps: Sequence[str] = (),
           recent: Sequence[RecentAction] = (), step: int = 1, budget_remaining: int = 0,
           provider: Optional[ChooserProvider] = None, installed_apps: Sequence[AppRef] = ()) -> ChooserOutcome:
    """Exactly one validated outcome for this utterance against THIS observation."""
    chooser_input = build_input(utterance, observation, recent, step, budget_remaining, allowed_apps)
    fast = fast_path(chooser_input, installed_apps)
    if fast.kind == "STOP":
        return ChooserOutcome(source="fast_path", stop=True, rule=fast.rule)
    if fast.kind == "DECISION":
        return _checked("fast_path", fast.decision, chooser_input, observation, fast.rule, installed_apps)
    if provider is None:
        return ChooserOutcome(source="boundary", rule="no_match", decision=AskDecision(
            question="I can't do that yet. What would you like me to do?", reason="UNRECOGNIZED_REQUEST"))
    try:
        raw = provider.choose(chooser_input)
    except Exception as e:                                # a provider failure is never an action
        return _blocked("provider", f"PROVIDER_ERROR: {type(e).__name__}")
    checked = validate_provider_output(raw, chooser_input, observation)
    if not checked.ok:
        return _blocked("provider", checked.error or "INVALID")
    return _hardened("provider", chooser_input, checked.decision)


class FastPathChooser:
    """
    The session chooser in CC-1a Step 11: the deterministic grammar only, no provider, no network.

    The grammar maps one command to at most ONE action. Once that action has been executed AND verified
    (a SUCCESS in recent_actions), the only thing left to say is DONE - a claim, which goes through the
    same validation and hardening and is then checked by the session's goal verification.
    """
    name = "fast_path"

    def choose(self, utterance: str, observation: Observation, *, recent: Sequence[RecentAction] = (),
               step: int = 1, allowed_apps: Sequence[str] = (), installed_apps: Sequence[AppRef] = ()) -> ChooserOutcome:
        if any(r.status == "SUCCESS" for r in recent):
            chooser_input = build_input(utterance, observation, recent, step, 0, allowed_apps)
            return _checked("fast_path", {"status": "DONE"}, chooser_input, observation, "single_action_done")
        return choose(utterance, observation, allowed_apps=allowed_apps, recent=recent, step=step, provider=None, installed_apps=installed_apps)
