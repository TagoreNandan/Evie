"""
Computer-control worker lifecycle (docs/07 Section 4): start -> READY / NOT_READY / FAILED -> stop.
Scripted fake handles cover failure paths; one real worker process is started with AX blocked.
"""

import io
import json

import pytest

from services.computer_control import worker as W
from services.computer_control.runtime import (
    AccessibilityProbe, AccessibilityStatus, FunctionalProbe, FunctionalStatus, ProcessInfo, RuntimeIdentity,
    RuntimeProbeResult,
)

PID = 5150


def _probe(ready=True, pid=PID):
    ident = RuntimeIdentity(worker=ProcessInfo(pid=pid, executable="/x/python"),
                            responsible=ProcessInfo(pid=1, name="host"))
    return RuntimeProbeResult(
        probed_at=1.0, worker_started=True, identity=ident,
        accessibility=AccessibilityProbe(status=AccessibilityStatus.TRUSTED if ready else AccessibilityStatus.NOT_TRUSTED),
        functional=FunctionalProbe(status=FunctionalStatus.OK, app_role="AXApplication"))


def _msg(model):
    return json.loads(model.model_dump_json())


class FakeHandle:
    """Scripted worker: `script` is the sequence of receive() outcomes (dict, None=timeout, or an exception)."""

    def __init__(self, script, pid=PID, exit_code=0, dies=False):
        self.pid, self.script, self.sent = pid, list(script), []
        self.exit_code, self.dies, self.terminated, self.killed = exit_code, dies, False, False
        self._alive = True

    def send(self, message):
        if not self._alive:
            raise BrokenPipeError()
        self.sent.append(message)
        if message["type"] == "stop" and not self.dies:
            self._alive = False

    def receive(self, timeout_s):
        if not self.script:
            return None
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def poll(self):
        return None if self._alive else self.exit_code

    def wait(self, timeout_s):
        return None if self._alive else self.exit_code

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.exit_code = -15

    def kill(self):
        self.killed = True
        self._alive = False


def _worker(handle):
    return W.ComputerControlWorker(launcher=lambda: handle, start_timeout_s=0.1, probe_timeout_s=0.1, stop_timeout_s=0.1)


HELLO = {"type": "hello", "protocol": 1, "pid": PID}


def test_start_ready_then_clean_stop():
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe())), {"type": "stopped"}])
    w = _worker(h)
    s = w.start()
    assert s.state is W.WorkerState.READY and s.ready and s.pid == PID
    s = w.stop()
    assert s.state is W.WorkerState.STOPPED and s.exit_code == 0 and s.failure is None
    assert h.sent == [{"type": "stop"}] and not h.terminated


def test_started_but_not_ready_fails_closed():
    s = _worker(FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe(ready=False)))])).start()
    assert s.state is W.WorkerState.NOT_READY and not s.ready and s.probe is not None


def test_reprobe():
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe(ready=False))),
                    _msg(W.ProbeResultMsg(result=_probe(ready=True)))])
    w = _worker(h)
    w.start()
    assert w.probe().ready and h.sent == [{"type": "probe"}]


@pytest.mark.parametrize("script, reason", [
    ([None], "START_TIMEOUT"),
    ([W.WorkerExited("gone")], "WORKER_EXITED"),
    ([{"type": "hello", "protocol": 2, "pid": PID}], "PROTOCOL_ERROR"),
    ([{"type": "hello", "protocol": 1, "pid": PID + 1}], "PROTOCOL_ERROR"),
    ([{"type": "execute", "op": "press"}], "PROTOCOL_ERROR"),
    ([HELLO, None], "PROBE_TIMEOUT"),
    ([HELLO, {"type": "error", "error": "ImportError"}], "WORKER_ERROR:ImportError"),
    ([HELLO, _msg(W.ProbeResultMsg(result=_probe(pid=999)))], "PROBE_IDENTITY_MISMATCH"),
    ([HELLO, {"type": "probe_result", "result": {"ready": True}}], "PROTOCOL_ERROR"),
])
def test_startup_failures_are_deterministic_and_reap_the_process(script, reason):
    h = FakeHandle(script)
    s = _worker(h).start()
    assert s.state is W.WorkerState.FAILED and (s.failure == reason or s.failure.startswith(reason + ":")) and not s.ready
    assert h.terminated


