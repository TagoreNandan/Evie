"""
CC-1a Step 5: activate_app for ALREADY-RUNNING allowed apps (executor path), with a fake platform.
The activation call's return value is never proof; only independent frontmost evidence is.
"""

import pytest
from pydantic import ValidationError

from cc_fixtures import CHROME, KEYCHAIN, SAFARI, TERMINAL, TEXTEDIT, observation
from services.computer_control.executor import ObservationCache, execute_action
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.gate import GatePolicy
from services.computer_control.models import (
    ActionRequest, ActionStatus as A, ActivateAppArgs, AppIdentity, ExecutionStatus as E, GateDecision, GateOutcome,
    Op, PermissionStatus, RiskLevel, VerificationStatus as V,
)
from services.computer_control.observer import ObservationHandles
from services.computer_control.session import ComputerControlSession, SessionState

RUNNING = (TEXTEDIT, CHROME, SAFARI, TERMINAL, KEYCHAIN)
SCOPE = frozenset({SAFARI.bundle_id, TEXTEDIT.bundle_id, TERMINAL.bundle_id, KEYCHAIN.bundle_id})
ALLOW = GateDecision(outcome=GateOutcome.ALLOW, risk=RiskLevel.LOW, reason="LOW_ALLOWED")


class Clock:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakePlatform:
    """Frontmost state + process table. Knobs simulate every failure mode the verifier must catch."""

    def __init__(self, front=SAFARI, returns=True, effect=True, disagree=False, self_report=None, exits=False,
                 live_pids=None):
        self.front, self.returns, self.effect, self.disagree = front, returns, effect, disagree
        self.self_report, self.exits = self_report, exits
        self.pids = dict(live_pids) if live_pids is not None else {a.bundle_id: a.pid for a in RUNNING}
        self.activations = []

    def check(self):
        readings = [FrontmostReading(source="nsworkspace", app=self.front),
                    FrontmostReading(source="app_ax_frontmost", app=self.front)]
        if self.disagree:
            readings.append(FrontmostReading(source="ax_focused_application", app=CHROME))
        return cross_check(readings)

    # ActionBackend (activation part)
    def running_pid(self, bundle_id):
        return self.pids.get(bundle_id)

    def is_running(self, pid):
        return pid in self.pids.values()

    def is_active(self, pid):
        return self.front.pid == pid

    def app_reports_frontmost(self, pid):
        return self.self_report if self.self_report is not None else self.front.pid == pid

    def activate_running_app(self, pid):
        self.activations.append(pid)
        if self.effect:
            self.front = next(a for a in RUNNING if a.pid == pid)
        if self.exits:
            self.pids = {b: p for b, p in self.pids.items() if p != pid}
        return self.returns


def _cache(clock, obs_id="obs-1"):
    obs = observation(obs_id=obs_id, app=TEXTEDIT, frontmost=SAFARI, running=RUNNING)
    return ObservationCache(obs, ObservationHandles(), clock())


def _request(bundle, obs_id="obs-1", step=1):
    return ActionRequest(session_id="s", step=step, op=Op.ACTIVATE_APP, args=ActivateAppArgs(bundle_id=bundle),
                         obs_id=obs_id, gate=ALLOW, cancel_epoch=0, timeout_s=10)


def _run(platform, request, cache=None, clock=None, scope=SCOPE, policy=None, frontmost=None):
    clock = clock or Clock()
    cache = cache if cache is not None else _cache(clock)
    counter = iter(range(2, 100))

    def reobserve(pid):
        app = next(a for a in RUNNING if a.pid == pid)
        obs = observation(obs_id=f"obs-{next(counter)}", app=app, frontmost=platform.front, running=RUNNING)
        return obs, ObservationHandles()

    return execute_action(request, utterance="switch to TextEdit then Safari", allowed_apps=scope, cache=cache,
                          backend=platform, frontmost=frontmost or platform.check(), reobserve=reobserve,
                          recheck_frontmost=platform.check, policy=policy or GatePolicy(), clock=clock,
                          sleep=clock.sleep, now=lambda: 1_790_000_000.0)


def test_verified_activation_is_success():
    p = FakePlatform()
    result, new_cache = _run(p, _request(TEXTEDIT.bundle_id))
    assert result.status is A.SUCCESS and result.verification is V.VERIFIED and p.activations == [TEXTEDIT.pid]
    ev = result.evidence
    assert ev["frontmost"] == SAFARI.bundle_id and ev["frontmost_after"] == TEXTEDIT.bundle_id
    assert ev["target_ax_frontmost_after"] is True and ev["target_running_after"] is True
    assert ev["activate_returned"] is True and ev["obs_before"] == "obs-1" and ev["obs_after"] == "obs-2"
    assert new_cache.observation.app == TEXTEDIT and result.mechanism == "ns_running_application.activate"


@pytest.mark.parametrize("bundle, reason", [
    ("com.example.notrunning", "APP_NOT_RUNNING_OR_UNKNOWN (nothing is launched)"),
])
def test_not_running_or_unknown_is_blocked_and_nothing_is_launched(bundle, reason):
    p = FakePlatform()
    result, _ = _run(p, _request(bundle), scope=SCOPE | {bundle})
    assert result.status is A.BLOCKED and result.reason == reason and p.activations == []


