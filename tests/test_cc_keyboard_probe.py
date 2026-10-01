"""
Step 15B bounded press_key probe (keyboard_probe.py), FAST tests: simulated world, fake keyboard port. No events.
"""

import pytest

from services.cc_live_validation import fixtures as F, keyboard_probe as K
from services.computer_control.actions import is_enabled
from services.computer_control.models import Op
from test_cc_live_validation import FakeLifecycle, FakeWorker, World


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "SCRATCH_DIR", tmp_path)


class FakeKeyboard:
    def __init__(self, deliver=True, front_pid=4000, text="EVIE_FIXTURE_MARKER", change_text=False):
        self.caret, self.deliver, self.front_pid, self.text = 0, deliver, front_pid, text
        self.change_text, self.posted = change_text, []

    def text_state(self, pid):
        return "AXTextArea", "First Text View", (self.caret, 0), self.text

    def frontmost(self, pid):
        return {"nsworkspace": self.front_pid, "app_ax_frontmost": self.front_pid == pid,
                "is_active": self.front_pid == pid}

    def post_key(self, pid, key):
        code = K.KEYCODES[key]
        self.posted += [(pid, code, True), (pid, code, False)]
        if self.deliver:
            self.caret += K.EXPECTED_DELTA[key]
        if self.change_text:
            self.text += "x"
        return 2


def run(kb=None, world=None):
    world = world or World()
    kb = kb or FakeKeyboard()
    lc = FakeLifecycle(world)
    return K.KeyboardProbe(FakeWorker(world), lc, kb, sleep=lambda s: None).run(), kb, lc


def test_right_then_left_verified_and_restored():
    report, kb, lc = run()
    assert report["status"] == "PRESS_KEY_READY_FOR_REVIEW", (report["stopped_at"], report["stop_reason"])
    assert [(k["key"], k["range_before"], k["range_after"], k["verified"]) for k in report["keys"]] == [
        ("RIGHT_ARROW", [0, 0], [1, 0], True), ("LEFT_ARROW", [1, 0], [0, 0], True)]
    assert kb.posted == [(4000, 124, True), (4000, 124, False), (4000, 123, True), (4000, 123, False)]
    assert report["keyboard_events_posted"] == 4 and report["production_mutations"] == 1
    assert [m["op"] for m in report["mutations"]] == ["activate_app"]
    wheres = [c["where"] for c in report["identity_checks"]]
    assert "RIGHT_ARROW:before_key" in wheres and "LEFT_ARROW:before_key" in wheres
    assert all(c["ok"] for c in report["identity_checks"])
    assert all(k["prechecks"]["gate_press_key"] == "ALLOW:LOW_ALLOWED" for k in report["keys"])   # enabled (15C)
    assert lc.actions[-1].startswith("SIGTERM") and report["textedit_running_at_end"] == []


def test_undelivered_key_stops_without_retry_or_restore():
    report, kb, lc = run(FakeKeyboard(deliver=False))
    assert report["stopped_at"] == "KEY_NOT_VERIFIED" and report["status"] == "PRESS_KEY_BLOCKED"
    assert len(kb.posted) == 2 and report["keyboard_events_posted"] == 2      # no retry, no LEFT
    assert not any(a.startswith("SIGTERM") for a in lc.actions)


def test_wrong_frontmost_stops_before_any_key():
    report, kb, _ = run(FakeKeyboard(front_pid=2494))
    assert report["stopped_at"] == "PRECHECK" and kb.posted == []


def test_changed_text_is_not_verified():
    report, kb, _ = run(FakeKeyboard(change_text=True))
    assert report["stopped_at"] == "KEY_NOT_VERIFIED" and len(kb.posted) == 2


def test_textedit_running_with_something_else_stops_before_anything():
    world = World()
    world.fixture = F.SCROLL
    report = K.KeyboardProbe(FakeWorker(world), FakeLifecycle(world, running_at_start=True),
                             FakeKeyboard(), sleep=lambda s: None).run()
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and report["keyboard_events_posted"] == 0


def test_leftover_text_fixture_is_retired_then_probe_passes():
    F.TEXT.path.parent.mkdir(parents=True, exist_ok=True)
    F.TEXT.path.write_text(F.TEXT_MARKER)
    world = World()
    world.fixture = F.TEXT
    lc = FakeLifecycle(world, running_at_start=True)
    report = K.KeyboardProbe(FakeWorker(world), lc, FakeKeyboard(), sleep=lambda s: None).run()
    assert report["status"] == "PRESS_KEY_READY_FOR_REVIEW" and lc.actions[0] == "SIGTERM TextEdit pid 4000"


@pytest.mark.parametrize("raw", [(3, 0), type("R", (), {"location": 3, "length": 0})()])
def test_cfrange_from_pyobjc_tuple_or_struct(raw):
    assert K._range(raw) == (3, 0)             # the first live run stopped on the tuple form (no key posted)


def test_only_two_keys_no_modifiers():
    assert K.KEYCODES == {"RIGHT_ARROW": 124, "LEFT_ARROW": 123}
    with pytest.raises(KeyError):
        K.Keyboard.post_key(object.__new__(K.Keyboard), 1, "RETURN")
    assert is_enabled(Op.PRESS_KEY) is True                      # Step 15C enabled the same two keys in production
