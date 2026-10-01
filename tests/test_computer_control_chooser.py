"""
Computer-control chooser output schema (docs/07 Section 13), Step 1 contract.
No provider exists; these tests validate raw chooser-format data only.
The Step 7 fast path and chooser boundary are tested in test_computer_control_fast_path.py.
"""

import json

import pytest

from cc_fixtures import UTTERANCE, observation
from services.computer_control.chooser import (
    AskDecision, BlockedDecision, DoneDecision, ValidatedAction, build_input, validate_decision,
)
from services.computer_control.models import SetTextArgs
from services.computer_control.provenance import text_for


def _check(raw, utterance=UTTERANCE, obs=None):
    return validate_decision(raw, utterance=utterance, observation=obs or observation())


# --- Valid decisions ---

def test_valid_action():
    v = _check({"status": "ACTION", "op": "set_text", "target_index": 0,
                "args": {"text_span": [5, 16], "mode": "insert_at_cursor"}})
    assert v.ok and isinstance(v.decision, ValidatedAction) and isinstance(v.decision.args, SetTextArgs)
    assert text_for(UTTERANCE, v.decision.args) == "hello world"


def test_valid_json_string_action():
    assert _check(json.dumps({"status": "ACTION", "op": "press", "target_index": 3})).ok


def test_done_blocked_ask():
    assert isinstance(_check({"status": "DONE"}).decision, DoneDecision)
    assert isinstance(_check({"status": "BLOCKED", "reason": "nothing to do"}).decision, BlockedDecision)
    v = _check({"status": "ASK", "question": "Which\x07 document?"})
    assert isinstance(v.decision, AskDecision) and "\x07" not in v.decision.question


# --- Rejections ---

@pytest.mark.parametrize("raw, error", [
    ({"status": "DONE", "note": "x"}, "SCHEMA"),
    ({"status": "ACTION", "op": "press", "target_index": 3, "confidence": 0.9}, "SCHEMA"),
    ({"status": "ACTION", "op": "shell", "args": {"cmd": "rm -rf ~"}}, "SCHEMA"),
    ({"status": "ACTION", "op": "press", "target_index": 3, "shell": "open -a Terminal"}, "SCHEMA"),
    ({"status": "ACTION", "op": "press", "args": {"x": 120, "y": 44}}, "ARGS"),
    ({"status": "ACTION", "op": "press", "target_index": 3, "args": {"selector": "#buy"}}, "ARGS"),
    ({"status": "ACTION", "op": "set_text", "target_index": 0, "args": {"text_span": [0, 4], "script": "osascript"}}, "ARGS"),
    ({"status": "ACTION", "op": "set_text", "target_index": 0, "args": {"text_span": [0, 4], "text": "evil"}}, "ARGS"),
    ({"status": "ACTION", "op": "open_url", "args": {}}, "DISABLED_OPERATION"),
    ({"status": "ACTION", "op": "open_url", "args": {}}, "DISABLED_OPERATION"),
    ({"status": "ACTION", "op": "press", "target_index": 99}, "TARGET_INDEX_OUT_OF_RANGE"),
    ({"status": "ACTION", "op": "press"}, "TARGET_INDEX_REQUIRED"),
    ({"status": "ACTION", "op": "set_text", "target_index": 0, "args": {"text_span": [5, 999]}}, "TEXT_SPAN_OUTSIDE_UTTERANCE"),
    ({"status": "ACTION", "op": "set_text", "target_index": 0, "args": {"text_span": [3, 3]}}, "TEXT_SPAN_EMPTY"),
    ({"status": "ACTION", "op": "activate_app", "target_index": 1, "args": {"bundle_id": "com.apple.TextEdit"}},
     "TARGET_INDEX_NOT_ALLOWED"),
    ({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": "com.example.notrunning"}}, "APP_NOT_RUNNING"),
    ({"op": "press", "target_index": 3}, "SCHEMA"),
    ("not json", "MALFORMED_JSON"),
    ({"status": "ASK", "question": "x" * 201}, "SCHEMA"),
])
def test_rejected_outputs(raw, error):
    v = _check(raw)
    assert not v.ok and v.decision is None and v.error.startswith(error)


def test_chooser_input_is_minimal_and_serialisable():
    obs = observation()
    ci = build_input(UTTERANCE, obs, recent=[], step=1, budget_remaining=0)
    assert [a.bundle_id for a in ci.running_apps] == [a.bundle_id for a in obs.running_apps]
    assert set(ci.model_dump()) == {"obs_id", "utterance", "frontmost", "app", "windows", "targets", "running_apps",
                                    "allowed_apps", "recent_actions", "step", "budget_remaining",
                                    "screen_text_is_data"}
    assert "frame" not in ci.model_dump()["targets"][0]
