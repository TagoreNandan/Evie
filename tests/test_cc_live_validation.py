"""
CC-1a Step 14B harness (services/cc_live_validation), FAST tests against a simulated TextEdit/Finder world.
No macOS, no processes, no network. The real Orchestrator/session/gate run; only the worker and the fixture
lifecycle are fakes.
"""

import ast
import json
from pathlib import Path

import pytest

from cc_fixtures import ALIGN_CENTER, ALIGN_LEFT, SCROLL_AREA, TEXT_AREA, observation, window
from services.cc_live_validation import fixtures as F, step14b as S
from services.computer_control.models import (
    ActionResult, AppIdentity, ExecutionStatus as E, Op, VerificationStatus as V,
)
from services.computer_control.observer import ObserveResult
from services.computer_control.runtime import AccessibilityStatus as A
from services.computer_control.worker import WorkerState, WorkerStatus
from test_computer_control_runtime_identity import _real_contract_identity, result

TEXTEDIT = AppIdentity(bundle_id="com.apple.TextEdit", pid=4000, name="TextEdit")
FINDER = AppIdentity(bundle_id="com.apple.finder", pid=629, name="Finder")
IDE = AppIdentity(bundle_id="com.google.antigravity-ide", pid=2494, name="Antigravity IDE")


class World:
    """Simulated TextEdit + Finder. Tracks the one open fixture, alignment and scroll state, and the front app."""

    def __init__(self):
        self.textedit_running, self.fixture, self.front = False, None, IDE
        self.align, self.scroll, self.extra_windows = "left", 0.0, 0
        self.n = 0


class FakeLifecycle(F.FixtureLifecycle):
    def __init__(self, world, running_at_start=False):
        super().__init__(run=None, kill=None, exe_of=None, sleep=lambda s: None)
        self.world = world
        world.textedit_running = running_at_start

    def textedit_pids(self):
        return [TEXTEDIT.pid] if self.world.textedit_running else []

    def write(self, fixture):
        return f"hash-{fixture.name}"

    def disk_hash(self, fixture):
        return f"hash-{fixture.name}"

    def open_in_background(self, fixture):
        self.actions.append(f"open -g -F -a TextEdit {fixture.filename}")
        self.world.textedit_running, self.world.fixture = True, fixture
        self.world.align, self.world.scroll = "left", 0.0
        return 0

    def terminate_textedit(self, timeout_s=15.0):
        self.actions.append(f"SIGTERM TextEdit pid {TEXTEDIT.pid}")
        self.world.textedit_running, self.world.fixture = False, None
        return True, [TEXTEDIT.pid]


class FakeWorker:
    def __init__(self, world, act_outcomes=None, identity_fails_at=None, scroll_restore_off=False,
                 align_initial=None):
        self.world, self.outcomes = world, dict(act_outcomes or {})
        self.identity_fails_at, self.scroll_restore_off = identity_fails_at, scroll_restore_off
        self.probes, self.acts = 0, []
        if align_initial:
            world.align_initial = align_initial
        self._status = WorkerStatus(state=WorkerState.READY, pid=702, probe=result(_real_contract_identity()))

    @property
    def status(self):
        return self._status

    def probe(self):
        self.probes += 1
        ok = self.identity_fails_at is None or self.probes < self.identity_fails_at
        probe = result(_real_contract_identity(), ax=A.TRUSTED if ok else A.NOT_TRUSTED)
        self._status = WorkerStatus(state=WorkerState.READY if ok else WorkerState.NOT_READY, pid=702, probe=probe)
        return self._status

    def observe(self, *, bundle_id=None, **kw):
        w = self.world
        w.n += 1
        running = (IDE, FINDER) + ((TEXTEDIT,) if w.textedit_running else ())
        if bundle_id == TEXTEDIT.bundle_id:
            if not w.textedit_running:
                return ObserveResult(status="UNAVAILABLE", reason="APP_NOT_RUNNING")
            wins = tuple(window(pid=TEXTEDIT.pid, doc=w.fixture.document_hash, main=i == 0, focused=i == 0)
                         for i in range(1 + w.extra_windows))
            targets = [dict(TEXT_AREA, window_index=0), dict(SCROLL_AREA, window_index=0)]
            if w.fixture.name == "alignment":
                initial = getattr(w, "align_initial", None)
                left = "1" if (initial or w.align) == "left" else "0"
                targets += [dict(ALIGN_LEFT, window_index=0, value_summary=left),
                            dict(ALIGN_CENTER, window_index=0, value_summary="1" if left == "0" else "0")]
            obs = observation(targets, obs_id=f"obs-{w.n}", taken_at=float(w.n), app=TEXTEDIT, frontmost=w.front,
                              win=wins[0], windows=wins, running=running, fingerprint=f"fp-{w.n}")
        else:
            obs = observation((), obs_id=f"obs-{w.n}", taken_at=float(w.n), app=FINDER, frontmost=w.front,
                              win=None, running=running, fingerprint=f"fp-{w.n}")
        return ObserveResult(status="OK", observation=obs)

    def act(self, request, *, utterance, allowed_apps):
        w = self.world
        self.acts.append(request.op.value)
        base = dict(session_id=request.session_id, step=request.step, op=request.op, requested_at=1.0)
        outcome = self.outcomes.get(len(self.acts), "SUCCESS")
        if outcome == "FAILED":
            return ActionResult.executed(execution=E.EXECUTED, verification=V.VERIFIED_NO_CHANGE,
                                         fingerprint_changed=False, **base)
        ev = {"gate": f"{request.gate.outcome.value}", "window_main_before": True}
        if request.op is Op.SET_TEXT:
            s, e = request.args.text_span
            ev.update(exact_text=True, chars_after=e - s, mode=request.args.mode)
        elif request.op is Op.SCROLL:
            down = request.args.direction == "down"
            ev.update(scroll_before=w.scroll, scroll_target=0.25 if down else 0.0, visible_before=0 if down else 2668,
                      visible_after=2668 if down else (7 if self.scroll_restore_off else 0))
            w.scroll = 0.25 if down else 0.0
        elif request.op is Op.PRESS:
            w.align = "center" if request.target.target.label == "align center" else "left"
            ev["values_after"] = json.dumps({"align center": int(w.align == "center"),
                                             "align left": int(w.align == "left")})
        elif request.op is Op.ACTIVATE_APP:
            w.front = TEXTEDIT if request.args.bundle_id == TEXTEDIT.bundle_id else FINDER
            ev.update(frontmost_after=w.front.bundle_id, frontmost_after_agreed=True)
        return ActionResult.executed(execution=E.EXECUTED, verification=V.VERIFIED, fingerprint_changed=True,
                                     evidence=ev, **base)

    def stop(self):
        return WorkerStatus(state=WorkerState.STOPPED)