def test_launch_failure():
    def boom():
        raise FileNotFoundError("python")
    s = W.ComputerControlWorker(launcher=boom).start()
    assert s.state is W.WorkerState.FAILED and s.failure == "LAUNCH_FAILED:FileNotFoundError"


def test_unresponsive_worker_is_terminated_on_stop():
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe())), None], dies=True)
    w = _worker(h)
    w.start()
    s = w.stop()
    assert s.state is W.WorkerState.STOPPED and s.failure == "UNCLEAN_SHUTDOWN" and h.terminated


def test_lifecycle_guards():
    w = _worker(FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe()))]))
    with pytest.raises(RuntimeError):
        w.probe()                                   # not started
    w.start()
    with pytest.raises(RuntimeError):
        w.start()                                   # single use
    assert w.stop().state is W.WorkerState.STOPPED
    assert W.ComputerControlWorker().stop().state is W.WorkerState.STOPPED   # stopping a never-started worker


def test_protocol_has_no_action_command():
    for bad in ('{"type": "press"}', '{"type": "execute"}', '{"type": "probe", "op": "set_text"}'):
        out = io.StringIO()
        W.serve(io.StringIO(bad + "\n"), out, probe=_probe)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        assert lines[-1] == {"type": "error", "error": "UNKNOWN_COMMAND"}


def test_serve_lifecycle_messages():
    out = io.StringIO()
    code = W.serve(io.StringIO('{"type": "probe"}\n{"type": "stop"}\n'), out, probe=_probe)
    kinds = [json.loads(l)["type"] for l in out.getvalue().splitlines()]
    assert code == 0 and kinds == ["hello", "probe_result", "probe_result", "stopped"]


def test_real_worker_process_starts_and_stops_with_ax_blocked():
    """A real `python -m services.computer_control.worker` child; AX stays blocked under pytest."""
    w = W.ComputerControlWorker(start_timeout_s=30, probe_timeout_s=30)
    s = w.start()
    try:
        assert s.state is W.WorkerState.NOT_READY and not s.ready
        assert s.probe.identity.worker.pid == s.pid and "AX_BLOCKED_UNDER_TEST" in s.probe.diagnostics
    finally:
        s = w.stop()
    assert s.state is W.WorkerState.STOPPED and s.exit_code == 0 and s.failure is None


# --- CC-1a Step 3: read-only observe command ---

from cc_fixtures import observation as _observation  # noqa: E402
from services.computer_control.observer import ObserveResult  # noqa: E402

OBS_OK = ObserveResult(status="OK", observation=_observation(), duration_ms=5.0)


def test_observe_command_needs_exactly_one_scope():
    assert W.ObserveCommand(bundle_id="com.apple.TextEdit").settle is True
    for bad in ({}, {"bundle_id": "com.apple.TextEdit", "pid": 3}, {"pid": 3, "frontmost": True},
                {"bundle_id": "bad bundle; rm -rf ~"}):
        with pytest.raises(Exception):
            W.ObserveCommand(**bad)


def test_observe_through_a_ready_worker():
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe())), _msg(W.ObservationMsg(result=OBS_OK))])
    w = _worker(h)
    w.start()
    r = w.observe(bundle_id="com.apple.TextEdit")
    assert r.status == "OK" and r.observation.obs_id == "obs-1"
    assert h.sent == [{"type": "observe", "bundle_id": "com.apple.TextEdit", "pid": None, "frontmost": False,
                       "settle": True}]


def test_observe_is_refused_unless_ready():
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe(ready=False)))])
    w = _worker(h)
    w.start()
    r = w.observe(bundle_id="com.apple.TextEdit")
    assert r.status == "NOT_READY" and h.sent == []          # nothing reaches the worker
    assert W.ComputerControlWorker().observe(pid=1).status == "NOT_READY"


def test_observe_timeout_fails_the_worker():
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe())), None])
    w = _worker(h)
    w.start()
    assert w.observe(pid=77).reason == "OBSERVE_TIMEOUT" and w.status.state is W.WorkerState.FAILED


