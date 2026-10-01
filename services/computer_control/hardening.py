"""
Deterministic semantic hardening of chooser output (docs/07_Computer_Control.md Section 31; CC-1a Step 10).

Pure: no OS, AX, network, LLM, worker, executor, session or gate. It sits AFTER strict schema validation
and BEFORE the code-owned gate:

    chooser / provider -> validate_provider_output -> harden() -> session (gate) -> orchestrator -> executor

The model proposes; this layer decides whether the proposal is unambiguous and internally consistent.
It can only make a decision SAFER (ACTION -> ASK / BLOCKED / REJECTED) - never turn ASK or BLOCKED into
an ACTION - and it never decides whether an action is ALLOWED (that remains the gate's job).

Checks, in order, for an ACTION:
  1. freshness: a target must belong to the current observation (else REJECTED, MODEL_OUTPUT_INVALID);
  2. deterministic precedence: if the fixed grammar (fast path) classifies the utterance as BLOCKED or
     ASK, a model ACTION cannot override it (MODEL_OUTPUT_INCORRECT);
  3. ambiguity: a chosen app/target with an indistinguishable eligible twin becomes ASK
     (AMBIGUOUS_APP / AMBIGUOUS_TARGET, MODEL_OUTPUT_AMBIGUOUS) - the model cannot break the tie; the
     only tie-breaker is a unique AXIdentifier that the USER named in the utterance (resolver priority 1);
  4. text provenance: when the text grammar applies, the executable span is DERIVED from the utterance
     (fast_path.text_payload_span). A model span that differs is recorded as MODEL_OUTPUT_INCORRECT
     (TEXT_SPAN_MISMATCH) - it is never counted as correct - and the safe action uses the derived span.
"""

from typing import Literal, Optional, Sequence, Tuple, Union

from pydantic import Field

from services.computer_control.chooser import (
    AppRef,
    AskDecision,
    BlockedDecision,
    ChooserInput,
    DoneDecision,
    ValidatedAction,
)
from services.computer_control.fast_path import control_name, fast_path, in_actionable_window, text_payload_span
from services.computer_control.models import Op, SetTextArgs, _Strict, normalize

Decision = Union[ValidatedAction, DoneDecision, BlockedDecision, AskDecision]
Assessment = Literal["VALID", "MODEL_OUTPUT_INVALID", "MODEL_OUTPUT_AMBIGUOUS", "MODEL_OUTPUT_INCORRECT"]
Outcome = Literal["SAFE_NORMALIZED_ACTION", "ASK", "BLOCKED", "DONE", "REJECTED"]
TEXT_MODE = "insert_at_cursor"                 # what a type/write/enter command means
AMBIGUITY = ("AMBIGUOUS_APP", "AMBIGUOUS_TARGET")


class HardenedChooserOutput(_Strict):
    assessment: Assessment                     # what the MODEL did (kept for benchmarking; never hidden)
    outcome: Outcome                           # what may proceed to the gate
    decision: Optional[Decision] = None        # None only when REJECTED
    findings: Tuple[str, ...] = Field(default=(), max_length=8)


def _outcome(decision: Decision) -> Outcome:
    if isinstance(decision, ValidatedAction):
        return "SAFE_NORMALIZED_ACTION"
    return {DoneDecision: "DONE", BlockedDecision: "BLOCKED", AskDecision: "ASK"}[type(decision)]


def _ask(reason: str, question: str, candidates=()) -> AskDecision:
    return AskDecision(question=question, reason=reason, candidates=tuple(candidates)[:10])


