"""
Operation-specific verification contracts (docs/07_Computer_Control.md Section 11).

Pure comparisons of evidence the (future) worker collects before and after an action. There is
no universal "did it work?" check: each verifier states what it needs and returns VERIFIED only
on that evidence. A plain button press has no expected-effect predicate, so GenericPressVerifier
can never return VERIFIED - at best CHANGED_UNSPECIFIED, which maps to NOT_VERIFIABLE.
"""

from typing import Dict, Literal, Optional, Protocol, Tuple

from pydantic import Field

from services.computer_control.models import EvidenceValue, Observation, VerificationStatus, _Strict


class VerificationResult(_Strict):
    status: VerificationStatus
    reason: str = Field(..., min_length=1, max_length=64)
    evidence: Dict[str, EvidenceValue] = Field(default_factory=dict, max_length=20)


def _result(status: VerificationStatus, reason: str, **evidence) -> VerificationResult:
    return VerificationResult(status=status, reason=reason, evidence=evidence)


# --- Activation ---

class ActivationEvidence(_Strict):
    frontmost_readings: Tuple[str, ...] = Field(..., min_length=1, max_length=5)   # bundle ids, independent sources
    target_is_active: Optional[bool] = None
    target_ax_frontmost: Optional[bool] = None


class ActivationVerifier:
    MIN_INDEPENDENT_READINGS = 2

    def verify(self, expected_bundle: str, after: ActivationEvidence) -> VerificationResult:
        readings = after.frontmost_readings
        own = (after.target_is_active, after.target_ax_frontmost)
        if (len(readings) >= self.MIN_INDEPENDENT_READINGS and all(r == expected_bundle for r in readings)
                and True in own and False not in own):
            return _result(VerificationStatus.VERIFIED, "FRONTMOST_ON_ALL_READINGS", readings=len(readings))
        if all(r != expected_bundle for r in readings) and True not in own:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "TARGET_NOT_FRONTMOST")
        return _result(VerificationStatus.INCONCLUSIVE, "READINGS_DISAGREE", readings=len(readings))


# --- Text ---

class TextEvidence(_Strict):
    """Text in the affected local range (not the whole document), total count, and selection."""
    local_text: str = Field(..., max_length=10_000)
    char_count: int = Field(..., ge=0)
    selection: Tuple[int, int]


class TextExpectation(_Strict):
    local_text: str = Field(..., max_length=10_000)
    char_delta: int
    selection: Optional[Tuple[int, int]] = None


class TextVerifier:
    def verify(self, before: TextEvidence, after: TextEvidence, expected: TextExpectation) -> VerificationResult:
        delta = after.char_count - before.char_count
        if (after.local_text == expected.local_text and delta == expected.char_delta
                and (expected.selection is None or after.selection == expected.selection)):
            return _result(VerificationStatus.VERIFIED, "EXACT_TEXT", char_delta=delta)
        if after == before:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "TEXT_UNCHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "TEXT_MISMATCH", char_delta=delta)


# --- Known-state controls ---

class ControlStateEvidence(_Strict):
    values: Dict[str, int] = Field(..., min_length=1, max_length=20)   # e.g. {"align left": 1, "align center": 0}
    independent_signal: Optional[float] = None                         # e.g. first-glyph x from text layout


class ControlStateExpectation(_Strict):
    values: Dict[str, int] = Field(..., min_length=1, max_length=20)
    independent_signal_must_change: bool = True


class ControlStateVerifier:
    def verify(self, before: ControlStateEvidence, after: ControlStateEvidence,
               expected: ControlStateExpectation) -> VerificationResult:
        signal_known = before.independent_signal is not None and after.independent_signal is not None
        signal_changed = signal_known and before.independent_signal != after.independent_signal
        if after.values == expected.values and (not expected.independent_signal_must_change or signal_changed):
            return _result(VerificationStatus.VERIFIED, "STATE_AND_SIGNAL", signal_changed=signal_changed)
        if after.values == before.values and not signal_changed:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "STATE_UNCHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "STATE_MISMATCH", signal_changed=signal_changed)


class GenericPressVerifier:
    """Plain buttons/links: no expected-effect predicate exists, so success is never claimed."""

    def verify(self, before_fingerprint: str, after_fingerprint: str) -> VerificationResult:
        if before_fingerprint == after_fingerprint:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "NO_OBSERVED_CHANGE")
        return _result(VerificationStatus.CHANGED_UNSPECIFIED, "CHANGE_WITHOUT_PREDICATE")


# --- Scroll ---

class CaretVerifier:
    """press_key: the caret (empty selection) moved by exactly `delta` and the text is identical."""

    def verify(self, before: Tuple[str, Tuple[int, int]], after: Tuple[str, Tuple[int, int]],
               delta: int) -> VerificationResult:
        (text_b, (loc_b, len_b)), (text_a, (loc_a, len_a)) = before, after
        if text_a != text_b:
            return _result(VerificationStatus.INCONCLUSIVE, "TEXT_CHANGED")
        if (loc_a, len_a) == (loc_b + delta, 0):
            return _result(VerificationStatus.VERIFIED, "CARET_MOVED_EXACTLY", moved=delta)
        if (loc_a, len_a) == (loc_b, len_b):
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "CARET_UNCHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "CARET_MOVED_UNEXPECTEDLY", moved=loc_a - loc_b)