def test_serve_routes_observe_only_when_its_own_probe_is_ready():
    calls = []

    def fake_observe(command):
        calls.append(command)
        return OBS_OK

    out = io.StringIO()
    W.serve(io.StringIO('{"type": "observe", "pid": 77}\n'), out, probe=lambda: _probe(ready=False),
            observe=fake_observe)
    last = json.loads(out.getvalue().splitlines()[-1])
    assert last["result"]["status"] == "NOT_READY" and calls == []
    out = io.StringIO()
    W.serve(io.StringIO('{"type": "observe", "pid": 77}\n'), out, probe=_probe, observe=fake_observe)
    last = json.loads(out.getvalue().splitlines()[-1])
    assert last["type"] == "observation" and last["result"]["status"] == "OK" and calls[0].pid == 77


# --- CC-1a Step 4: closed act command ---

from services.computer_control.models import (  # noqa: E402
    ActionRequest, ActionResult, ActionStatus, ExecutionStatus, GateDecision, GateOutcome, NoArgs, Op,
    ResolutionMethod, ResolvedTarget, RiskLevel, VerificationStatus,
)


def _act_request():
    obs = _observation()
    t = obs.targets[3]
    return ActionRequest(session_id="s", step=1, op=Op.PRESS, args=NoArgs(), obs_id=obs.obs_id, cancel_epoch=0,
                         target=ResolvedTarget(target=t, method=ResolutionMethod.ORDINAL, obs_id=obs.obs_id),
                         gate=GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.LOW, reason="LOW_ALLOWED"),
                         timeout_s=10)


def _act_result(req, status=ActionStatus.BLOCKED):
    return ActionResult(session_id=req.session_id, step=req.step, op=req.op, status=status,
                        execution=ExecutionStatus.NOT_EXECUTED, verification=VerificationStatus.NOT_APPLICABLE,
                        requested_at=1.0, reason="TEST")


def test_act_through_a_ready_worker():
    req = _act_request()
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe())), _msg(W.ActionResultMsg(result=_act_result(req)))])
    w = _worker(h)
    w.start()
    r = w.act(req, utterance="press align center", allowed_apps={"com.apple.TextEdit"})
    assert r.reason == "TEST" and h.sent[0]["type"] == "act" and h.sent[0]["allowed_apps"] == ["com.apple.TextEdit"]


def test_act_is_refused_unless_ready_and_nothing_is_sent():
    req = _act_request()
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe(ready=False)))])
    w = _worker(h)
    w.start()
    r = w.act(req, utterance="press align center", allowed_apps={"com.apple.TextEdit"})
    assert r.status is ActionStatus.BLOCKED and r.execution is ExecutionStatus.NOT_EXECUTED and h.sent == []


def test_act_timeout_is_an_unknown_outcome_not_a_result():
    req = _act_request()
    h = FakeHandle([HELLO, _msg(W.ProbeResultMsg(result=_probe())), None])
    w = _worker(h)
    w.start()
    with pytest.raises(TimeoutError):
        w.act(req, utterance="press align center", allowed_apps={"com.apple.TextEdit"})
    assert w.status.state is W.WorkerState.FAILED


def test_act_command_is_closed():
    req = _act_request()
    base = {"request": json.loads(req.model_dump_json()), "utterance": "x", "allowed_apps": ["com.apple.TextEdit"]}
    W.ActCommand(**base)
    for bad in ({**base, "ax_call": "AXUIElementPerformAction"}, {**base, "allowed_apps": []},
                {**base, "request": {**base["request"], "gate": {"outcome": "BLOCK", "risk": "HIGH", "reason": "X"}}}):
        with pytest.raises(Exception):
            W.ActCommand(**bad)


def test_serve_routes_act_only_when_its_own_probe_is_ready():
    req = _act_request()
    line = W.ActCommand(request=req, utterance="press align center", allowed_apps=["com.apple.TextEdit"]) \
        .model_dump_json() + "\n"
    calls = []
    out = io.StringIO()
    W.serve(io.StringIO(line), out, probe=lambda: _probe(ready=False), act=lambda c: calls.append(c))
    assert json.loads(out.getvalue().splitlines()[-1])["result"]["reason"] == "RUNTIME_PROBE_NOT_READY" and not calls
    out = io.StringIO()
    W.serve(io.StringIO(line), out, probe=_probe, act=lambda c: _act_result(c.request, ActionStatus.STALE))
    last = json.loads(out.getvalue().splitlines()[-1])
    assert last["type"] == "action_result" and last["result"]["status"] == "STALE"
