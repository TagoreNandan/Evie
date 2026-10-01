"""
Computer-control target resolver (docs/07 Section 8): identifier -> exact -> ordinal -> ASK/BLOCKED.
"""

import pytest
from pydantic import ValidationError

from cc_fixtures import ALIGN_CENTER, ALIGN_LEFT, PLAIN_BUTTON, TEXT_AREA, observation
from services.computer_control.models import ResolutionMethod
from services.computer_control.resolver import TargetSpec, resolve


def test_unique_identifier_wins():
    out = resolve(observation(), TargetSpec(obs_id="obs-1", identifier="first text view"))
    assert out.kind == "RESOLVED" and out.target.method is ResolutionMethod.IDENTIFIER
    assert out.target.target.index == 0 and out.target.obs_id == "obs-1"


def test_exact_role_subrole_label_match():
    out = resolve(observation(), TargetSpec(obs_id="obs-1", role="AXCheckBox", subrole="AXSegment",
                                            label="  Align   CENTER "))
    assert out.kind == "RESOLVED" and out.target.method is ResolutionMethod.EXACT and out.target.target.index == 3


def test_exact_match_requires_exact_subrole_and_context():
    obs = observation()
    assert resolve(obs, TargetSpec(obs_id="obs-1", role="AXCheckBox", label="align center")).kind == "BLOCKED"
    assert resolve(obs, TargetSpec(obs_id="obs-1", role="AXCheckBox", subrole="AXSegment", label="align center",
                                   context=("AXWindow",))).kind == "BLOCKED"


def test_no_fuzzy_matching():
    assert resolve(observation(), TargetSpec(obs_id="obs-1", role="AXCheckBox", subrole="AXSegment",
                                             label="align centre")).reason == "NO_MATCH"


def test_ordinal_against_same_observation():
    out = resolve(observation(), TargetSpec(obs_id="obs-1", index=2))
    assert out.kind == "RESOLVED" and out.target.method is ResolutionMethod.ORDINAL and out.target.target.label == "align left"


def test_ordinal_rejected_against_a_different_observation():
    out = resolve(observation(obs_id="obs-2"), TargetSpec(obs_id="obs-1", index=2))
    assert out.kind == "STALE" and out.target is None


def test_ordinal_out_of_range_and_inconsistent():
    obs = observation()
    assert resolve(obs, TargetSpec(obs_id="obs-1", index=99)).reason == "INDEX_OUT_OF_RANGE"
    out = resolve(obs, TargetSpec(obs_id="obs-1", index=0, role="AXButton", label="OK"))
    assert out.kind == "BLOCKED" and out.reason == "INDEX_INCONSISTENT"


def test_no_match_is_blocked():
    out = resolve(observation(), TargetSpec(obs_id="obs-1", role="AXButton", label="Cancel"))
    assert out.kind == "BLOCKED" and out.reason == "NO_MATCH"
    assert resolve(observation(), TargetSpec(obs_id="obs-1", identifier="nope")).kind == "BLOCKED"


def test_duplicate_match_asks_and_never_chooses():
    obs = observation([PLAIN_BUTTON, TEXT_AREA, PLAIN_BUTTON])
    out = resolve(obs, TargetSpec(obs_id="obs-1", role="AXButton", label="OK"))
    assert out.kind == "ASK" and out.candidates == (0, 2) and out.target is None


def test_duplicate_identifier_asks():
    dup = dict(PLAIN_BUTTON, identifier="_NS:1")
    out = resolve(observation([dup, dup]), TargetSpec(obs_id="obs-1", identifier="_NS:1"))
    assert out.kind == "ASK" and out.candidates == (0, 1)


def test_explicit_index_disambiguates_duplicates_only_when_consistent():
    obs = observation([PLAIN_BUTTON, TEXT_AREA, PLAIN_BUTTON])
    out = resolve(obs, TargetSpec(obs_id="obs-1", role="AXButton", label="OK", index=2))
    assert out.kind == "RESOLVED" and out.target.target.index == 2


def test_disabled_target_is_blocked():
    obs = observation([dict(ALIGN_LEFT, enabled=False), ALIGN_CENTER])
    out = resolve(obs, TargetSpec(obs_id="obs-1", index=0))
    assert out.kind == "BLOCKED" and out.reason == "TARGET_DISABLED"


def test_spec_needs_a_key():
    with pytest.raises(ValidationError):
        TargetSpec(obs_id="obs-1", role="AXButton")
