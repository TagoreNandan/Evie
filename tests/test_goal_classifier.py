"""
Focused test suite for Evie Natural-Language Goal Classifier & Semantic Normalizer (Step 4).
Validates goal normalization (A-F), intent preservation (H-I), ambiguity handling (J-K),
prompt injection / security defense (M-Q), schema validation (R-V), non-action-invention (W),
and regression tests.
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from services.computer_control.classifier import (
    ClassificationKind, ClassificationResult, classify_intent, classify_intent_deterministic, normalize_goal_text
)
from services.computer_control.container import (
    ComputerControlContainer, initialize_production_container
)
from cc_fixtures import TEXTEDIT


class FakeWorkerBackend:
    def __init__(self, is_trusted: bool = True):
        self.is_trusted = is_trusted
        self.executed_actions = []

    def probe(self):
        m = MagicMock()
        m.probe.production_identity_verified = self.is_trusted
        m.probe.production_identity_reasons = [] if self.is_trusted else ["UNTRUSTED_TEST_HOST"]
        m.probe.to_permission_status.return_value = MagicMock()
        return m

    def act(self, request, utterance=None, allowed_apps=None):
        self.executed_actions.append(request)
        res = MagicMock()
        res.status = "OK"
        res.reason = "Success"
        return res


def _mock_observation(app=TEXTEDIT):
    from cc_fixtures import observation
    obs = observation(app=app, frontmost=app)
    return obs, {"handle": 1}


@pytest.fixture
def test_client():
    return TestClient(app)


def test_matrix_a_polite_computer_request():
    """Test A: Polite computer request is normalized into a concise computer goal."""
    res = classify_intent("Could you please open TextEdit for me?")
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert res.goal == "Open TextEdit."


def test_matrix_b_concise_computer_request():
    """Test B: Concise computer request remains concise."""
    res = classify_intent("Open TextEdit.")
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert res.goal == "Open TextEdit."


def test_matrix_c_activation_request():
    """Test C: Application activation request is normalized."""
    res = classify_intent("Please switch over to Calculator.")
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert res.goal in ("Activate Calculator.", "Switch over to Calculator.")


def test_matrix_d_typing_request():
    """Test D: Typing request preserves content without inventing intermediate actions."""
    res = classify_intent("I want to enter Hello Evie into the document.")
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert "Hello Evie" in res.goal
    assert "TextEdit" not in res.goal  # Must NOT invent unrequested app


def test_matrix_e_photo_request():
    """Test E: Photo request is normalized into a goal."""
    res = classify_intent("Can you take a picture?")
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert "picture" in res.goal.lower() or "photo" in res.goal.lower()


def test_matrix_f_scrolling_request():
    """Test F: Scrolling request is normalized into a goal."""
    res = classify_intent("Scroll down.")
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert "Scroll down" in res.goal


def test_matrix_g_natural_conversational_variation():
    """Test G: Natural conversational variation remains CONVERSATION."""
    res = classify_intent("Explain Python decorators.")
    assert res.kind == ClassificationKind.CONVERSATION
    assert res.goal is None


def test_matrix_h_preservation_of_user_text():
    """Test H: Exact user-provided typing text is preserved."""
    norm = normalize_goal_text("I want to enter Hello Evie into the document.")
    assert "Hello Evie" in norm


def test_matrix_i_missing_information_no_action_invention():
    """Test I: Classifier does not invent actions or applications for missing information."""
    norm = normalize_goal_text("I need to write something in a text document.")
    assert "TextEdit" not in norm
    assert "open" not in norm.lower()


def test_matrix_j_ambiguous_pronoun():
    """Test J: Ambiguous pronoun requests default to CONVERSATION."""
    for phrase in ["Open it.", "Do that.", "Save it.", "Use the other one.", "Put it there."]:
        res = classify_intent(phrase)
        assert res.kind == ClassificationKind.CONVERSATION


def test_matrix_k_ambiguous_application_reference():
    """Test K: Bare application names remain CONVERSATION."""
    for app_name in ["TextEdit", "Calculator", "Safari"]:
        res = classify_intent(app_name)
        assert res.kind == ClassificationKind.CONVERSATION


def test_matrix_l_conversation_remains_conversation():
    """Test L: Informational queries remain CONVERSATION."""
    queries = ["How does DNS work?", "What is a binary tree?", "Why is the sky blue?"]
    for q in queries:
        res = classify_intent(q)
        assert res.kind == ClassificationKind.CONVERSATION


def test_matrix_m_shell_injection():
    """Test M: Shell injection in goal string is rejected by validator."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="shell: rm -rf /")


def test_matrix_n_applescript_injection():
    """Test N: AppleScript injection in goal string is rejected by validator."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="applescript: tell app TextEdit to activate")


def test_matrix_o_universalaction_injection():
    """Test O: UniversalAction injection in goal string is rejected by validator."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="UniversalAction(op=Op.CLICK)")


def test_matrix_p_coordinate_injection():
    """Test P: Coordinate injection in goal string is rejected by validator."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="Click at coords=[100, 200]")


def test_matrix_q_selector_injection():
    """Test Q: Selector injection in goal string is rejected by validator."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="Select element selector='#btn'")


def test_matrix_r_s_t_schema_validation():
    """Test R, S, T: Strict Pydantic model rejects unknown kinds and extra fields."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind="UNKNOWN_KIND", goal="Open TextEdit.")

    with pytest.raises(ValidationError):
        ClassificationResult.model_validate({"kind": "COMPUTER_GOAL", "goal": "Open TextEdit.", "extra": "data"})


def test_matrix_u_v_goal_length_and_empty_checks():
    """Test U & V: Goal length > 500 or empty goal for COMPUTER_GOAL raises ValidationError."""
    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="a" * 501)

    with pytest.raises(ValidationError):
        ClassificationResult(kind=ClassificationKind.COMPUTER_GOAL, goal="")


def test_matrix_w_classifier_does_not_choose_execution_actions():
    """Test W: Assertions explicitly proving the classifier does NOT choose execution actions."""
    res = classify_intent("Could you please open TextEdit for me?")
    d = res.model_dump()

    # The classifier MUST NOT output execution decisions or targets
    assert "op" not in d
    assert "action" not in d
    assert "target" not in d
    assert "selector" not in d
    assert "coords" not in d
    assert "universal_action" not in d
    assert "universalaction" not in d
    assert res.kind == ClassificationKind.COMPUTER_GOAL
    assert res.goal == "Open TextEdit."
