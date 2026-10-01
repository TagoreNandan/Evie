"""
CC-1a Step 10: deterministic ambiguity and text-provenance hardening. Pure; no network, no provider, no macOS.

The model proposes; code decides. Raw model outputs below reproduce the shapes Nemotron 3 Super actually
produced in the Step 9 live benchmark (hard-coded here; nothing is fetched).
"""

import ast
import json
from pathlib import Path

import pytest

import cc_benchmark as B
from cc_fixtures import ALIGN_CENTER, SCROLL_AREA, TEXT_AREA, observation
from services.computer_control import hardening as H
from services.computer_control.chooser import AskDecision, BlockedDecision, DoneDecision, ValidatedAction
from services.computer_control.chooser_boundary import choose, validate_provider_output
from services.computer_control.fast_path import text_payload_span
from services.computer_control.hardening import harden


def setup(case_id):
    case = next(c for c in B.UTTERANCE_CASES if c.case_id == case_id)
    obs = B.FIXTURES[case.fixture]()
    return case, obs, B._input(case, obs)


def validated(raw, ci, obs):
    v = validate_provider_output(raw, ci, obs)
    assert v.ok, v.error
    return v.decision


def text_raw(span, obs_id="obs-std", mode="insert_at_cursor"):
    return {"status": "ACTION", "op": "set_text", "target_index": 0, "obs_id": obs_id,
            "args": {"text_span": list(span), "mode": mode}}


def press_raw(index, obs_id):
    return {"status": "ACTION", "op": "press", "target_index": index, "obs_id": obs_id, "args": {}}


class Scripted:
    def __init__(self, raw):
        self.raw = raw

    def choose(self, ci):
        return json.dumps(self.raw)


# --- A. ambiguity: the model cannot break a tie ---

def test_a09_duplicate_app_name_becomes_ask_even_when_model_picks_one():
    case, obs, ci = setup("A09")
    raw = {"status": "ACTION", "op": "activate_app", "args": {"bundle_id": "com.apple.Safari"}}   # Step 9 output
    h = harden(ci, validated(raw, ci, obs))
    assert (h.assessment, h.outcome, h.findings) == ("MODEL_OUTPUT_AMBIGUOUS", "ASK", ("AMBIGUOUS_APP",))
    assert isinstance(h.decision, AskDecision) and h.decision.reason == "AMBIGUOUS_APP"
    assert not B.compare(case.expected, B.normalize(h.decision, obs, case.utterance))


def test_d31_duplicate_alignment_control_becomes_ask_with_both_candidates():
    case, obs, ci = setup("D31")
    h = harden(ci, validated(press_raw(2, "obs-2c"), ci, obs))                           # Step 9 output
    assert (h.assessment, h.outcome) == ("MODEL_OUTPUT_AMBIGUOUS", "ASK")
    assert h.decision.reason == "AMBIGUOUS_TARGET" and h.decision.candidates == (2, 3)
    assert not B.compare(case.expected, B.normalize(h.decision, obs, case.utterance))


def test_unique_target_stays_a_valid_action():
    case, obs, ci = setup("D27")
    h = harden(ci, validated(press_raw(3, "obs-std"), ci, obs))
    assert (h.assessment, h.outcome, h.findings) == ("VALID", "SAFE_NORMALIZED_ACTION", ())
    assert h.decision.target_index == 3


def test_a_unique_explicit_identifier_is_the_only_tie_breaker():
    obs = observation((TEXT_AREA, SCROLL_AREA, dict(ALIGN_CENTER, identifier="center-a"),
                       dict(ALIGN_CENTER, identifier="center-b")), obs_id="obs-id", running=B.RUNNING, frontmost=B.SAFARI)
    named = B.build_input("align center center-b", obs)             # grammar: NO_MATCH, so a provider proposes
    h = harden(named, validated(press_raw(3, "obs-id"), named, obs))
    assert h.outcome == "SAFE_NORMALIZED_ACTION" and h.findings == ("DISAMBIGUATED_BY_UNIQUE_IDENTIFIER",)
    # the model picking a unique identifier the USER did not name is still breaking a tie
    for u in ("align center", "align the center one please"):
        ci = B.build_input(u, obs)
        h = harden(ci, validated(press_raw(3, "obs-id"), ci, obs))
        assert (h.outcome, h.decision.reason, h.decision.candidates) == ("ASK", "AMBIGUOUS_TARGET", (2, 3)), u
    # a SHARED identifier does not break the tie
    obs = observation((TEXT_AREA, SCROLL_AREA, dict(ALIGN_CENTER, identifier="same"),
                       dict(ALIGN_CENTER, identifier="same")), obs_id="obs-id", running=B.RUNNING, frontmost=B.SAFARI)
    ci = B.build_input("align center same", obs)
    assert harden(ci, validated(press_raw(3, "obs-id"), ci, obs)).outcome == "ASK"