class ScrollEvidence(_Strict):
    visible_start: int   # AXVisibleCharacterRange location, or the content's offset above the scroll area's top


class ScrollVerifier:
    def verify(self, before: ScrollEvidence, after: ScrollEvidence,
               direction: Literal["up", "down"]) -> VerificationResult:
        moved = after.visible_start - before.visible_start
        if moved == 0:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "NOT_MOVED")
        if (moved > 0) == (direction == "down"):
            return _result(VerificationStatus.VERIFIED, "MOVED_AS_REQUESTED", moved=moved)
        return _result(VerificationStatus.INCONCLUSIVE, "MOVED_OPPOSITE", moved=moved)


# --- Phase 2 Universal Action Substrate Verifiers ---

class FocusVerifier:
    def verify(self, before_focused: bool, after_focused: bool) -> VerificationResult:
        if after_focused is True:
            return _result(VerificationStatus.VERIFIED, "ELEMENT_FOCUSED")
        if not after_focused and not before_focused:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "FOCUS_UNCHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "FOCUS_MISMATCH")


class SelectionVerifier:
    def verify(self, before_selected: Optional[bool], after_selected: Optional[bool]) -> VerificationResult:
        if after_selected is True:
            return _result(VerificationStatus.VERIFIED, "ELEMENT_SELECTED")
        if after_selected == before_selected:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "SELECTION_UNCHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "SELECTION_MISMATCH")


class MenuItemVerifier:
    def verify(self, before_fingerprint: str, after_fingerprint: str) -> VerificationResult:
        if before_fingerprint == after_fingerprint:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "NO_OBSERVED_CHANGE")
        return _result(VerificationStatus.VERIFIED, "MENU_ACTION_EXECUTED")


class ClipboardVerifier:
    """Metadata only: the pasteboard change count and a length comparison. Contents are never kept or logged."""
    def verify(self, changed: bool, length_matches: bool) -> VerificationResult:
        if not changed:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "CLIPBOARD_UNCHANGED")
        if length_matches:
            return _result(VerificationStatus.VERIFIED, "CLIPBOARD_HOLDS_SELECTION")
        return _result(VerificationStatus.INCONCLUSIVE, "CLIPBOARD_LENGTH_MISMATCH")


class PasteVerifier:
    """The field's text must equal the old text with the clipboard text at the selection - compared in memory."""
    def verify(self, before_text: str, after_text: Optional[str], expected_text: str) -> VerificationResult:
        if after_text is None:
            return _result(VerificationStatus.INCONCLUSIVE, "TEXT_UNREADABLE_AFTER")
        if after_text == expected_text:
            return _result(VerificationStatus.VERIFIED, "PASTED_CLIPBOARD_TEXT")
        if after_text == before_text:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "TEXT_UNCHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "TEXT_CHANGED_UNEXPECTEDLY")


class ClickVerifier:
    """A press is proven only by an observable change: the control's own state, or the app's re-observed UI."""
    def verify(self, control_changed: bool, ui_changed: bool) -> VerificationResult:
        if control_changed:
            return _result(VerificationStatus.VERIFIED, "CONTROL_STATE_CHANGED")
        if ui_changed:
            return _result(VerificationStatus.VERIFIED, "UI_STATE_CHANGED")
        return _result(VerificationStatus.INCONCLUSIVE, "NO_OBSERVABLE_CHANGE")


class StateConditionVerifier:
    def verify(self, condition_met: bool) -> VerificationResult:
        if condition_met:
            return _result(VerificationStatus.VERIFIED, "CONDITION_MET")
        return _result(VerificationStatus.VERIFIED_NO_CHANGE, "CONDITION_NOT_MET")


class QuitAppVerifier:
    def verify(self, is_running_after: bool) -> VerificationResult:
        if not is_running_after:
            return _result(VerificationStatus.VERIFIED, "APP_TERMINATED")
        return _result(VerificationStatus.INCONCLUSIVE, "APP_STILL_RUNNING")


class CloseWindowVerifier:
    def verify(self, window_closed: bool, app_running: bool) -> VerificationResult:
        if window_closed and app_running:
            return _result(VerificationStatus.VERIFIED, "WINDOW_CLOSED_APP_RUNNING")
        if not window_closed:
            return _result(VerificationStatus.VERIFIED_NO_CHANGE, "WINDOW_STILL_PRESENT")
        return _result(VerificationStatus.INCONCLUSIVE, "APP_TERMINATED_INSTEAD_OF_WINDOW")


# --- Goal checks for DONE (docs/07 Section 11) ---

class GoalCheck(Protocol):
    """Returns True (goal met), False (goal not met), or None (this check does not apply)."""
    def check(self, observation: Observation) -> Optional[bool]: ...


class AppNotRunningGoal:
    """quit_app's goal, checked on a FRESH observation of another app: the bundle is no longer running."""
    def __init__(self, bundle_id: str):
        self.bundle_id = bundle_id

    def check(self, observation: Observation) -> Optional[bool]:
        return all(a.bundle_id != self.bundle_id for a in observation.running_apps)


class AppFrontmostGoal:
    def __init__(self, bundle_id: str):
        self.bundle_id = bundle_id

    def check(self, observation: Observation) -> Optional[bool]:
        return observation.frontmost is not None and observation.frontmost.bundle_id == self.bundle_id
