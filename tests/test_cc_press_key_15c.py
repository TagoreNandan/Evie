"""Step 15C production-path live harness (press_key_15c.py), FAST tests on the simulated world. No events."""

import pytest

from services.cc_live_validation import fixtures as F, press_key_15c as P
from services.computer_control.models import ActionResult, ExecutionStatus as E, Op, VerificationStatus as V
from test_cc_live_validation import FakeLifecycle, FakeWorker, World


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "SCRATCH_DIR", tmp_path)


class KeyWorker(FakeWorker):
    def __init__(self, world, left_restores=True):
        super().__init__(world)
        self.caret, self.left_restores = 0, left_restores

    def act(self, request, *, utterance, allowed_apps):
        if request.op is not Op.PRESS_KEY:
            return super().act(request, utterance=utterance, allowed_apps=allowed_apps)
        self.acts.append(f"press_key:{request.args.key}")
        before = self.caret
        self.caret += 1 if request.args.key == "RIGHT_ARROW" else (-1 if self.left_restores else 0)
        base = dict(session_id=request.session_id, step=request.step, op=request.op, requested_at=1.0)
        ev = {"range_before": f"{before},0", "range_after": f"{self.caret},0", "text_unchanged": True}
        return ActionResult.executed(execution=E.EXECUTED, verification=V.VERIFIED, fingerprint_changed=True,
                                     evidence=ev, **base)


def run(worker=None, world=None):
    world = world or World()
    worker = worker or KeyWorker(world)
    lc = FakeLifecycle(world)
    return P.PressKey15C(worker, lc, read_text=lambda pid: (F.TEXT_MARKER, (0, 0)),
                         sleep=lambda s: None).run(), worker, lc


def test_production_path_pass():
    report, worker, lc = run()
    assert report["status"] == "PRESS_KEY_PRODUCTION_PASS", (report["stopped_at"], report["stop_reason"])
    assert worker.acts == ["activate_app", "press_key:RIGHT_ARROW", "press_key:LEFT_ARROW"]
    assert report["caret"] == {"start": "0,0", "after_right": "1,0", "after_left": "0,0"}
    assert report["final_text_exact"] and report["production_mutations"] == 3 and lc.actions[-1].startswith("SIGTERM")


def test_caret_not_restored_is_not_hidden():
    world = World()
    report, _, lc = run(KeyWorker(world, left_restores=False), world)
    assert report["stopped_at"] == "RESTORATION_NOT_VERIFIED" and not lc.actions[-1].startswith("SIGTERM")


def test_sentinel_admits_only_the_two_keys_and_textedit_activation():
    world = World()
    h = P.PressKey15C(KeyWorker(world), FakeLifecycle(world))
    mk = lambda op, **a: type("R", (), {"op": op, "args": type("A", (), a)(), "target": None})()
    assert h._approved(mk(Op.PRESS_KEY, key="RIGHT_ARROW")) and h._approved(mk(Op.PRESS_KEY, key="LEFT_ARROW"))
    assert not h._approved(mk(Op.PRESS_KEY, key="RETURN"))
    assert not h._approved(mk(Op.ACTIVATE_APP, bundle_id="com.apple.finder"))
    assert not h._approved(mk(Op.SET_TEXT))
