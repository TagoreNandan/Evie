"""
ComputerControlSession pure state machine (docs/07 Section 6). Deterministic: fake clock, no threads,
no sleeps, no worker. The "worker" here is the test itself building ActionResults.
"""

import pytest

from cc_fixtures import CHROME, SAFARI, TEXTEDIT, FakeClock, observation, window
from services.computer_control.models import (
    ActionResult, ActionStatus, ExecutionStatus, Op, PermissionStatus, SettleStatus, VerificationStatus,
)
from services.computer_control.session import (
    ActiveSessionSlot, ComputerControlSession, SessionConflict, SessionError, SessionLimits, SessionState as S,
)
from services.computer_control.verification import AppFrontmostGoal

OK = PermissionStatus(ax_trusted=True, ax_functional=True)
SCOPE = frozenset({TEXTEDIT.bundle_id, CHROME.bundle_id})
TYPE = {"status": "ACTION", "op": "set_text", "target_index": 0, "args": {"text_span": [5, 16]}}
CENTER = {"status": "ACTION", "op": "press", "target_index": 3}


def _session(**kw):
    clock = kw.pop("clock", FakeClock())
    s = ComputerControlSession("sess-1", kw.pop("utterance", "type hello world"), kw.pop("scope", SCOPE),
                               clock=clock, **kw)
    s.start(OK)
    return s, clock


def _obs(n=1, **kw):
    kw.setdefault("taken_at", float(n))
    return observation(obs_id=f"obs-{n}", fingerprint=kw.pop("fingerprint", f"fp-{n}"), **kw)


def _result(req, verification=VerificationStatus.VERIFIED, changed=True, execution=ExecutionStatus.EXECUTED):
    return ActionResult.executed(execution=execution, verification=verification, fingerprint_changed=changed,
                                 session_id=req.session_id, step=req.step, op=req.op, requested_at=1.0)


def _not_executed(req, status, **kw):
    return ActionResult(session_id=req.session_id, step=req.step, op=req.op, status=status,
                        execution=ExecutionStatus.NOT_EXECUTED, verification=VerificationStatus.NOT_APPLICABLE,
                        requested_at=1.0, **kw)


def _one_action(s, n, decision=TYPE, **result_kw):
    s.observe(_obs(n))
    req = s.propose(decision, f"obs-{n}")
    assert req is not None and s.authorize(req)
    return s.record_result(_result(req, **result_kw)), req


# --- Lifecycle and transitions ---

def test_happy_path_transitions():
    s, _ = _session()
    assert s.state is S.OBSERVING
    assert s.observe(_obs(1)) is S.CHOOSING
    req = s.propose(TYPE, "obs-1")
    assert s.state is S.EXECUTING and req.op is Op.SET_TEXT and req.obs_id == "obs-1" and req.cancel_epoch == 0
    assert s.authorize(req)
    assert s.record_result(_result(req)) is S.OBSERVING
    events = [(h.event, h.state) for h in s.history]
    for expected in [("resolve", S.RESOLVING), ("gate", S.GATING), ("approved", S.EXECUTING),
                     ("result", S.VERIFYING), ("result", S.OBSERVING)]:
        assert expected in events


def test_permission_failure_fails_closed():
    s = ComputerControlSession("s", "type x", SCOPE, clock=FakeClock())
    assert s.start(PermissionStatus(ax_trusted=True, ax_functional=False)) is S.ERROR
    assert s.stop_reason == "PERMISSION_UNAVAILABLE"
    assert s.observe(_obs(1)) is S.ERROR


def test_fail_enters_error_state():
    s, _ = _session()
    s.observe(_obs(1))
    assert s.fail("WORKER_UNAVAILABLE") is S.ERROR and s.stop_reason == "WORKER_UNAVAILABLE"


# --- One action per iteration / latest observation ---

def test_one_action_per_iteration():
    s, _ = _session()
    s.observe(_obs(1))
    req = s.propose(TYPE, "obs-1")
    with pytest.raises(SessionError):
        s.propose(TYPE, "obs-1")                       # second action before the first completes
    s.authorize(req)
    s.record_result(_result(req))
    with pytest.raises(SessionError):
        s.propose(TYPE, "obs-1")                       # must re-observe first


def test_stale_decision_rejected_without_takeover():
    s, _ = _session()
    s.observe(_obs(1))
    assert s.observe(_obs(2)) is S.CHOOSING            # same app/window: not a takeover
    assert s.propose(TYPE, "obs-1") is None and s.state is S.CHOOSING
    assert s.history[-1].reason == "DECISION_FOR_STALE_OBSERVATION"
    assert s.propose(TYPE, "obs-2") is not None