def test_process_gone_since_observation_is_blocked_and_relaunch_is_stale():
    p = FakePlatform(live_pids={SAFARI.bundle_id: SAFARI.pid})
    assert _run(p, _request(TEXTEDIT.bundle_id))[0].reason == "APP_NOT_RUNNING (nothing is launched)"
    p = FakePlatform(live_pids={SAFARI.bundle_id: SAFARI.pid, TEXTEDIT.bundle_id: 9999})
    p.pids[TEXTEDIT.bundle_id + ".old"] = TEXTEDIT.pid        # old pid still "running" under another name
    r, _ = _run(p, _request(TEXTEDIT.bundle_id))
    assert r.status is A.STALE and r.reason == "APP_PROCESS_CHANGED" and p.activations == []


def test_outside_allowed_scope_asks():
    p = FakePlatform()
    r, _ = _run(p, _request(CHROME.bundle_id))
    assert r.status is A.BLOCKED and r.reason == "ASK:OUTSIDE_ALLOWED_APPS" and p.activations == []


@pytest.mark.parametrize("app, reason", [(TERMINAL, "RESTRICTED_APP"), (KEYCHAIN, "SENSITIVE_APP")])
def test_sensitive_and_restricted_apps_are_blocked(app, reason):
    p = FakePlatform()
    r, _ = _run(p, _request(app.bundle_id))
    assert r.status is A.BLOCKED and r.reason == reason and p.activations == []


def test_evie_is_blocked():
    p = FakePlatform()
    r, _ = _run(p, _request(TEXTEDIT.bundle_id), policy=GatePolicy(evie_pids=frozenset({TEXTEDIT.pid})))
    assert r.status is A.BLOCKED and r.reason == "EVIE_SELF" and p.activations == []


def test_unknown_identity_cannot_even_be_requested():
    with pytest.raises(ValidationError):
        ActivateAppArgs(bundle_id="Safari; open -a Terminal")


def test_unknown_frontmost_is_blocked():
    p = FakePlatform()
    unknown = cross_check([FrontmostReading(source="nsworkspace", app=SAFARI)])
    r, _ = _run(p, _request(TEXTEDIT.bundle_id), frontmost=unknown)
    assert r.status is A.BLOCKED and r.reason.startswith("FRONTMOST_UNRESOLVED") and p.activations == []


def test_already_frontmost_is_not_re_activated():
    p = FakePlatform(front=TEXTEDIT)
    r, _ = _run(p, _request(TEXTEDIT.bundle_id))
    assert r.status is A.BLOCKED and r.reason == "ALREADY_FRONTMOST" and p.activations == []


def test_api_returning_false_is_failed():
    r, _ = _run(FakePlatform(returns=False, effect=False), _request(TEXTEDIT.bundle_id))
    assert r.status is A.FAILED and r.execution is E.ERROR and r.reason == "ACTIVATE_RETURNED_FALSE"


def test_api_true_without_effect_is_failed():
    r, _ = _run(FakePlatform(effect=False), _request(TEXTEDIT.bundle_id))
    assert r.status is A.FAILED and r.verification is V.VERIFIED_NO_CHANGE and r.evidence["activate_returned"] is True


def test_independent_sources_disagreeing_is_not_verifiable():
    r, _ = _run(FakePlatform(disagree=True), _request(TEXTEDIT.bundle_id),
                frontmost=cross_check([FrontmostReading(source="nsworkspace", app=SAFARI),
                                       FrontmostReading(source="app_ax_frontmost", app=SAFARI)]))
    assert r.status is A.NOT_VERIFIABLE and r.reason == "FRONTMOST_SOURCES_DISAGREE"


def test_ax_frontmost_disagreement_is_not_verifiable():
    r, _ = _run(FakePlatform(self_report=False), _request(TEXTEDIT.bundle_id))
    assert r.status is A.NOT_VERIFIABLE and r.evidence["target_ax_frontmost_after"] is False


def test_process_exiting_after_activation_is_not_success():
    r, new_cache = _run(FakePlatform(exits=True), _request(TEXTEDIT.bundle_id))
    assert r.status is A.NOT_VERIFIABLE and r.reason == "TARGET_PROCESS_EXITED" and new_cache is None


def test_stale_request_and_no_retry():
    p = FakePlatform(effect=False)
    clock = Clock()
    first, new_cache = _run(p, _request(TEXTEDIT.bundle_id), clock=clock)
    again, _ = _run(p, _request(TEXTEDIT.bundle_id), cache=new_cache, clock=clock)   # its observation is spent
    assert first.status is A.FAILED and again.status is A.STALE and again.reason == "OBSERVATION_NOT_LATEST"
    assert p.activations == [TEXTEDIT.pid]


def test_fresh_observation_is_required():
    clock = Clock()
    old = _cache(clock)
    old.taken_monotonic -= 61
    p = FakePlatform()
    r, _ = _run(p, _request(TEXTEDIT.bundle_id), cache=old, clock=clock)
    assert r.status is A.STALE and r.reason == "OBSERVATION_EXPIRED" and p.activations == []


def test_session_round_trip():
    clock = Clock()
    cache = _cache(clock)
    session = ComputerControlSession("sess", "switch to TextEdit", frozenset({TEXTEDIT.bundle_id, SAFARI.bundle_id}),
                                     clock=clock)
    session.start(PermissionStatus(ax_trusted=True, ax_functional=True))
    session.observe(cache.observation)
    req = session.propose({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": TEXTEDIT.bundle_id}},
                          "obs-1")
    assert req is not None and session.authorize(req)
    result, _ = _run(FakePlatform(), req, cache=cache, clock=clock, scope=session.allowed_apps)
    assert result.status is A.SUCCESS and session.record_result(result) is SessionState.OBSERVING