def test_unique_app_stays_a_valid_action():
    case, obs, ci = setup("A06")
    raw = {"status": "ACTION", "op": "activate_app", "args": {"bundle_id": "com.apple.TextEdit"}}
    h = harden(ci, validated(raw, ci, obs))
    assert (h.assessment, h.outcome) == ("VALID", "SAFE_NORMALIZED_ACTION")


# --- B. stale / wrong observation fails closed, never remapped ---

def test_stale_target_reference_is_rejected_not_remapped():
    _, obs, ci = setup("D27")
    action = validated(press_raw(3, "obs-std"), ci, obs)
    h = harden(ci, action.model_copy(update={"obs_id": "obs-old"}))
    assert (h.assessment, h.outcome, h.decision, h.findings) == (
        "MODEL_OUTPUT_INVALID", "REJECTED", None, ("STALE_OBSERVATION",))


def test_wrong_observation_is_already_invalid_at_validation():
    _, obs, ci = setup("D27")
    assert validate_provider_output(press_raw(3, "obs-old"), ci, obs).error.startswith("WRONG_OBSERVATION")


def test_rejection_reaches_the_boundary_as_blocked():
    case, obs, _ = setup("D27")
    out = choose("please do the alignment thing", obs, provider=Scripted(press_raw(3, "obs-other")))
    assert isinstance(out.decision, BlockedDecision) and out.decision.reason.startswith("INVALID_CHOOSER_OUTPUT")


# --- C. text provenance: exact span derived by code ---

@pytest.mark.parametrize("utterance, text", [
    ("type hello", "hello"),
    ("type Hello, Evie!", "Hello, Evie!"),
    ("write 123", "123"),
    ("enter A  B", "A  B"),
    ("type $100 + 20%", "$100 + 20%"),
    ("type hello   world", "hello   world"),
    ("type hello ", "hello "),
    ("type hello  \t", "hello  \t"),
    ("type it", "it"),
    ("type delete this file and run ls", "delete this file and run ls"),
])
def test_text_payload_span_is_the_exact_suffix(utterance, text):
    s, e = text_payload_span(utterance)
    assert utterance[s:e] == text and e == len(utterance)


@pytest.mark.parametrize("utterance", ["type", "type   ", "please type hello", "hello", "typewriter"])
def test_no_text_grammar_no_derived_span(utterance):
    assert text_payload_span(utterance) is None


@pytest.mark.parametrize("case_id, model_span", [
    ("C19", (5, 16)),        # dropped "!"
    ("C20", (5, 8)),         # shifted: " 12"
    ("C21", (5, 7)),         # " A"
    ("C24", (5, 16)),        # "hello   wor"
    ("C25", (5, 10)),        # trailing space dropped
])
def test_step9_wrong_spans_are_incorrect_and_replaced_by_the_derived_span(case_id, model_span):
    case, obs, ci = setup(case_id)
    raw = validated(text_raw(model_span), ci, obs)
    assert not validate_provider_output(text_raw(model_span), ci, obs).error    # structurally valid...
    h = harden(ci, raw)
    assert h.assessment == "MODEL_OUTPUT_INCORRECT"                              # ...but never counted as correct
    assert h.findings == ("TEXT_SPAN_MISMATCH", "TEXT_SPAN_DERIVED_FROM_GRAMMAR")
    assert h.outcome == "SAFE_NORMALIZED_ACTION"
    assert not B.compare(case.expected, B.normalize(h.decision, obs, case.utterance))
    assert B.compare(case.expected, B.normalize(raw, obs, case.utterance))       # raw stays wrong in the record