def test_worker_stale_result_returns_to_observing():
    s, _ = _session()
    s.observe(_obs(1))
    req = s.propose(TYPE, "obs-1")
    s.authorize(req)
    assert s.record_result(_not_executed(req, ActionStatus.STALE, retry_permitted=True)) is S.OBSERVING
    assert s.consecutive_noops == 0


def test_unsettled_observation_is_not_acted_on():
    s, _ = _session()
    assert s.observe(_obs(1, settle=SettleStatus.GROWING)) is S.OBSERVING
    assert s.observation is None


def test_degraded_observation_blocks():
    s, _ = _session()
    assert s.observe(_obs(1, caps_hit=("node_cap",))) is S.BLOCKED


# --- Cancellation ---

def test_cancellation_before_execution():
    s, _ = _session()
    s.observe(_obs(1))
    req = s.propose(TYPE, "obs-1")
    s.cancel()
    assert not s.authorize(req) and s.state is S.CANCELLED and s.stop_reason == "USER_STOP"


def test_cancellation_after_chooser_returns():
    s, _ = _session()
    s.observe(_obs(1))
    s.cancel()                                          # Stop arrives while the chooser is running
    assert s.propose(TYPE, "obs-1") is None and s.state is S.CANCELLED


def test_cancel_epoch_for_worker_side_check():
    s, _ = _session()
    s.observe(_obs(1))
    req = s.propose(TYPE, "obs-1")
    assert s.authorize(req) and s.is_epoch_current(req.cancel_epoch)
    assert s.cancel() == req.cancel_epoch + 1
    assert not s.is_epoch_current(req.cancel_epoch)
    assert s.record_result(_result(req)) is S.CANCELLED   # in-flight result is logged, nothing continues
    assert s.history[-1].reason.startswith("AFTER_TERMINAL")


# --- Limits ---

def test_action_limit():
    s, _ = _session(limits=SessionLimits(max_actions=40))
    n = 0
    while s.state is not S.LIMIT_REACHED:
        n += 1
        decision = CENTER if n % 2 else {"status": "ACTION", "op": "press", "target_index": 2}
        _one_action(s, n, decision=decision)
        assert n <= 40
    assert n == 40 and s.stop_reason == "ACTION_LIMIT"


def test_three_consecutive_noops():
    s, _ = _session()
    _one_action(s, 1, verification=VerificationStatus.VERIFIED_NO_CHANGE)
    _one_action(s, 2, decision=CENTER, verification=VerificationStatus.VERIFIED_NO_CHANGE)
    assert s.consecutive_noops == 2 and s.state is S.OBSERVING
    state, _ = _one_action(s, 3, decision={"status": "ACTION", "op": "scroll", "target_index": 1,
                                             "args": {"direction": "down"}},
                           verification=VerificationStatus.VERIFIED_NO_CHANGE)
    assert state is S.LIMIT_REACHED and s.stop_reason == "NOOP_LIMIT"


def test_success_resets_noops():
    s, _ = _session()
    _one_action(s, 1, verification=VerificationStatus.VERIFIED_NO_CHANGE)
    _one_action(s, 2, decision=CENTER)
    assert s.consecutive_noops == 0


def test_wall_clock_limit():
    s, clock = _session()
    s.observe(_obs(1))
    req = s.propose(TYPE, "obs-1")
    clock.advance(180.0)
    assert not s.authorize(req) and s.state is S.LIMIT_REACHED and s.stop_reason == "WALL_CLOCK_LIMIT"


def test_rejected_chooser_output_is_a_noop():
    s, _ = _session()
    s.observe(_obs(1))
    for _ in range(3):
        assert s.propose({"status": "ACTION", "op": "shell", "args": {"cmd": "ls"}}, "obs-1") is None
    assert s.state is S.LIMIT_REACHED and s.stop_reason == "NOOP_LIMIT"


def test_per_action_timeout_hook():
    with pytest.raises(ValueError):
        SessionLimits(per_action_timeout_s=3.0)
    s, _ = _session(limits=SessionLimits(per_action_timeout_s=12.0))
    s.observe(_obs(1))
    assert s.propose(TYPE, "obs-1").timeout_s == 12.0


def test_model_budget_and_daily_cap_hooks():
    s, _ = _session()
    assert not s.can_call_model()                       # CC-1a: zero model budget
    assert s.record_model_usage(10, 5, 0.0001) is S.LIMIT_REACHED and s.stop_reason == "MODEL_BUDGET"
    s2, _ = _session(daily_cap_ok=lambda: False, limits=SessionLimits(token_budget=1000, cost_budget_usd=1.0))
    assert not s2.can_call_model()
    s2.observe(_obs(1))
    req = s2.propose(TYPE, "obs-1")
    assert not s2.authorize(req) and s2.stop_reason == "DAILY_SPEND_CAP"


# --- No blind retries ---