REAL_SCRATCH_DIR = F.SCRATCH_DIR                              # captured before any test redirects it


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "SCRATCH_DIR", tmp_path)          # never touch the real scratch fixtures


def run(worker_kw=None, lifecycle_kw=None, world=None):
    world = world or World()
    worker = FakeWorker(world, **(worker_kw or {}))
    lifecycle = FakeLifecycle(world, **(lifecycle_kw or {}))
    return S.Step14B(worker, lifecycle, sleep=lambda s: None).run(), worker, lifecycle


# --- the full sequence ---

def test_full_sequence_passes_through_the_orchestrator():
    report, worker, lc = run()
    assert report["result"] == "PASS", (report["stopped_at"], report["stop_reason"])
    assert worker.acts == ["activate_app", "set_text", "set_text", "scroll", "scroll", "press", "press",
                           "activate_app", "activate_app", "activate_app",
                           "activate_app", "activate_app", "set_text", "set_text"]
    assert report["computer_control_mutation_count"] == 14 and report["unexpected_computer_control_operations"] == 0
    assert report["fixture_process_lifecycle_actions"] == 8 and report["retries"] == 0
    assert [p["phase"] for p in report["phases"]] == ["text", "scroll", "alignment", "activation", "multistep"]
    assert all(p["passed"] for p in report["phases"])
    # identity: at start, before EVERY execution (14) and right after every activation (6)
    wheres = [c["where"] for c in report["identity_checks"]]
    assert len(wheres) == 21 and all(c["ok"] for c in report["identity_checks"])
    assert sum(w.endswith("after_activation") for w in wheres) == 6
    assert report["textedit_running_at_end"] == [] and report["fixtures_unchanged_on_disk"] is True


def test_every_action_went_through_observe_resolve_gate_freshness_execute_verify():
    report, _, _ = run()
    for phase in report["phases"]:
        kinds = [e.split("[")[0].split(" ")[0] for e in phase["events"]]
        n = len(phase["steps"])
        assert kinds.count("EXECUTE") == n and kinds.count("VERIFY") == n and kinds.count("GATE") == n
        assert kinds.count("FRESHNESS") == n and kinds.count("OBSERVE") == n + 1
        for i, k in enumerate(kinds):
            if k == "EXECUTE":
                assert kinds[i - 2:i] == ["GATE", "FRESHNESS"] and kinds[i + 1] == "VERIFY"


def test_lifecycle_order_terminates_only_after_restoration():
    _, _, lc = run()
    assert lc.actions == [
        "open -g -F -a TextEdit evie_step14b_text.txt", "SIGTERM TextEdit pid 4000",
        "open -g -F -a TextEdit evie_step14b_scroll.txt", "SIGTERM TextEdit pid 4000",
        "open -g -F -a TextEdit evie_step14b_alignment.rtf", "SIGTERM TextEdit pid 4000",
        "open -g -F -a TextEdit evie_step14b_text.txt", "SIGTERM TextEdit pid 4000"]