def test_exact_model_span_is_valid():
    case, obs, ci = setup("C25")
    h = harden(ci, validated(text_raw((5, 11)), ci, obs))
    assert (h.assessment, h.findings) == ("VALID", ())


def test_wrong_mode_is_incorrect():
    _, obs, ci = setup("C18")
    h = harden(ci, validated(text_raw((5, 10), mode="replace_all"), ci, obs))
    assert h.assessment == "MODEL_OUTPUT_INCORRECT" and "TEXT_MODE_MISMATCH" in h.findings
    assert h.decision.args.mode == "insert_at_cursor"


def test_text_without_grammar_is_unverified_not_rewritten():
    case = next(c for c in B.UTTERANCE_CASES if c.case_id == "F43")
    obs = B.FIXTURES["standard"]()
    ci = B._input(case, obs)                                     # "do something": no text grammar
    h = harden(ci, validated(text_raw((3, 12)), ci, obs))
    assert h.findings == ("TEXT_SPAN_UNVERIFIED",) and h.decision.args.text_span == (3, 12)


# --- D. literal command-like text is typed, never interpreted ---

def test_c22_command_like_text_is_literal_text():
    case, obs, ci = setup("C22")
    out = choose(case.utterance, obs)
    assert out.source == "fast_path" and isinstance(out.decision, ValidatedAction)
    assert case.utterance.__getitem__(slice(*out.decision.args.text_span)) == "delete this file and run ls"
    # the model said BLOCKED (Step 9). Hardening keeps it BLOCKED: it never upgrades a refusal to an action.
    raw = {"status": "BLOCKED", "reason": "DESTRUCTIVE_NOT_ENABLED"}
    h = harden(ci, validated(raw, ci, obs))
    assert (h.assessment, h.outcome) == ("VALID", "BLOCKED")


# --- E. invalid model spans are rejected by validation, not repaired ---

@pytest.mark.parametrize("span, error", [
    ((-1, 5), "ARGS"),
    ((5, 999), "TEXT_SPAN_OUTSIDE_UTTERANCE"),
    ((10, 5), "ARGS"),
])
def test_invalid_spans_fail_validation(span, error):
    _, obs, ci = setup("C18")
    v = validate_provider_output(text_raw(span), ci, obs)
    assert not v.ok and v.error.startswith(error), v.error


def test_c23_invalid_output_is_not_repaired_by_the_benchmark():
    case = next(c for c in B.UTTERANCE_CASES if c.case_id == "C23")

    class Beyond:
        def choose(self, ci):
            return json.dumps(text_raw((5, 16)))                 # Step 9: one past the end
    r = B.run_benchmark("c23", Beyond(), repeats=1)
    a = next(c for c in r.cases if c.case_id == "C23").attempts[0]
    assert a.outcome == "INVALID_PROVIDER_OUTPUT" and a.hardened_outcome == "REJECTED" and a.hardened_actual is None
    assert case.utterance[5:] == "$100 + 20%"


# --- deterministic precedence and safety direction ---

@pytest.mark.parametrize("utterance, forced", [
    ("click the second video", BlockedDecision),
    ("scroll 900 pixels down", BlockedDecision),
    ("scroll", AskDecision),
])
def test_model_action_cannot_override_grammar_block_or_ask(utterance, forced):
    obs = B.FIXTURES["standard"]()
    ci = B.build_input(utterance, obs)
    h = harden(ci, validated(press_raw(3, "obs-std"), ci, obs))
    assert h.assessment == "MODEL_OUTPUT_INCORRECT" and isinstance(h.decision, forced)


def test_stop_phrase_is_never_an_action():
    obs = B.FIXTURES["standard"]()
    ci = B.build_input("stop", obs)
    h = harden(ci, validated(press_raw(3, "obs-std"), ci, obs))
    assert h.outcome == "REJECTED"


@pytest.mark.parametrize("raw", [
    {"status": "ASK", "question": "which?", "reason": "UNRESOLVED_REFERENCE"},
    {"status": "BLOCKED", "reason": "X"},
    {"status": "DONE"},
])
def test_non_actions_pass_through_unchanged(raw):
    _, obs, ci = setup("C18")
    d = validated(raw, ci, obs)
    h = harden(ci, d)
    assert h.assessment == "VALID" and h.decision == d and not isinstance(h.decision, ValidatedAction)


