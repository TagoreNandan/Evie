"""
CC-1a Step 14B: the first Evie-owned live mutation validation (docs/07_Computer_Control.md Section 36).

    Evie.app (mode live_validation_14b) -> evie-python -m services.cc_live_validation.step14b -> existing worker

ONE invocation runs, in order, stopping at the first failure (no retries):
  0. preconditions: TextEdit NOT running; worker READY; production_identity_verified; Finder running
  1. TEXT       fixture -> set_text replace_all EVIE_KEYBOARD_TEST -> set_text replace_all EVIE_FIXTURE_MARKER
  2. SCROLL     fixture -> scroll down 0.25 -> scroll up 0.25 (visible start restored exactly)
  3. ALIGNMENT  fixture (left) -> press align center -> press align left
  4. ACTIVATION TextEdit <-> Finder: activate TextEdit -> activate Finder (goal: Finder frontmost)
  5. MULTI-STEP (4 actions): activate TextEdit -> set_text EVIE_MULTISTEP_TEST -> set_text EVIE_FIXTURE_MARKER
                -> activate Finder (goal: Finder frontmost)
Every action goes through the EXISTING Step 6 path: Orchestrator.run(Plan) -> fresh observation -> intent
(CHOOSE) -> resolve -> session.propose (validate, resolve, code-owned gate) -> freshness -> authorize -> ONE
worker act (worker re-checks freshness, gate, live element, main window; executes; verifies) -> re-observe.

Between fixture phases, TEST INFRASTRUCTURE (fixtures.FixtureLifecycle) ends TextEdit and opens the next
scratch file in the background - only after the phase's restoration was verified. That is reported as
fixture_process_lifecycle_actions and never counted as computer control.
Sentinels: identity is re-probed before EVERY execution (checkpoint "before_execute"); the act port refuses
any operation outside {set_text, scroll, press(align center|align left), activate_app(TextEdit|Finder)}.
"""

import json
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.cc_live_validation import fixtures as F
from services.computer_control.gate import derive_allowed_apps
from services.computer_control.models import Op, normalize
from services.computer_control.orchestrator import ActionIntent, Goal, Orchestrator, Plan, TargetSelector, goal_check_for
from services.computer_control.runtime import PRODUCTION_IDENTITY_CONTRACT as CONTRACT
from services.computer_control.session import ComputerControlSession, SessionState
from services.computer_control.worker import ComputerControlWorker, WorkerState

TEXTEDIT, FINDER = F.TEXTEDIT_BUNDLE, "com.apple.finder"
APPROVED_OPS = frozenset({Op.SET_TEXT, Op.SCROLL, Op.PRESS, Op.ACTIVATE_APP})
APPROVED_PRESS_LABELS = frozenset({"align center", "align left"})
APPROVED_ACTIVATION = frozenset({TEXTEDIT, FINDER})
FIXTURE_WAIT_TRIES, FIXTURE_WAIT_S = 20, 0.5
SCROLL_VALUE_TOLERANCE = 0.001        # scroll bar value; the visible start must match EXACTLY (EVIDENCE.md)

TEXT_SEL = TargetSelector(role="AXTextArea")
SCROLL_SEL = TargetSelector(role="AXScrollArea")
CENTER_SEL = TargetSelector(role="AXCheckBox", subrole="AXSegment", label="align center")
LEFT_SEL = TargetSelector(role="AXCheckBox", subrole="AXSegment", label="align left")


def _span(utterance: str, text: str, last: bool = False) -> List[int]:
    start = utterance.rindex(text) if last else utterance.index(text)
    return [start, start + len(text)]


def _set_text(u: str, text: str, observe: str = TEXTEDIT) -> ActionIntent:
    return ActionIntent(op=Op.SET_TEXT, observe_bundle=observe, target=TEXT_SEL,
                        args={"text_span": _span(u, text), "mode": "replace_all"})