# --- preconditions ---

def test_textedit_running_with_something_else_stops_before_anything():
    world = World()
    world.fixture = F.SCROLL                                    # not the leftover text fixture
    report, worker, lc = run(lifecycle_kw={"running_at_start": True}, world=world)
    assert (report["result"], report["stopped_at"]) == ("PARTIAL_STOPPED", "FIXTURE_NOT_READY")
    assert worker.acts == [] and lc.actions == [] and report["computer_control_mutation_count"] == 0


def test_leftover_text_fixture_is_retired_by_test_infrastructure_only():
    F.TEXT.path.parent.mkdir(parents=True, exist_ok=True)
    F.TEXT.path.write_text(F.TEXT_MARKER)
    world = World()
    world.fixture = F.TEXT
    report, worker, lc = run(lifecycle_kw={"running_at_start": True}, world=world)
    assert report["result"] == "PASS" and report["stale_fixture"]["document"] == "text fixture"
    assert lc.actions[0] == "SIGTERM TextEdit pid 4000" and worker.acts[0] == "activate_app"


def test_leftover_with_an_extra_window_is_not_touched():
    F.TEXT.path.parent.mkdir(parents=True, exist_ok=True)
    F.TEXT.path.write_text(F.TEXT_MARKER)
    world = World()
    world.fixture, world.extra_windows = F.TEXT, 1
    report, worker, lc = run(lifecycle_kw={"running_at_start": True}, world=world)
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and lc.actions == [] and worker.acts == []


def test_restored_extra_window_stops_with_fixture_not_ready():
    world = World()
    world.extra_windows = 1
    report, worker, lc = run(world=world)
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and worker.acts == []
    assert not any(a.startswith("SIGTERM") for a in lc.actions)          # the unexpected window is not closed


def test_alignment_fixture_not_left_stops_before_pressing():
    report, worker, _ = run(worker_kw={"align_initial": "center"})
    assert report["stopped_at"] == "FIXTURE_NOT_READY" and "not LEFT" in report["stop_reason"]
    assert "press" not in worker.acts


def test_identity_failure_at_start():
    report, worker, _ = run(worker_kw={"identity_fails_at": 1})
    assert report["stopped_at"] == "IDENTITY" and worker.acts == []


# --- stop on failure, never retry, never hide a failure ---

def test_failed_action_stops_everything_without_retry():
    report, worker, lc = run(worker_kw={"act_outcomes": {2: "FAILED"}})
    assert report["result"] == "FAILED" and report["stopped_at"] == "PHASE_TEXT_FAILED"
    assert worker.acts == ["activate_app", "set_text"]                  # no retry, no restore attempt, no next phase
    assert lc.actions == ["open -g -F -a TextEdit evie_step14b_text.txt"]   # TextEdit NOT terminated
    assert report["textedit_running_at_end"] == [TEXTEDIT.pid]


def test_unverified_restoration_is_not_hidden():
    report, worker, lc = run(worker_kw={"scroll_restore_off": True})
    assert report["stopped_at"] == "RESTORATION_NOT_VERIFIED" and report["result"] == "PARTIAL_STOPPED"
    assert worker.acts[-1] == "scroll" and "press" not in worker.acts
    assert lc.actions[-1] == "open -g -F -a TextEdit evie_step14b_scroll.txt"   # not terminated to hide it


def test_identity_lost_right_after_an_activation_stops_the_run():
    report, worker, _ = run(worker_kw={"identity_fails_at": 3})   # 1 start, 2 before activate, 3 after activate
    assert worker.acts == ["activate_app"] and report["stopped_at"] == "PHASE_TEXT_FAILED"
    assert "IDENTITY_NOT_VERIFIED_AFTER_ACTIVATION" in report["stop_reason"]
    assert report["identity_checks"][-1]["where"] == "text:after_activation" and not report["identity_checks"][-1]["ok"]


def test_identity_lost_before_a_mutation_cancels_it():
    report, worker, _ = run(worker_kw={"identity_fails_at": 4})   # 4 = before the first set_text
    assert worker.acts == ["activate_app"] and "IDENTITY_NOT_VERIFIED" in report["stop_reason"]
    assert report["identity_checks"][-1]["where"] == "text:before_execute"


# --- sentinels and scope ---

class _Req:
    def __init__(self, op, label=None, bundle=None):
        self.op = op
        self.target = type("T", (), {"target": type("O", (), {"label": label})()})() if label else None
        self.args = type("A", (), {"bundle_id": bundle})()