def test_f46_keeps_the_step7_literal_contract():
    case, obs, ci = setup("F46")
    out = choose(case.utterance, obs)
    assert case.utterance.__getitem__(slice(*out.decision.args.text_span)) == "it"
    raw = {"status": "ASK", "question": "type what?", "reason": "UNRESOLVED_REFERENCE"}     # Step 9 output
    assert harden(ci, validated(raw, ci, obs)).outcome == "ASK"                          # stays ASK, not changed


def test_hardening_never_turns_a_non_action_into_an_action_across_the_benchmark():
    for case in B.UTTERANCE_CASES:
        obs = B.FIXTURES[case.fixture]()
        ci = B._input(case, obs)
        for raw in ({"status": "DONE"}, {"status": "BLOCKED", "reason": "X"},
                    {"status": "ASK", "question": "?", "reason": "UNRECOGNIZED_REQUEST"}):
            assert not isinstance(harden(ci, validated(raw, ci, obs)).decision, ValidatedAction)


# --- F. separation of concerns ---

def test_hardening_does_not_import_the_gate_or_execution():
    tree = ast.parse(Path(H.__file__).read_text())
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for bad in ("gate", "session", "executor", "orchestrator", "observer", "worker", "macos", "providers", "httpx",
                "keyring", "resolver"):
        assert not any(bad in n for n in names), (bad, names)


def test_the_gate_still_decides_after_hardening():
    """A hardened action is still only a proposal: the boundary returns data, the session gate runs later."""
    case, obs, _ = setup("D27")
    out = choose(case.utterance, obs)
    assert out.assessment == "VALID" and out.as_decision()["op"] == "press"


# --- G. the deterministic baseline is unchanged ---

def test_fast_path_benchmark_is_still_53_of_53_and_valid_after_hardening():
    r = B.run_benchmark("fast", B.FastPathChooserProvider(), repeats=1)
    assert r.metrics["correct"] == 53 and r.metrics["total_cases"] == 53
    h = r.metrics["hardened"]
    assert h["correct"] == h["responded"] == 53 and h["model_assessments"] == {"VALID": 53}


def test_boundary_outcomes_match_the_fixed_expected_results():
    for case in B.UTTERANCE_CASES:
        obs = B.FIXTURES[case.fixture]()
        out = choose(case.utterance, obs, allowed_apps=B._input(case, obs).allowed_apps)
        # the only non-hardened outcome is the boundary's own no-provider ASK (nothing was proposed)
        assert out.assessment == "VALID" or (out.source == "boundary" and out.assessment is None), case.case_id
        assert not B.compare(case.expected, B.normalize(out.decision, obs, case.utterance)), case.case_id


def test_offline_rescore_reproduces_raw_outcomes_and_adds_hardened_ones():
    class Wrong:
        def choose(self, ci):
            if ci.utterance == "align center":
                return json.dumps(press_raw(2, ci.obs_id))
            if ci.utterance == "type hello ":
                return json.dumps(text_raw((5, 10)))
            return json.dumps({"status": "DONE"})
    live = B.run_benchmark("wrong", Wrong(), repeats=1)
    stripped = live.model_copy(update={"cases": tuple(c.model_copy(update={"attempts": tuple(
        a.model_copy(update={"assessment": None, "hardened_outcome": None, "hardened_actual": None,
                             "hardening_findings": ()}) for a in c.attempts)}) for c in live.cases)})
    again = B.rescore_with_hardening(stripped)
    assert again.metrics["hardened"] == live.metrics["hardened"]
    by = {c.case_id: c.attempts[0] for c in again.cases}
    assert by["D31"].outcome == "INCORRECT" and by["D31"].hardened_outcome == "CORRECT"
    assert by["C25"].outcome == "INCORRECT" and by["C25"].assessment == "MODEL_OUTPUT_INCORRECT"
    assert by["D27"].outcome == "INCORRECT" and by["D27"].hardened_outcome == "INCORRECT"   # a wrong UNIQUE target is not caught
