"""
Step 17: redacted per-attempt audit records in computer_control_steps, and the router entry point.
Fake world / fake worker / in-memory + temp SQLite. No macOS, no network.
"""

import json
import os
import sqlite3

import pytest

from cc_fixtures import ALIGN_CENTER, TEXT_AREA, TEXTEDIT, FakeClock, observation
from services.computer_control.audit import safe_label, target_summary
from services.computer_control.chooser_boundary import FastPathChooser
from services.computer_control.models import ActionResult, ExecutionStatus as E, Op, PressKeyArgs
from services.computer_control.orchestrator import ActionIntent, Orchestrator, Plan, TargetSelector
from services.computer_control_log import SqliteStepLog
from test_computer_control_session_integration import World, new_session, run as run_cc


class MemLog:
    def __init__(self, ready=True, fail_write=False):
        self.rows, self._ready, self.fail_write = [], ready, fail_write

    def ready(self):
        return self._ready

    def write(self, record):
        if self.fail_write:
            raise OSError("disk full")
        self.rows.append(record)


def run(utterance, world=None, log=None, **kw):
    world = world or World()
    log = log if log is not None else MemLog()
    s = new_session(utterance, world, clock=FakeClock())
    world.clock = world.clock or FakeClock()
    result = Orchestrator(s, world.observe, world.act, step_log=log).run_command(FastPathChooser(), TEXTEDIT.bundle_id)
    return result, world, log


def statuses(log):
    return [(r.op, r.status) for r in log.rows]


# --- 1-4: every attempted decision is recorded once, with its outcome ---

def test_successful_action_record():
    result, world, log = run("type hello")
    assert statuses(log) == [("set_text", "SUCCESS"), (None, "NOT_VERIFIABLE")]     # action, then the DONE claim
    r = log.rows[0]
    assert (r.decision_source, r.execution, r.verification, r.step) == ("fast_path", "EXECUTED", "VERIFIED", 1)
    assert r.gate_decision.startswith("ALLOW:LOW") and r.target_summary == {"app_bundle_id": TEXTEDIT.bundle_id,
                                                                            "role": "AXTextArea"}
    assert [x.step for x in log.rows] == [1, 2]


def test_blocked_and_asked_decisions_are_recorded_without_execution():
    for utterance, gate in (("delete this file", "DECISION:BLOCKED:CHOOSER_BLOCKED:DESTRUCTIVE_NOT_ENABLED"),
                            ("click that", "DECISION:ASKING:CHOOSER_ASK:UNRESOLVED_REFERENCE")):
        _, world, log = run(utterance)
        assert world.acts() == [] and len(log.rows) == 1
        r = log.rows[0]
        assert (r.op, r.status, r.execution) == (None, "BLOCKED", "NOT_EXECUTED") and r.gate_decision == gate


def test_gate_block_on_a_secure_field_is_recorded_redacted():
    secure = dict(TEXT_AREA, role="AXTextField", subrole="AXSecureTextField", label="Password")
    _, world, log = run("type hunter2", World(targets=(secure,)))
    assert world.acts() == []
    blob = json.dumps([r.model_dump() for r in log.rows])
    assert "hunter2" not in blob and "Password" not in blob


@pytest.mark.parametrize("outcome, status, execution", [("STALE", "STALE", "NOT_EXECUTED"), ("FAILED", "FAILED",
                                                        "EXECUTED"), ("NOT_VERIFIABLE", "NOT_VERIFIABLE", "EXECUTED")])
def test_stale_failed_and_unverified_attempts_are_recorded(outcome, status, execution):
    _, world, log = run("type hello", World(outcomes=[outcome]))
    assert len(world.acts()) == 1 and statuses(log) == [("set_text", status)] and log.rows[0].execution == execution


# --- 5-7: redaction ---

def test_typed_text_is_never_persisted_even_in_sqlite(tmp_path):
    db = str(tmp_path / "cc.db")
    _, _, _ = run("type hunter2-SECRET-VALUE", log=SqliteStepLog(db))
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT * FROM computer_control_steps").fetchall()
    assert len(rows) == 2 and "hunter2" not in json.dumps(rows)


def test_labels_are_redacted_or_dropped():
    base = observation((dict(ALIGN_CENTER),), obs_id="o").targets[0]
    assert safe_label(base) == "align center"
    assert safe_label(base.model_copy(update={"label": "Reset password now"})) == "[REDACTED]"
    assert safe_label(base.model_copy(update={"label": "ghp_" + "a" * 36})) == "[REDACTED]"
    text = observation((TEXT_AREA,), obs_id="o").targets[0].model_copy(update={"label": "Dear Bob, my secret..."})
    assert safe_label(text) is None


def test_press_key_record_has_only_the_bounded_key():
    s = target_summary(None, TEXTEDIT, key="RIGHT_ARROW")
    assert s == {"app_bundle_id": TEXTEDIT.bundle_id, "key": "RIGHT_ARROW"}
    utterance = "press the right arrow key"
    world, log = World(), MemLog()
    session = new_session(utterance, world, clock=FakeClock())
    plan = Plan(utterance=utterance, final_observe_bundle=TEXTEDIT.bundle_id, intents=(ActionIntent(
        op=Op.PRESS_KEY, observe_bundle=TEXTEDIT.bundle_id, target=TargetSelector(role="AXTextArea"),
        args={"key": "RIGHT_ARROW"}),))
    Orchestrator(session, world.observe, world.act, step_log=log).run(plan)
    blob = json.dumps([r.model_dump() for r in log.rows])
    assert log.rows[0].op == "press_key" and log.rows[0].target_summary["key"] == "RIGHT_ARROW"
    assert "124" not in blob and "keycode" not in blob and "flags" not in blob
    assert log.rows[0].decision_source == "plan"


