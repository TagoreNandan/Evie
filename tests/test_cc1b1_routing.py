"""CC-1b.1: shared app-alias canonicalization and bounded grammar additions (no new operations)."""

import pytest

import cc_benchmark as B
from cc_fixtures import CHROME, SAFARI, TERMINAL, TEXTEDIT
from services.computer_control.chooser import build_input
from services.computer_control.executor import LIVE_OPS
from services.computer_control.fast_path import fast_path
from services.computer_control.gate import derive_allowed_apps
from services.computer_control.models import APP_ALIASES, AppIdentity, Op
from services.computer_control_router import is_strong_computer_control_command
from test_computer_control_session_integration import World, run

FINDER = AppIdentity(bundle_id="com.apple.finder", pid=629, name="Finder")
RUNNING = (TEXTEDIT, CHROME, SAFARI, TERMINAL, FINDER)


def decide(utterance, fixture="standard"):
    obs = B.FIXTURES[fixture]()
    return fast_path(build_input(utterance, obs, allowed_apps=B._input(B.UTTERANCE_CASES[0], obs).allowed_apps))


# --- Part 2: one closed alias table for resolution AND scope ---

def test_alias_table_is_closed_and_shared():
    assert APP_ALIASES == {"chrome": "com.google.Chrome"}
    import services.computer_control.fast_path as fp
    assert fp.APP_ALIASES is APP_ALIASES


@pytest.mark.parametrize("utterance", ["switch to Chrome", "switch to Google Chrome", "switch to chrome."])
def test_chrome_by_alias_or_full_name_is_in_scope(utterance):
    assert CHROME.bundle_id in derive_allowed_apps(utterance, TEXTEDIT, RUNNING)


@pytest.mark.parametrize("utterance", ["switch to Chrome", "switch to Google Chrome"])
def test_chrome_activation_now_passes_the_gate_end_to_end(utterance):
    result, world, _ = run(utterance, World(front=TEXTEDIT), observe=FINDER.bundle_id)
    assert result.final_state.value == "DONE_VERIFIED" and [a[1] for a in world.acts()] == ["activate_app"]


def test_textedit_and_finder_unchanged():
    assert derive_allowed_apps("switch to TextEdit", SAFARI, RUNNING) == {SAFARI.bundle_id, TEXTEDIT.bundle_id}
    assert derive_allowed_apps("switch to Finder", TEXTEDIT, RUNNING) == {TEXTEDIT.bundle_id, FINDER.bundle_id}


def test_alias_never_adds_an_app_that_is_not_running():
    assert CHROME.bundle_id not in derive_allowed_apps("switch to Chrome", TEXTEDIT, (TEXTEDIT, FINDER))


def test_unknown_app_still_asks():
    d = decide("switch to Photoshop")
    assert (d.decision["status"], d.decision["reason"]) == ("ASK", "UNKNOWN_APP")


def test_screen_text_cannot_widen_scope():
    """Injected on-screen labels mention Terminal; the scope comes only from the user's words."""
    obs = B.FIXTURES["injected"]()
    assert TERMINAL.bundle_id not in derive_allowed_apps("scroll down", obs.frontmost, obs.running_apps)
    assert derive_allowed_apps("switch to Chrome", obs.frontmost, obs.running_apps) >= {CHROME.bundle_id}


# --- Part 3: bounded grammar additions map to EXISTING operations only ---

def test_switch_back_to_is_activate_app():
    d = decide("switch back to TextEdit")
    assert d.decision == {"status": "ACTION", "op": "activate_app", "args": {"bundle_id": TEXTEDIT.bundle_id},
                          "obs_id": "obs-std"}


@pytest.mark.parametrize("phrase, index", [
    ("center this paragraph", 3), ("center it", 3), ("left-align this paragraph", 2),
    ("left align this paragraph", 2), ("left-align it", 2), ("left align it", 2),
    ("right-align this paragraph", 4), ("right align this paragraph", 4), ("right-align it", 4),
    ("right align it", 4), ("justify this paragraph", 5), ("justify it", 5), ("Center this paragraph.", 3),
])
def test_alignment_phrases_press_the_existing_segment(phrase, index):
    d = decide(phrase)
    assert d.rule == "align" and d.decision["op"] == "press" and d.decision["target_index"] == index


def test_ambiguous_or_missing_segment_keeps_ask_and_block():
    assert decide("center this paragraph", "two_centers").decision["reason"] == "AMBIGUOUS_TARGET"
    assert decide("center it", "no_alignment").decision["reason"] == "ALIGNMENT_CONTROL_NOT_AVAILABLE"


@pytest.mark.parametrize("phrase", ["center the page", "center everything", "left align", "justify the document",
                                    "center this sentence", "right align that", "switch back", "align it left"])
def test_unlisted_phrases_are_not_recognised(phrase):
    assert fast_path(build_input(phrase, B.FIXTURES["standard"]())).kind == "NO_MATCH"


@pytest.mark.parametrize("phrase", ["switch back to TextEdit", "center this paragraph", "left align it",
                                    "right-align this paragraph", "justify it"])
def test_router_routes_the_new_forms_to_computer_control(phrase):
    assert is_strong_computer_control_command(phrase)


def test_no_new_operations():
    assert Op.CLICK_ELEMENT in LIVE_OPS and Op.COPY_SELECTION in LIVE_OPS
