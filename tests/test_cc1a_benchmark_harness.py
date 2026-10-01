"""CC-1b.1 benchmark HARNESS fixes (test infrastructure only): verified termination, bounded fixture-open retries,
observable documents, and a read-only frontmost precondition that never re-activates. Fakes only."""

import pytest

from cc_fixtures import observation, window
from services.cc_live_validation import cc1a_benchmark as B, fixtures as F
from services.computer_control.models import AppIdentity
from services.computer_control.observer import ObserveResult
from services.computer_control_router import ComputerControlRuntime

TE = AppIdentity(bundle_id=B.TEXTEDIT, pid=4000, name="TextEdit")
CHROME = AppIdentity(bundle_id=B.CHROME, pid=2400, name="Google Chrome")


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "SCRATCH_DIR", tmp_path)


class LC:
    def __init__(self, open_codes=(0,), running=False):
        self.codes, self.running, self.opened, self.actions = list(open_codes), running, [], []

    def textedit_pids(self):
        return [4000] if self.running else []

    def terminate_textedit(self, timeout_s=15.0):
        self.actions.append("SIGTERM")
        self.running = False
        self.opened = []
        return True, [4000]

    def write(self, doc):
        return "h"

    def open_in_background(self, doc):
        self.actions.append(f"open {doc.filename}")
        code = self.codes.pop(0) if self.codes else 0
        if code == 0:
            self.running = True
            self.opened.append(doc)
        return code

    def open_in_foreground(self, doc):
        self.actions.append(f"open {doc.filename}")
        code = self.codes.pop(0) if self.codes else 0
        if code == 0:
            self.running = True
            self.opened.append(doc)
        return code

    def open_chrome_window(self):
        self.actions.append("open_chrome")
        return 0


class Worker:
    def __init__(self, lc, front=TE, visible=True):
        self.lc, self.front, self.visible = lc, front, visible

    def observe(self, *, bundle_id=None, **kw):
        if bundle_id == B.CHROME:
            obs = observation((), obs_id="o", frontmost=self.front, app=CHROME, win=window(doc="chrome"), windows=(window(doc="chrome"),))
            return ObserveResult(status="OK", observation=obs)
        wins = tuple(window(doc=d.document_hash, main=i == 0) for i, d in enumerate(self.lc.opened)) \
            if self.visible else ()
        obs = observation((), obs_id="o", frontmost=self.front, app=TE, win=wins[0] if wins else None, windows=wins)
        return ObserveResult(status="OK", observation=obs)


def bench(lc, worker):
    return B.Benchmark(worker, lc, ":memory:", lambda text: {}, ComputerControlRuntime, None, sleep=lambda s: None)


def test_termination_is_verified_and_a_failed_open_is_retried_as_fixture_setup_only():
    lc = LC(open_codes=(1, 0), running=True)
    b = bench(lc, Worker(lc))
    info = b.fixtures(F.TEXT)
    assert lc.actions == ["SIGTERM", "open evie_step14b_text.txt", "open evie_step14b_text.txt"]
    assert b.fixture_open_retries == ["evie_step14b_text.txt#1"] and info["windows_observed"] == 1


def test_persistent_open_failure_is_fixture_not_ready():
    lc = LC(open_codes=(1, 1, 1))
    with pytest.raises(RuntimeError, match="FIXTURE_NOT_READY"):
        bench(lc, Worker(lc)).fixtures(F.TEXT)


def test_documents_must_be_observable():
    lc = LC()
    with pytest.raises(RuntimeError, match="not observable"):
        bench(lc, Worker(lc, visible=False)).fixtures(F.TEXT)


def test_both_documents_required_for_two_document_tasks():
    lc = LC()
    info = bench(lc, Worker(lc)).fixtures(F.ALIGNMENT, F.TEXT)
    assert info["windows_observed"] == 2


def test_wrong_frontmost_is_a_recorded_precondition_failure_not_a_reactivation():
    lc = LC()
    routed = []
    b = B.Benchmark(Worker(lc, front=CHROME), lc, ":memory:", lambda text: routed.append(text) or {},
                    ComputerControlRuntime, None, sleep=lambda s: None)
    r = {}
    b.fixtures(F.SCROLL)
    with pytest.raises(B.PreconditionFailed, match="expected com.apple.TextEdit, found com.google.Chrome"):
        b.require_front(B.TEXTEDIT)
    assert routed == []                                 # nothing was routed to "fix" the frontmost app


def test_two_paragraph_plan_uses_existing_ops_and_exact_spans():
    from services.cc_live_validation import two_paragraph_1b1 as T
    from services.computer_control.models import Op
    plan = T.PLANS["text"]
    assert [i.op for i in plan.intents] == [Op.ACTIVATE_APP, Op.SET_TEXT, Op.SET_TEXT]
    insert, restore = plan.intents[1].args, plan.intents[2].args
    assert plan.utterance[slice(*insert["text_span"])] == T.TWO_PARAGRAPHS and "\n\n" in T.TWO_PARAGRAPHS
    assert insert["mode"] == "insert_at_cursor" and restore["mode"] == "replace_all"
    assert plan.utterance[slice(*restore["text_span"])] == "EVIE_FIXTURE_MARKER"