def _activate(bundle: str, observe: str) -> ActionIntent:
    return ActionIntent(op=Op.ACTIVATE_APP, observe_bundle=observe, args={"bundle_id": bundle})


U_TEXT = "switch to TextEdit (Finder stays), replace the text with EVIE_KEYBOARD_TEST then restore EVIE_FIXTURE_MARKER"
U_SCROLL = "in TextEdit scroll down then scroll up"
U_ALIGN = "in TextEdit press align center then press align left"
U_ACTIVATE = "switch to TextEdit (setup), then TextEdit to Finder, then Finder to TextEdit"
U_MULTI = "switch to Finder, switch to TextEdit, replace the text with EVIE_MULTISTEP_TEST, restore EVIE_FIXTURE_MARKER"

PLANS: Dict[str, Plan] = {
    "text": Plan(utterance=U_TEXT, final_observe_bundle=TEXTEDIT, goal=Goal(kind="frontmost_app", bundle_id=TEXTEDIT),
                 intents=(_activate(TEXTEDIT, FINDER), _set_text(U_TEXT, "EVIE_KEYBOARD_TEST"),
                          _set_text(U_TEXT, "EVIE_FIXTURE_MARKER"))),
    "scroll": Plan(utterance=U_SCROLL, final_observe_bundle=TEXTEDIT, intents=(
        ActionIntent(op=Op.SCROLL, observe_bundle=TEXTEDIT, target=SCROLL_SEL, args={"direction": "down", "amount": 0.25}),
        ActionIntent(op=Op.SCROLL, observe_bundle=TEXTEDIT, target=SCROLL_SEL, args={"direction": "up", "amount": 0.25}))),
    "alignment": Plan(utterance=U_ALIGN, final_observe_bundle=TEXTEDIT, intents=(
        ActionIntent(op=Op.PRESS, observe_bundle=TEXTEDIT, target=CENTER_SEL),
        ActionIntent(op=Op.PRESS, observe_bundle=TEXTEDIT, target=LEFT_SEL))),
    "activation": Plan(utterance=U_ACTIVATE, final_observe_bundle=FINDER,
                       goal=Goal(kind="frontmost_app", bundle_id=TEXTEDIT), intents=(
        _activate(TEXTEDIT, FINDER), _activate(FINDER, FINDER), _activate(TEXTEDIT, FINDER))),
    "multistep": Plan(utterance=U_MULTI, final_observe_bundle=TEXTEDIT,
                      goal=Goal(kind="frontmost_app", bundle_id=TEXTEDIT), intents=(
        _activate(FINDER, TEXTEDIT), _activate(TEXTEDIT, FINDER),
        _set_text(U_MULTI, "EVIE_MULTISTEP_TEST"), _set_text(U_MULTI, "EVIE_FIXTURE_MARKER"))),
}
FIXTURE_FOR = {"text": F.TEXT, "scroll": F.SCROLL, "alignment": F.ALIGNMENT, "activation": F.TEXT}
EVIDENCE_KEYS = ("gate", "verifier", "exact_text", "chars_before", "chars_after", "mode", "scroll_before",
                 "scroll_target", "visible_before", "visible_after", "values_before", "values_after", "glyph_before",
                 "glyph_after", "target_bundle", "activate_returned", "frontmost_after", "frontmost_after_agreed",
                 "frontmost_after_sources", "target_is_active_after", "target_ax_frontmost_after",
                 "window_main_before", "obs_after", "key", "range_before", "range_after", "text_unchanged",
                 "target_pid")


class Stop(Exception):
    def __init__(self, stage: str, reason: str):
        super().__init__(f"{stage}: {reason}")
        self.stage, self.reason = stage, reason[:300]


class _Clock:
    def now(self) -> float:
        return time.monotonic()