def test_failed_action_cannot_repeat_on_unchanged_observation():
    s, _ = _session()
    _one_action(s, 1, verification=VerificationStatus.VERIFIED_NO_CHANGE)
    s.observe(_obs(2, fingerprint="fp-1"))              # nothing changed on screen
    assert s.propose(TYPE, "obs-2") is None
    assert s.state is S.CHOOSING and s.history[-1].reason == "REPEAT_OF_FAILED_ACTION"


def test_fresh_observation_allows_replanning():
    s, _ = _session()
    _one_action(s, 1, verification=VerificationStatus.VERIFIED_NO_CHANGE)
    s.observe(_obs(2, fingerprint="fp-2"))              # the screen changed
    assert s.propose(TYPE, "obs-2") is not None


# --- User takeover ---

def test_user_takeover_pauses_and_asks():
    s, _ = _session()
    s.observe(_obs(1))
    assert s.observe(_obs(2, frontmost=CHROME)) is S.ASKING and s.stop_reason == "USER_TAKEOVER"


def test_window_change_without_evie_action_is_takeover():
    s, _ = _session()
    s.observe(_obs(1))
    assert s.observe(_obs(2, win=window(doc="doc-b"))) is S.ASKING


def test_change_after_evie_acted_is_not_takeover():
    s, _ = _session(utterance="switch to Google Chrome")
    s.observe(_obs(1))
    req = s.propose({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": CHROME.bundle_id}}, "obs-1")
    s.authorize(req)
    s.record_result(_result(req))
    assert s.observe(_obs(2, frontmost=CHROME)) is S.CHOOSING


# --- DONE / BLOCKED / ASK ---

def test_done_with_goal_verifier():
    s, _ = _session(goal_check=AppFrontmostGoal(TEXTEDIT.bundle_id))
    s.observe(_obs(1))
    assert s.propose({"status": "DONE"}, "obs-1") is None and s.state is S.DONE_VERIFIED


def test_done_without_verifier_is_unverified():
    s, _ = _session()
    s.observe(_obs(1))
    s.propose({"status": "DONE"}, "obs-1")
    assert s.state is S.DONE_UNVERIFIED and s.stop_reason == "NO_GOAL_VERIFIER"


def test_false_done_claim_is_not_done():
    s, _ = _session(goal_check=AppFrontmostGoal(CHROME.bundle_id))
    s.observe(_obs(1))
    s.propose({"status": "DONE"}, "obs-1")
    assert s.state is S.CHOOSING and s.consecutive_noops == 1


def test_blocked_and_ask_states():
    s, _ = _session()
    s.observe(_obs(1))
    s.propose({"status": "BLOCKED", "reason": "can't"}, "obs-1")
    assert s.state is S.BLOCKED
    s2, _ = _session()
    s2.observe(_obs(1))
    s2.propose({"status": "ASK", "question": "Which document?"}, "obs-1")
    assert s2.state is S.ASKING and s2.question == "Which document?"


def test_gate_block_and_ask_end_the_iteration():
    s, _ = _session()
    s.observe(_obs(1))
    assert s.propose({"status": "ACTION", "op": "press", "target_index": 4}, "obs-1") is None   # plain button
    assert s.state is S.BLOCKED and "TARGET_CLASS_NOT_VALIDATED" in s.stop_reason
    s2, _ = _session()
    s2.observe(_obs(1))
    s2.propose({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": SAFARI.bundle_id}}, "obs-1")
    assert s2.state is S.ASKING and s2.stop_reason == "OUTSIDE_ALLOWED_APPS"


def test_ambiguous_or_missing_target_from_resolver():
    s, _ = _session()
    s.observe(_obs(1, targets=({"role": "AXTextArea", "enabled": False},)))
    s.propose({"status": "ACTION", "op": "set_text", "target_index": 0, "args": {"text_span": [5, 16]}}, "obs-1")
    assert s.state is S.BLOCKED and s.stop_reason == "TARGET_DISABLED"


# --- Only one active session ---

def test_single_active_session_slot():
    slot = ActiveSessionSlot()
    a, _ = _session()
    b, _ = _session()
    slot.claim(a)
    with pytest.raises(SessionConflict):
        slot.claim(b)
    slot.claim(b, preempt=True)
    assert a.state is S.CANCELLED and a.stop_reason == "PREEMPTED" and slot.current is b
    slot.release(b)
    assert slot.current is None


def test_allowed_apps_never_change_during_a_session():
    s, _ = _session()
    before = s.allowed_apps
    _one_action(s, 1)
    s.observe(_obs(2))
    s.propose({"status": "ACTION", "op": "activate_app", "args": {"bundle_id": SAFARI.bundle_id}}, "obs-2")
    assert s.allowed_apps == before and SAFARI.bundle_id not in s.allowed_apps