# --- 8-10: logging is observational, fail-safe, and not duplicated ---

def test_unavailable_log_means_no_action():
    result, world, log = run("type hello", log=MemLog(ready=False))
    assert world.acts() == [] and result.stop_reason == "AUDIT_LOG_UNAVAILABLE"


def test_failed_write_halts_further_actions():
    result, world, log = run("type hello", log=MemLog(fail_write=True))
    assert len(world.acts()) == 1 and result.final_state.value == "BLOCKED"
    assert result.stop_reason == "AUDIT_LOG_WRITE_FAILED"


def test_one_record_per_attempt_no_duplicates_across_layers():
    _, world, log = run("scroll down")
    assert len(log.rows) == 2 and len([r for r in log.rows if r.op == "scroll"]) == 1


def test_stop_decision_is_recorded_as_cancelled():
    _, world, log = run("stop")
    assert statuses(log) == [(None, "CANCELLED")] and world.acts() == []


# --- router entry point ---

from services import tool_registry, tool_router  # noqa: E402
from services.computer_control_router import ComputerControlRuntime, is_computer_control_command, set_runtime  # noqa


@pytest.mark.parametrize("message, captured", [
    ("type hello", True), ("switch to Chrome", True), ("scroll down", True), ("click align center", True),
    ("delete this file", True), ("stop", True), ("open findings", False), ("open Chrome", False),
    ("write a summary of my findings", False), ("enter the dragon", False), ("what is my security score", False),
    ("Center this paragraph", True),                     # CC-1b.1 bounded alignment phrase
])
def test_router_capture_policy(message, captured):
    assert is_computer_control_command(message) is captured


def test_llm_can_neither_see_nor_call_computer_control(tmp_path):
    import main
    db = str(tmp_path / "r.db")
    main.init_db(db)
    assert all(t["name"] != "computer_control" for t in tool_registry.llm_tool_definitions(db))
    r = tool_router.execute_tool_call("computer_control", {"command": "type hello"}, channel="chat", db_path=db,
                                      conversation={}, planner="llm")
    assert r["tool"]["reason"] == "TOOL_NOT_AVAILABLE_TO_LLM"


def test_without_a_runtime_the_tool_is_unavailable(tmp_path):
    import main
    db = str(tmp_path / "r.db")
    main.init_db(db)
    set_runtime(None)
    r = tool_router.route_message("type hello", "chat", db, {})
    assert r["tool"]["name"] == "computer_control" and r["computer_control"]["status"] == "UNAVAILABLE"


class RouterWorker:
    """Fake worker with a production-identity probe and frontmost observation."""

    def __init__(self, world, verified=True):
        from test_computer_control_runtime_identity import _real_contract_identity, result
        from services.computer_control.runtime import AccessibilityStatus as A
        from services.computer_control.worker import WorkerState, WorkerStatus
        self.world, self.acts = world, []
        probe = result(_real_contract_identity(), ax=A.TRUSTED if verified else A.NOT_TRUSTED)
        self.status = WorkerStatus(state=WorkerState.READY, pid=702, probe=probe)

    def probe(self):
        return self.status

    def observe(self, *, bundle_id=None, frontmost=False, **kw):
        return self.world.observe(bundle_id or TEXTEDIT.bundle_id)

    def act(self, request, *, utterance, allowed_apps):
        self.acts.append(request.op.value)
        return self.world.act(request, utterance, allowed_apps)


def test_router_runs_a_logged_session_under_verified_identity(tmp_path):
    db = str(tmp_path / "r.db")
    world = World()
    world.clock = FakeClock()
    runtime = ComputerControlRuntime(RouterWorker(world), SqliteStepLog(db), clock=FakeClock())
    summary = runtime.run("type hello")
    assert summary["final_state"] == "DONE_UNVERIFIED" and summary["actions"][0]["status"] == "SUCCESS"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT op, status FROM computer_control_steps").fetchall() == [
            ("set_text", "SUCCESS"), (None, "NOT_VERIFIABLE")]


def test_unverified_identity_starts_no_session(tmp_path):
    world = World()
    worker = RouterWorker(world, verified=False)
    runtime = ComputerControlRuntime(worker, SqliteStepLog(str(tmp_path / "r.db")), clock=FakeClock())
    assert runtime.run("type hello")["status"] == "IDENTITY_NOT_VERIFIED" and worker.acts == [] and world.log == []


def test_stop_cancels_the_active_session(tmp_path):
    world = World()
    world.clock = FakeClock()
    holder = {}

    def checkpoint(where, session):
        if where == "before_execute" and "stopped" not in holder:
            holder["stopped"] = runtime.run("stop")
    runtime = ComputerControlRuntime(RouterWorker(world), SqliteStepLog(str(tmp_path / "r.db")),
                                     checkpoint=checkpoint, clock=FakeClock())
    summary = runtime.run("type hello")
    assert holder["stopped"]["status"] == "STOPPED" and summary["final_state"] == "CANCELLED"
    assert world.acts() == []


@pytest.mark.parametrize("message", ["type hello", "type my security score", "switch to Chrome", "scroll down",
                                     "click align center"])
def test_unambiguous_commands_route_before_intent_parsing(message, tmp_path):
    import main
    db = str(tmp_path / "r.db")
    main.init_db(db)
    assert tool_router.keyword_plan(message, db, {}).tool_name == "computer_control"


@pytest.mark.parametrize("message, tool", [("open Chrome", "open_app"), ("What is my security score?",
                                                                         "get_security_score"),
                                           ("what PRs are open", "get_unreviewed_prs")])
def test_existing_routes_unchanged(message, tool, tmp_path):
    import main
    db = str(tmp_path / "r.db")
    main.init_db(db)
    assert tool_router.keyword_plan(message, db, {}).tool_name == tool
