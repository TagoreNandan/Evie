"""
Computer-control verification contracts (docs/07 Section 11): operation-specific, evidence-based,
and never "API returned success".
"""

from cc_fixtures import CHROME, TEXTEDIT, observation
from services.computer_control.models import VerificationStatus as V
from services.computer_control.verification import (
    ActivationEvidence, ActivationVerifier, AppFrontmostGoal, ControlStateEvidence, ControlStateExpectation,
    ControlStateVerifier, GenericPressVerifier, ScrollEvidence, ScrollVerifier, TextEvidence, TextExpectation,
    TextVerifier,
)

TE = TEXTEDIT.bundle_id


def test_activation_requires_independent_readings_and_own_state():
    v = ActivationVerifier()
    ok = ActivationEvidence(frontmost_readings=(TE, TE, TE), target_is_active=True, target_ax_frontmost=True)
    assert v.verify(TE, ok).status is V.VERIFIED
    single = ActivationEvidence(frontmost_readings=(TE,), target_is_active=True, target_ax_frontmost=True)
    assert v.verify(TE, single).status is V.INCONCLUSIVE
    no_own = ActivationEvidence(frontmost_readings=(TE, TE))
    assert v.verify(TE, no_own).status is V.INCONCLUSIVE
    failed = ActivationEvidence(frontmost_readings=(CHROME.bundle_id,) * 3, target_is_active=False,
                                target_ax_frontmost=False)
    assert v.verify(TE, failed).status is V.VERIFIED_NO_CHANGE
    disagree = ActivationEvidence(frontmost_readings=(TE, CHROME.bundle_id), target_is_active=True)
    assert v.verify(TE, disagree).status is V.INCONCLUSIVE


def test_text_exact_match():
    before = TextEvidence(local_text="EVIE_FIXTURE_MARKER", char_count=19, selection=(0, 0))
    after = TextEvidence(local_text="EVIE_KEYBOARD_TESTEVIE_FIXTURE_MARKER", char_count=37, selection=(18, 0))
    exp = TextExpectation(local_text="EVIE_KEYBOARD_TESTEVIE_FIXTURE_MARKER", char_delta=18, selection=(18, 0))
    assert TextVerifier().verify(before, after, exp).status is V.VERIFIED
    assert TextVerifier().verify(before, before, exp).status is V.VERIFIED_NO_CHANGE
    wrong = TextEvidence(local_text="EVIE_KEYBOARD_TESTXEVIE_FIXTURE_MARKER", char_count=38, selection=(19, 0))
    assert TextVerifier().verify(before, wrong, exp).status is V.INCONCLUSIVE


def test_control_state_needs_state_and_independent_signal():
    before = ControlStateEvidence(values={"align left": 1, "align center": 0}, independent_signal=204.0)
    after = ControlStateEvidence(values={"align left": 0, "align center": 1}, independent_signal=429.6)
    exp = ControlStateExpectation(values={"align left": 0, "align center": 1})
    assert ControlStateVerifier().verify(before, after, exp).status is V.VERIFIED
    no_signal = ControlStateEvidence(values={"align left": 0, "align center": 1}, independent_signal=204.0)
    assert ControlStateVerifier().verify(before, no_signal, exp).status is V.INCONCLUSIVE
    assert ControlStateVerifier().verify(before, before, exp).status is V.VERIFIED_NO_CHANGE


def test_generic_press_is_never_verified():
    g = GenericPressVerifier()
    assert g.verify("fp-1", "fp-2").status is V.CHANGED_UNSPECIFIED
    assert g.verify("fp-1", "fp-1").status is V.VERIFIED_NO_CHANGE


def test_scroll_direction():
    s = ScrollVerifier()
    assert s.verify(ScrollEvidence(visible_start=0), ScrollEvidence(visible_start=2668), "down").status is V.VERIFIED
    assert s.verify(ScrollEvidence(visible_start=10), ScrollEvidence(visible_start=10), "down").status is V.VERIFIED_NO_CHANGE
    assert s.verify(ScrollEvidence(visible_start=10), ScrollEvidence(visible_start=0), "down").status is V.INCONCLUSIVE


def test_app_frontmost_goal():
    assert AppFrontmostGoal(TE).check(observation()) is True
    assert AppFrontmostGoal(CHROME.bundle_id).check(observation()) is False