def harden(chooser_input: ChooserInput, decision: Decision, installed_apps: Sequence[AppRef] = ()) -> HardenedChooserOutput:
    """Semantic hardening of ONE already schema-validated decision. Pure and deterministic."""
    if not isinstance(decision, ValidatedAction):
        return HardenedChooserOutput(assessment="VALID", outcome=_outcome(decision), decision=decision)
    ci, op = chooser_input, decision.op

    # 1. freshness: never remap a target from another observation
    if decision.target_index is not None:
        if decision.obs_id != ci.obs_id:
            return HardenedChooserOutput(assessment="MODEL_OUTPUT_INVALID", outcome="REJECTED",
                                         findings=("STALE_OBSERVATION",))
        if decision.target_index >= len(ci.targets):
            return HardenedChooserOutput(assessment="MODEL_OUTPUT_INVALID", outcome="REJECTED",
                                         findings=("TARGET_NOT_IN_OBSERVATION",))

    # 2. deterministic precedence: a model ACTION cannot override a grammar-level BLOCKED / ASK / STOP
    fast = fast_path(ci, installed_apps)
    if fast.kind == "STOP":
        return HardenedChooserOutput(assessment="MODEL_OUTPUT_INCORRECT", outcome="REJECTED",
                                     findings=("STOP_PHRASE_NOT_AN_ACTION",))
    if fast.kind == "DECISION" and fast.decision["status"] in ("BLOCKED", "ASK"):
        forced = (BlockedDecision if fast.decision["status"] == "BLOCKED" else AskDecision).model_validate(
            fast.decision)
        if isinstance(forced, AskDecision) and forced.reason in AMBIGUITY:     # the grammar saw the same tie
            return HardenedChooserOutput(assessment="MODEL_OUTPUT_AMBIGUOUS", outcome="ASK", decision=forced,
                                         findings=(forced.reason,))
        return HardenedChooserOutput(assessment="MODEL_OUTPUT_INCORRECT", outcome=_outcome(forced), decision=forced,
                                     findings=(f"DETERMINISTIC_{fast.decision['status']}_PRECEDENCE",))

    # 3. ambiguity: the model cannot break a tie between indistinguishable candidates
    findings = []
    if op is Op.ACTIVATE_APP:
        all_apps = list(ci.running_apps) + list(installed_apps)
        chosen = next((a for a in all_apps if a.bundle_id == decision.args.bundle_id), None)
        if chosen is None:
            return HardenedChooserOutput(assessment="MODEL_OUTPUT_INVALID", outcome="REJECTED",
                                         findings=("APP_NOT_IN_OBSERVATION",))
        # One app is listed twice when it is both running and installed: twins are DIFFERENT bundles only.
        twins = {a.bundle_id for a in all_apps if chosen.name and normalize(a.name) == normalize(chosen.name)}
        if len(twins) > 1:
            return HardenedChooserOutput(
                assessment="MODEL_OUTPUT_AMBIGUOUS", outcome="ASK", findings=("AMBIGUOUS_APP",),
                decision=_ask("AMBIGUOUS_APP", "More than one running app has that name. Which one do you mean?"))
    elif decision.target_index is not None:
        t = ci.targets[decision.target_index]
        identity = (t.role, t.subrole, control_name(t))
        eligible = [x for x in ci.targets if op.value in x.ops and not x.secure and x.enabled is not False
                    and (x.role, x.subrole, control_name(x)) == identity and in_actionable_window(ci, x)]
        if len(eligible) > 1:
            # the USER must name the identifier; a unique identifier the model picked on its own is still a tie-break
            explicit_id = (t.identifier is not None and sum(x.identifier == t.identifier for x in ci.targets) == 1
                           and normalize(t.identifier) in normalize(ci.utterance))
            if not explicit_id:
                return HardenedChooserOutput(
                    assessment="MODEL_OUTPUT_AMBIGUOUS", outcome="ASK", findings=("AMBIGUOUS_TARGET",),
                    decision=_ask("AMBIGUOUS_TARGET", "More than one matching control. Which one?",
                                  [x.index for x in eligible]))
            findings.append("DISAMBIGUATED_BY_UNIQUE_IDENTIFIER")

    # 4. text provenance: derive the exact span in code when the grammar makes it deterministic
    if op is Op.SET_TEXT:
        derived = text_payload_span(ci.utterance)
        if derived is None:
            findings.append("TEXT_SPAN_UNVERIFIED")          # still a validated substring of the utterance
        elif tuple(decision.args.text_span) != derived or decision.args.mode != TEXT_MODE:
            if tuple(decision.args.text_span) != derived:
                findings.append("TEXT_SPAN_MISMATCH")
            if decision.args.mode != TEXT_MODE:
                findings.append("TEXT_MODE_MISMATCH")
            safe = decision.model_copy(update={"args": SetTextArgs(text_span=derived, mode=TEXT_MODE)})
            return HardenedChooserOutput(assessment="MODEL_OUTPUT_INCORRECT", outcome="SAFE_NORMALIZED_ACTION",
                                         decision=safe, findings=tuple(findings + ["TEXT_SPAN_DERIVED_FROM_GRAMMAR"]))
    return HardenedChooserOutput(assessment="VALID", outcome="SAFE_NORMALIZED_ACTION", decision=decision,
                                 findings=tuple(findings))