class Step14B:
    def __init__(self, worker, lifecycle: F.FixtureLifecycle, sleep: Callable[[float], None] = time.sleep,
                 plans: Optional[Dict[str, Plan]] = None):
        self.worker, self.lifecycle, self.sleep = worker, lifecycle, sleep
        self.plans = plans or PLANS
        self.mutations: List[Dict[str, Any]] = []
        self.unexpected = 0
        self.identity_checks: List[Dict[str, Any]] = []
        self.phases: List[Dict[str, Any]] = []
        self.textedit_started_by_harness = False
        self.verify_after_activation = True          # Step 14B retry: identity re-checked right after activations
        self._last_op = None
        self.written: Dict[str, str] = {}

    # --- sentinels ---

    def _identity(self, where: str) -> Dict[str, Any]:
        status = self.worker.probe()
        p = status.probe
        resp = p.identity.responsible if p else None
        sig = resp.signature if resp and resp.signature else None
        row = {"where": where, "verified": bool(p and p.production_identity_verified),
               "bundle_id": resp.bundle_id if resp else None,
               "bundle_lookup": resp.bundle_lookup.model_dump() if resp and resp.bundle_lookup else None,
               "team_id": sig.team_id if sig else None,
               "signature_valid": sig.valid if sig else None, "responsible_pid": resp.pid if resp else None,
               "responsible_executable": resp.executable if resp else None,
               "accessibility": p.accessibility.status.value if p else None,
               "reasons": list(p.production_identity_reasons) if p else ["NO_PROBE"]}
        row["ok"] = (row["verified"] and row["bundle_id"] == CONTRACT.bundle_id and row["team_id"] == CONTRACT.team_id
                     and (row["bundle_lookup"] or {}).get("status") == "FOUND"
                     and row["signature_valid"] is True and row["accessibility"] == "TRUSTED"
                     and row["responsible_executable"] == CONTRACT.bundle_path + CONTRACT.launcher_executable)
        self.identity_checks.append(row)
        return row

    def _approved(self, request) -> bool:
        if request.op not in APPROVED_OPS:
            return False
        if request.op is Op.PRESS:
            return request.target is not None and normalize(request.target.target.label) in APPROVED_PRESS_LABELS
        if request.op is Op.ACTIVATE_APP:
            return request.args.bundle_id in APPROVED_ACTIVATION
        return True

    def act(self, request, utterance, allowed_apps):
        """The ONLY route to worker.act. Refuses anything outside the approved Step 14B set."""
        if not self._approved(request):
            self.unexpected += 1
            raise RuntimeError(f"UNEXPECTED_OPERATION {request.op.value}")
        self._last_op = request.op
        result = self.worker.act(request, utterance=utterance, allowed_apps=allowed_apps)
        self.mutations.append({"op": request.op.value, "obs_id": request.obs_id,
                               "target": None if request.target is None else
                               {"role": request.target.target.role, "subrole": request.target.target.subrole,
                                "label": request.target.target.label if request.target.target.role != "AXTextArea"
                                else None},
                               "gate": f"{request.gate.outcome.value}:{request.gate.risk.value}:{request.gate.reason}",
                               "execution": result.execution.value, "verification": result.verification.value,
                               "status": result.status.value, "latency_ms": result.latency_ms,
                               "reason": result.reason})
        return result

    # --- fixture lifecycle (test infrastructure) ---

    def _textedit_windows(self, fixture: F.Fixture):
        """Read-only: wait until TextEdit shows exactly ONE window, and it is this fixture's document."""
        last = None
        for _ in range(FIXTURE_WAIT_TRIES):
            r = self.worker.observe(bundle_id=TEXTEDIT)
            if r.status == "OK" and r.observation is not None:
                obs = r.observation
                last = [(w.document_hash, w.is_main) for w in obs.windows]
                if len(obs.windows) == 1 and obs.windows[0].document_hash == fixture.document_hash \
                        and obs.windows[0].is_main:
                    return obs
                if len(obs.windows) > 1:
                    raise Stop("FIXTURE_NOT_READY", f"{len(obs.windows)} TextEdit windows {last}")
            else:
                last = f"{r.status}:{r.reason}"
            self.sleep(FIXTURE_WAIT_S)
        raise Stop("FIXTURE_NOT_READY", f"fixture window not observed: {last}")

    def _end_textedit(self, phase: str) -> Dict[str, Any]:
        ok, pids = self.lifecycle.terminate_textedit()
        if not ok or self.lifecycle.textedit_pids():
            raise Stop("FIXTURE_NOT_READY", f"TextEdit still running after termination ({phase}): {pids}")
        return {"terminated_pids": pids, "textedit_running_after": False}

    def _prepare(self, fixture: F.Fixture) -> Dict[str, Any]:
        if self.lifecycle.textedit_pids():
            raise Stop("FIXTURE_NOT_READY", "TextEdit running before fixture preparation")
        disk = self.lifecycle.write(fixture)
        self.written[fixture.name] = disk
        code = self.lifecycle.open_in_background(fixture)
        if code != 0:
            raise Stop("FIXTURE_NOT_READY", f"open -g returned {code}")
        self.textedit_started_by_harness = True
        obs = self._textedit_windows(fixture)
        pids = self.lifecycle.textedit_pids()
        if len(pids) != 1:
            raise Stop("FIXTURE_NOT_READY", f"expected one TextEdit process, found {pids}")
        return {"fixture": fixture.name, "path": str(fixture.path), "disk_sha256_16": disk, "textedit_pid": pids[0],
                "windows": len(obs.windows), "document_matches": True, "obs_id": obs.obs_id,
                "targets": len(obs.targets), "frontmost": obs.frontmost.bundle_id if obs.frontmost else None,
                "_obs": obs}

    # --- one phase through the existing orchestrator ---

    def _run_plan(self, name: str, pre_obs) -> Tuple[Any, Dict[str, Any]]:
        plan = self.plans[name]
        allowed = derive_allowed_apps(plan.utterance, pre_obs.frontmost, pre_obs.running_apps)
        session = ComputerControlSession(f"s14b-{name}-{uuid.uuid4().hex[:6]}", plan.utterance, allowed,
                                         clock=_Clock(), goal_check=goal_check_for(plan))
        session.start(self.worker.status.probe.to_permission_status())

        def checkpoint(where: str) -> None:
            if where == "before_execute" and not session.is_terminal:
                if not self._identity(f"{name}:before_execute")["ok"]:
                    session.cancel("IDENTITY_NOT_VERIFIED")
            if where == "after_execute" and self.verify_after_activation and self._last_op is Op.ACTIVATE_APP \
                    and not session.is_terminal:
                self._last_op = None
                if not self._identity(f"{name}:after_activation")["ok"]:
                    session.cancel("IDENTITY_NOT_VERIFIED_AFTER_ACTIVATION")

        before = len(self.mutations)
        result = Orchestrator(session, lambda b: self.worker.observe(bundle_id=b), self.act,
                              checkpoint=checkpoint).run(plan)
        steps = [{"intent": s.intent, "op": s.op.value, "obs_before": s.obs_before, "gate": s.gate,
                  "status": s.status.value if s.status else None, "verification": s.verification,
                  "reason": s.reason, "evidence": {k: s.evidence[k] for k in EVIDENCE_KEYS if k in s.evidence}}
                 for s in result.steps]
        return result, {"final_state": result.final_state.value, "stop_reason": result.stop_reason,
                        "allowed_apps": sorted(allowed), "observations": list(result.observations),
                        "events": [f"{e.kind}{'' if e.step is None else f'[{e.step}]'} {e.detail}"
                                   for e in result.events],
                        "steps": steps, "mutations_in_phase": len(self.mutations) - before}

    @staticmethod
    def _all_success(result, n: int) -> bool:
        return len(result.steps) == n and all(s.status is not None and s.status.value == "SUCCESS"
                                              for s in result.steps)

    def _restoration(self, name: str, result) -> Tuple[bool, Dict[str, Any]]:
        ev = [s.evidence for s in result.steps]
        if name in ("text", "multistep"):
            texts = [e for e, s in zip(ev, result.steps) if s.op is Op.SET_TEXT]
            last = texts[-1] if texts else {}
            ok = bool(texts) and last.get("exact_text") is True and last.get("chars_after") == len(F.TEXT_MARKER)
            return ok, {"text_restored_exact": ok, "chars_after_restore": last.get("chars_after")}
        if name == "scroll":
            down, up = ev
            ok = (up.get("visible_after") == down.get("visible_before")
                  and abs(float(up.get("scroll_target", -9)) - float(down.get("scroll_before", 9)))
                  <= SCROLL_VALUE_TOLERANCE)
            return ok, {"visible_before": down.get("visible_before"), "visible_after_down": down.get("visible_after"),
                        "visible_after_restore": up.get("visible_after"), "scroll_before": down.get("scroll_before"),
                        "scroll_after_restore": up.get("scroll_target"), "scroll_restored": ok}
        if name == "alignment":
            center = json.loads(ev[0].get("values_after") or "{}")
            left = json.loads(ev[1].get("values_after") or "{}")
            ok = center.get("align center") == 1 and left.get("align left") == 1 and left.get("align center") == 0
            return ok, {"after_center": center, "after_restore": left, "alignment_restored_left": ok}
        return True, {}

    def _phase(self, name: str, n_actions: int, pre_obs, expect: str, extra: Dict[str, Any]) -> None:
        result, detail = self._run_plan(name, pre_obs)
        detail.update(extra)
        restored, rdetail = self._restoration(name, result)
        detail["restoration"] = rdetail
        passed = result.final_state.value == expect and self._all_success(result, n_actions) and restored
        detail["passed"] = passed
        self.phases.append({"phase": name, **detail})
        if not passed:
            stage = "RESTORATION_NOT_VERIFIED" if self._all_success(result, n_actions) and not restored \
                else f"PHASE_{name.upper()}_FAILED"
            raise Stop(stage, f"{result.final_state.value}: {result.stop_reason}")

    def _finder_pre(self, stage: str):
        r = self.worker.observe(bundle_id=FINDER)
        if r.status != "OK" or r.observation is None:
            raise Stop(f"PHASE_{stage.upper()}_FAILED", f"pre-observation {r.status}: {r.reason}")
        return r.observation

    def _retire_stale_fixture(self, pids) -> Dict[str, Any]:
        """TextEdit left open by the previous stopped run: end it ONLY if, read-only, it holds exactly our one
        unchanged text fixture (then it is test infrastructure). Anything else is FIXTURE_NOT_READY."""
        r = self.worker.observe(bundle_id=TEXTEDIT)
        obs = r.observation if r.status == "OK" else None
        content = F.TEXT.path.read_text() if F.TEXT.path.exists() else None
        if obs is None or len(obs.windows) != 1 or obs.windows[0].document_hash != F.TEXT.document_hash \
                or content != F.TEXT_MARKER or len(pids) != 1:
            raise Stop("FIXTURE_NOT_READY", f"TextEdit running and not exactly our unchanged text fixture "
                                            f"({r.status}, windows={len(obs.windows) if obs else None})")
        return {"pid": pids[0], "windows": 1, "document": "text fixture", **self._end_textedit("stale")}

    def _classify(self) -> str:
        """PASS is only set by a completed run. A stop before any action, or after only successful actions,
        is PARTIAL_STOPPED; a stop after an action that did not succeed is FAILED."""
        if any(m["status"] != "SUCCESS" for m in self.mutations) or self.unexpected:
            return "FAILED"
        return "PARTIAL_STOPPED"

    # --- the run ---

    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"result": "FAILED", "stopped_at": None, "stop_reason": None}
        try:
            running = self.lifecycle.textedit_pids()
            report["textedit_running_at_start"] = running
            status = self.worker.status
            if status.state is not WorkerState.READY:
                raise Stop("WORKER", f"{status.state.value}: {status.failure}")
            if not self._identity("start")["ok"]:
                raise Stop("IDENTITY", ",".join(self.identity_checks[-1]["reasons"]))
            if running:
                report["stale_fixture"] = self._retire_stale_fixture(running)

            for name, fixture in (("text", F.TEXT), ("scroll", F.SCROLL), ("alignment", F.ALIGNMENT)):
                prep = self._prepare(fixture)
                obs = prep.pop("_obs")
                extra = {"fixture": prep}
                if name == "alignment":
                    segs = {normalize(t.label): t.value_summary for t in obs.targets
                            if t.role == "AXCheckBox" and t.subrole == "AXSegment"}
                    extra["initial_alignment"] = segs
                    if segs.get("align left") != "1" or segs.get("align center") != "0":
                        raise Stop("FIXTURE_NOT_READY", f"alignment fixture not LEFT: {segs}")
                if name == "text":
                    obs = self._finder_pre("text")
                self._phase(name, 3 if name == "text" else 2, obs,
                            "DONE_VERIFIED" if name == "text" else "DONE_UNVERIFIED", extra)
                self.phases[-1]["fixture_end"] = self._end_textedit(name)

            prep = self._prepare(FIXTURE_FOR["activation"])
            prep.pop("_obs")
            fobs = self._finder_pre("activation")
            running_ids = {a.bundle_id for a in fobs.running_apps}
            if not {TEXTEDIT, FINDER} <= running_ids:
                raise Stop("ACTIVATION_FIXTURE_NOT_READY", "TextEdit/Finder not both running")
            self._phase("activation", 3, fobs, "DONE_VERIFIED",
                        {"fixture": prep, "starting_frontmost": fobs.frontmost.bundle_id if fobs.frontmost else None})
            mobs = self._finder_pre("multistep")
            self._phase("multistep", 4, mobs, "DONE_VERIFIED",
                        {"starting_frontmost": mobs.frontmost.bundle_id if mobs.frontmost else None})
            self.phases[-1]["fixture_end"] = self._end_textedit("final")
            changed = {f.name: (self.written.get(f.name), self.lifecycle.disk_hash(f)) for f in F.ALL
                       if self.lifecycle.disk_hash(f) != self.written.get(f.name)}
            report["fixtures_unchanged_on_disk"] = not changed
            if changed:
                raise Stop("RESTORATION_NOT_VERIFIED", f"fixture files changed on disk: {changed}")
            report.update(result="PASS")
        except Stop as s:
            report.update(stopped_at=s.stage, stop_reason=s.reason, result=self._classify())
        except Exception as e:                                     # never hide an unexpected error
            report.update(stopped_at="HARNESS_ERROR", stop_reason=f"{type(e).__name__}: {e}"[:300], result="FAILED")
        report.update(
            computer_control_mutation_count=len(self.mutations),
            executed_mutations=sum(1 for m in self.mutations if m["execution"] != "NOT_EXECUTED"),
            unexpected_computer_control_operations=self.unexpected,
            fixture_process_lifecycle_actions=len(self.lifecycle.actions),
            lifecycle_actions=list(self.lifecycle.actions), retries=0,
            mutations=self.mutations, identity_checks=self.identity_checks, phases=self.phases,
            textedit_running_at_end=self.lifecycle.textedit_pids(),
            fixture_disk_sha256_16={f.name: self.lifecycle.disk_hash(f) for f in F.ALL})
        return report


def main() -> int:
    worker = ComputerControlWorker(start_timeout_s=60, probe_timeout_s=60, observe_timeout_s=90, act_timeout_s=90)
    worker.start()
    try:
        report = Step14B(worker, F.FixtureLifecycle()).run()
    finally:
        stopped = worker.stop()
    report["worker_stopped"] = stopped.state is WorkerState.STOPPED
    print("STEP14B_HARNESS " + json.dumps(report, indent=1, default=str), flush=True)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
