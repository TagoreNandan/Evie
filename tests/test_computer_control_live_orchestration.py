"""
LIVE (CC-1a Step 6): one deterministic multi-step orchestration through the real worker.
Run explicitly and alone:
    python -m pytest tests/test_computer_control_live_orchestration.py -m live -s -q

A = the app frontmost at the start (cross-checked; not restricted/sensitive). B = Finder (TextEdit if
Finder is A). Both must already be running - nothing is launched. Plan (4 reversible activations, each
chosen on its own fresh observation of B): activate B -> activate A -> activate B -> activate A.
Goal (checked on a fresh final observation): A is frontmost, i.e. the starting app is restored.
No documents, typing, clicking or scrolling. The worker is always stopped.
"""

import json
import time
import uuid

import pytest

from services.computer_control import macos_probe
from services.computer_control.gate import DEFAULT_POLICY, derive_allowed_apps
from services.computer_control.models import Op
from services.computer_control.orchestrator import ActionIntent, Goal, Orchestrator, Plan, goal_check_for
from services.computer_control.session import ComputerControlSession
from services.computer_control.worker import ComputerControlWorker, WorkerState

pytestmark = pytest.mark.live
FINDER, TEXTEDIT = "com.apple.finder", "com.apple.TextEdit"


class RealClock:
    def now(self):
        return time.monotonic()


def test_live_deterministic_orchestration(monkeypatch):
    monkeypatch.setenv(macos_probe.LIVE_ENV, "1")
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    status = worker.start()
    report = {}
    try:
        assert status.state is WorkerState.READY, (status.state, status.failure)
        pre = worker.observe(bundle_id=FINDER)                # read-only: establish the starting context
        assert pre.status == "OK", pre.reason
        start = pre.observation
        report["initial_observation"] = start.obs_id
        report["initial_frontmost"] = {"app": start.frontmost.model_dump() if start.frontmost else None,
                                       "sources": list(start.frontmost_sources),
                                       "diagnostic": start.frontmost_diagnostic}
        a = start.frontmost
        if a is None:
            pytest.skip(f"PRECONDITION frontmost unresolved ({start.frontmost_diagnostic}); nothing activated")
        if a.bundle_id in DEFAULT_POLICY.restricted_bundles | DEFAULT_POLICY.no_observe_bundles:
            pytest.skip(f"PRECONDITION starting app {a.bundle_id} is restricted; nothing activated")
        running = {x.bundle_id: x for x in start.running_apps}
        b_id = TEXTEDIT if a.bundle_id == FINDER else FINDER
        if b_id not in running:
            pytest.skip(f"PRECONDITION {b_id} is not running; not launching it")
        b = running[b_id]
        utterance = f"switch to {b.name} and then {a.name}, twice"
        allowed = derive_allowed_apps(utterance, a, start.running_apps)
        if not {a.bundle_id, b.bundle_id} <= allowed:
            pytest.skip("PRECONDITION apps not in the utterance-derived allowed scope; nothing activated")

        def act_on(app):
            return ActionIntent(op=Op.ACTIVATE_APP, observe_bundle=b.bundle_id, args={"bundle_id": app.bundle_id})

        plan = Plan(utterance=utterance, intents=(act_on(b), act_on(a), act_on(b), act_on(a)),
                    final_observe_bundle=b.bundle_id, goal=Goal(kind="frontmost_app", bundle_id=a.bundle_id))
        report["plan"] = [f"{i.op.value}({i.args['bundle_id']}) observe={i.observe_bundle}" for i in plan.intents]
        report["goal"] = plan.goal.model_dump()
        report["allowed_apps"] = sorted(allowed)

        session = ComputerControlSession(f"live-{uuid.uuid4().hex[:8]}", utterance, allowed, clock=RealClock(),
                                         goal_check=goal_check_for(plan))
        session.start(status.probe.to_permission_status())
        result = Orchestrator(session, lambda bundle: worker.observe(bundle_id=bundle),
                              lambda req, utt, apps: worker.act(req, utterance=utt, allowed_apps=apps)).run(plan)

        report["events"] = [f"{e.kind}{'' if e.step is None else f'[{e.step}]'} {e.detail}" for e in result.events]
        report["observations"] = list(result.observations)
        report["steps"] = [s.model_dump(mode="json") for s in result.steps]
        report["final_state"] = result.final_state.value
        report["stop_reason"] = result.stop_reason
        # Only an observation taken AFTER the last execution can say what is frontmost now.
        kinds = [e.kind for e in result.events]
        last_exec = max((i for i, k in enumerate(kinds) if k == "EXECUTE"), default=-1)
        later = [e.detail for i, e in enumerate(result.events) if e.kind == "OBSERVE" and i > last_exec]
        final_front = later[-1].split("frontmost=")[-1] if later else None
        report["final_frontmost_fresh_observation"] = final_front
        report["restored_starting_app"] = (final_front == a.bundle_id) if final_front else "NOT_OBSERVED_AFTER_LAST_ACTION"
        print("\nLIVE_ORCHESTRATION_REPORT " + json.dumps(report, indent=1, default=str))
        assert result.final_state.value == "DONE_VERIFIED", result.stop_reason
        assert final_front == a.bundle_id
    finally:
        stopped = worker.stop()
        print(f"WORKER_CLEANUP state={stopped.state.value} exit_code={stopped.exit_code}")
        assert stopped.state is WorkerState.STOPPED