@pytest.mark.parametrize("req", [_Req(Op.FOCUS), _Req(Op.PRESS, label="OK"), _Req(Op.PRESS, label="align right"),
                                 _Req(Op.ACTIVATE_APP, bundle="com.apple.Terminal"), _Req(Op.SELECT_TEXT)])
def test_unapproved_operations_never_reach_the_worker(req):
    world = World()
    worker = FakeWorker(world)
    h = S.Step14B(worker, FakeLifecycle(world))
    with pytest.raises(RuntimeError):
        h.act(req, "u", frozenset())
    assert h.unexpected == 1 and worker.acts == []


def test_plans_use_only_approved_operations_and_at_most_four_actions():
    for name, plan in S.PLANS.items():
        assert len(plan.intents) <= 4
        for intent in plan.intents:
            assert intent.op in S.APPROVED_OPS
            if intent.op is Op.PRESS:
                assert intent.target.label in S.APPROVED_PRESS_LABELS
            if intent.op is Op.ACTIVATE_APP:
                assert intent.args["bundle_id"] in S.APPROVED_ACTIVATION
            if intent.op is Op.SCROLL:
                assert intent.args["amount"] == 0.25
    texts = [S.PLANS["text"].utterance[slice(*i.args["text_span"])] for i in S.PLANS["text"].intents[1:]]
    assert texts == ["EVIE_KEYBOARD_TEST", "EVIE_FIXTURE_MARKER"]


def test_validation_package_cannot_reach_ax_mutation_directly():
    pkg = Path(S.__file__).parent
    for path in pkg.glob("*.py"):
        tree = ast.parse(path.read_text())
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.endswith(("executor", "macos_actions", "macos_ax")) for m in mods), (path.name, mods)
        src = path.read_text()
        for bad in ("AXUIElementSetAttributeValue", "AXUIElementPerformAction", "osascript", "NSAppleScript",
                    "shell=True", "PostKeyboardEvent", "CGRequestPostEventAccess", "CGRequestListenEventAccess"):
            assert bad not in src, (path.name, bad)
        calls = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        assert not {"CGEventPost", "CGEventTapCreate", "CGEventSetFlags", "CGEventKeyboardSetUnicodeString",
                    "CGEventCreateMouseEvent", "CGEventCreateScrollWheelEvent"} & set(calls), path.name
        if path.name == "keyboard_probe.py":                      # the ONE pinned Step 15B posting site
            assert calls.count("CGEventPostToPid") == 1 and calls.count("CGEventCreateKeyboardEvent") == 1
        else:
            assert "CGEventPostToPid" not in calls, path.name
            if path.name != "keyboard_preflight.py":              # only the read-only preflight builds an event
                assert "CGEvent" not in src, path.name
        # activation only in the activation-boundary diagnostic, exactly once, on its fixed scratch target
        if path.name == "activation_boundary.py":
            assert src.count("activateWithOptions_(") == 1
        else:
            assert "activateWithOptions" not in src, path.name


def test_fixture_lifecycle_runs_only_pgrep_open_and_sigterm():
    src = Path(F.__file__).read_text()
    tree = ast.parse(src)
    lists = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.List) and n.elts
             and isinstance(n.elts[0], ast.Constant) and str(n.elts[0].value).startswith("/usr/bin/")]
    assert sorted(lists) == sorted(["['/usr/bin/pgrep', '-x', 'TextEdit']",
                                    "['/usr/bin/open', '-g', '-F', '-a', 'TextEdit', str(fixture.path)]",
                                    "['/usr/bin/open', '-a', 'TextEdit', str(fixture.path)]",
                                    "['/usr/bin/open', '-g', '-a', 'Google Chrome', 'about:blank']"])
    assert "signal.SIGTERM" in src and "SIGKILL" not in src


def test_termination_refuses_a_process_that_is_not_the_system_textedit():
    killed = []

    class Out:
        stdout = "4000\n"
    lc = F.FixtureLifecycle(run=lambda *a, **k: Out(), kill=lambda pid, sig: killed.append(pid),
                            exe_of=lambda pid: "/Users/x/Evil/TextEdit", sleep=lambda s: None)
    ok, pids = lc.terminate_textedit(timeout_s=0.1)
    assert ok is False and killed == [] and lc.actions == []


def test_computer_control_package_never_imports_the_validation_package():
    pkg = Path(S.__file__).parents[1] / "computer_control"
    for path in pkg.glob("*.py"):
        assert "cc_live_validation" not in path.read_text(), path.name


def test_scratch_directory_is_owned_and_isolated():
    assert REAL_SCRATCH_DIR.parts[-3:] == ("Caches", "com.evie-assistant.evie", "step14b-fixtures")
    assert {f.path.parent for f in F.ALL} == {F.SCRATCH_DIR}
    assert F.TEXT.content == "EVIE_FIXTURE_MARKER" and F.ALIGNMENT.content.count("\\ql") == 1
