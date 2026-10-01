"""
Router entry point for computer control (docs/07 Sections 15-16 and 37; CC-1a Step 17).

    typed/voice message -> tool_router (deterministic keyword planner) -> tool `computer_control`
    -> ComputerControlRuntime.run(verbatim utterance) -> session -> fast path -> gate -> worker

- Reached ONLY through the deterministic keyword route with the user's verbatim message (set_text provenance);
  the LLM planner can neither see nor call this tool.
- Needs a host-provided runtime (the worker + the computer_control_steps audit log). Without one the tool is
  UNAVAILABLE; it never starts a worker by itself.
- Requires signed Evie identity (production_identity_verified with bundle lookup FOUND) before a session starts
  AND before every dispatch. Otherwise nothing runs.
- "stop" cancels the one active session (ActiveSessionSlot); it never starts one.
"""

import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.computer_control.chooser import AppRef, ChooserInput
from services.computer_control.chooser_boundary import FastPathChooser
from services.computer_control.fast_path import FastPathResult, fast_path
from services.computer_control.gate import derive_allowed_apps
from services.computer_control.orchestrator import Orchestrator
from services.computer_control.session import ActiveSessionSlot, ComputerControlSession, SessionConflict

FINDER = "com.apple.finder"
TOOL_NAME = "computer_control"
# Non-sensitive verification facts only (never text, labels or values)
SUMMARY_EVIDENCE = ("verifier", "visible_before", "visible_after", "range_before", "range_after", "frontmost_after",
                    "exact_text", "target_bundle")


def classify(utterance: str, installed_apps: Sequence[AppRef] = ()) -> Optional[FastPathResult]:
    """Text-only: does the CC-1a grammar recognise this command form? (No observation, no OS access.)"""
    ci = ChooserInput(obs_id="text-only", utterance=utterance[:2000] or " ",
                      app=AppRef(bundle_id="com.evie-assistant.none"), step=1, budget_remaining=0)
    result = fast_path(ci, installed_apps=installed_apps)
    return None if result.kind == "NO_MATCH" else result


# Router capture policy (conservative, docs/07 Section 37): the router hands a message to computer control only
# when it is unambiguously a computer-control command. "open ..." stays with open_app / findings phrasing, and
# "write ..." / "enter ..." are common chat requests ("write a summary"), so only "type ..." reaches set_text.
_NOT_CAPTURED = re.compile(r"^\s*(open|write|enter)\b", re.I)


# Unambiguous command verbs: routed to computer control BEFORE intent parsing (so "type hello" is not a greeting).
_STRONG = re.compile(r"^\s*(type|scroll|click|tap|press|align|fully\s+justify|switch\s+to|switch\s+back\s+to|"
                     r"change\s+to|activate|bring|center|left[- ]align|right[- ]align|justify)\b", re.I)


def is_computer_control_command(utterance: str) -> bool:
    if not utterance or not utterance.strip() or _NOT_CAPTURED.match(utterance):
        return False
    return classify(utterance) is not None


def is_strong_computer_control_command(utterance: str) -> bool:
    return is_computer_control_command(utterance) and bool(_STRONG.match(utterance))


def reply_for(summary: Dict[str, Any]) -> str:
    """A short, content-free reply: never echoes typed text or screen content."""
    status = summary.get("status")
    if status != "SESSION_ENDED":
        return {"STOPPED": "Stopped.", "NOTHING_TO_STOP": "Nothing is running.", "BUSY": "Another task is running.",
                "IDENTITY_NOT_VERIFIED": "Computer control isn't available in this runtime.",
                "UNAVAILABLE": "Computer control isn't available here.",
                "OBSERVE_FAILED": "I couldn't read the screen, so I did nothing."}.get(status, "I did nothing.")
    state = summary.get("final_state")
    if state == "DONE_VERIFIED" or (state == "DONE_UNVERIFIED" and summary.get("actions")):
        return "Done."
    if state == "ASKING":
        return "I need more detail before I do that."
    if state == "CANCELLED":
        return "Stopped."
    reason = summary.get("stop_reason") or ""
    for code, text in _BLOCKED_REPLIES:
        if code in reason:
            return text
    return "I didn't do that."


_ASK_QUESTIONS = {
    "UNRESOLVED_REFERENCE": "Which one do you mean?",
    "AMBIGUOUS_TARGET": "More than one control matches. Which one do you mean?",
    "AMBIGUOUS_APP": "More than one app has that name. Which one do you mean?",
    "AMBIGUOUS_CLOSE_TARGET": "Should I close the window or quit the app?",
    "APP_REQUIRED": "Which app do you mean?",
    "UNKNOWN_APP": "I don't know that app. Which app do you mean?",
    "TEXT_REQUIRED": "What should I type?",
    "DIRECTION_REQUIRED": "Should I scroll up or down?",
    "SCROLL_FORM_NOT_UNDERSTOOD": "Should I scroll up or down?",
    "USER_TAKEOVER": "The active app changed. Should I continue?",
    "CONFIRMATION_REQUIRED": "Are you sure?",
}


def question_for(ask_reason: Optional[str]) -> str:
    """A spoken clarification for an ASK reason code (content-free)."""
    return _ASK_QUESTIONS.get(ask_reason or "", "I need more detail before I do that.")


# Content-free explanations keyed by reason codes (never screen text, labels or typed text).
_BLOCKED_REPLIES = (
    ("ALREADY_FRONTMOST", "That app is already in front."),
    ("CONTROL_NOT_FOUND", "I can't see a control with that name, so I did nothing."),
    ("CONTROL_DISABLED", "That control is disabled, so I did nothing."),
    ("DESTRUCTIVE_LABEL", "That looks destructive, so I won't click it."),
    ("NO_TEXT_SELECTED", "Nothing is selected, so there was nothing to copy."),
    ("CLIPBOARD_CONCEALED", "The clipboard holds a protected item, so I won't paste it."),
    ("CLIPBOARD_HAS_NO_TEXT", "The clipboard has no text to paste."),
    ("SECURE_FIELD", "I don't act on password fields."),
    ("EVIE_SELF", "I won't act on myself."),
    ("OBSERVATION_DEGRADED", "I couldn't read that window completely, so I did nothing."),
    ("NOT_MAIN_WINDOW", "That isn't in the main window, so I did nothing."),
    ("USER_TAKEOVER", "The active app changed, so I stopped."),
)


class _Clock:
    def now(self) -> float:
        return time.monotonic()


class ComputerControlRuntime:
    """Host-provided runtime: the worker (running under the HOST process identity) and the audit log."""

    def __init__(self, worker, step_log, *, checkpoint: Optional[Callable[[str, ComputerControlSession], None]] = None,
                 clock=None):
        self.worker, self.step_log, self.extra_checkpoint = worker, step_log, checkpoint
        self.clock = clock or _Clock()
        self.slot = ActiveSessionSlot()
        self.history: List[Dict[str, Any]] = []

    def identity(self) -> Tuple[bool, List[str]]:
        status = self.worker.probe()
        p = status.probe
        resp = p.identity.responsible if p else None
        found = bool(resp and resp.bundle_lookup and resp.bundle_lookup.status == "FOUND")
        ok = bool(p and p.production_identity_verified and found)
        return ok, (list(p.production_identity_reasons) if p else ["NO_PROBE"]) + ([] if found else ["LOOKUP_NOT_FOUND"])

    def run(self, utterance: str) -> Dict[str, Any]:
        started = time.monotonic()
        installed = []
        if hasattr(self.worker, "installed_apps"):
            try:
                raw_installed = self.worker.installed_apps()
                installed = [AppRef(bundle_id=a.bundle_id, name=a.name) for a in raw_installed]
            except Exception:
                installed = []
        fp = classify(utterance, installed_apps=installed)
        if fp is None:
            return self._done({"status": "NOT_A_COMMAND"}, started)
        if fp.kind == "STOP":
            current = self.slot.current
            if current is not None and not current.is_terminal:
                current.cancel("USER_STOP")
                return self._done({"status": "STOPPED", "session_id": current.session_id}, started)
            return self._done({"status": "NOTHING_TO_STOP"}, started)
        ok, reasons = self.identity()
        if not ok:                                              # no session starts without signed Evie identity
            return self._done({"status": "IDENTITY_NOT_VERIFIED", "reasons": reasons}, started)
        # App-level commands name their app; they never act inside the frontmost app's windows, so they observe
        # Finder (always running) instead of a possibly huge or degraded frontmost app.
        activation = fp.rule in ("activate", "quit")
        pre = self.worker.observe(bundle_id=FINDER) if activation else self.worker.observe(frontmost=True)
        if pre.status != "OK" or pre.observation is None:
            return self._done({"status": "OBSERVE_FAILED", "reason": f"{pre.status}:{pre.reason}"}, started)
        obs0 = pre.observation
        observe_bundle = FINDER if activation else obs0.app.bundle_id
        allowed = derive_allowed_apps(utterance, obs0.frontmost, obs0.running_apps, installed_apps=installed) or frozenset({obs0.app.bundle_id})
        session = ComputerControlSession(f"cc-{uuid.uuid4().hex[:12]}", utterance, allowed, clock=self.clock, installed_apps=installed)
        try:
            self.slot.claim(session)                            # one active session; never preempts
        except SessionConflict:
            return self._done({"status": "BUSY"}, started)
        try:
            session.start(self.worker.status.probe.to_permission_status())

            def checkpoint(where: str) -> None:
                if where == "before_execute" and not session.is_terminal and not self.identity()[0]:
                    session.cancel("IDENTITY_NOT_VERIFIED")
                if self.extra_checkpoint is not None:
                    self.extra_checkpoint(where, session)

            orchestrator = Orchestrator(session, lambda bundle: self.worker.observe(bundle_id=bundle), self._act,
                                        checkpoint=checkpoint, step_log=self.step_log)
            chooser = FastPathChooser()
            # Wrap chooser to pass installed_apps
            class _InstalledChooser:
                def choose(self, u, o, recent=(), step=1, allowed_apps=()):
                    return chooser.choose(u, o, recent=recent, step=step, allowed_apps=allowed_apps, installed_apps=installed)

            result = orchestrator.run_command(_InstalledChooser(), observe_bundle)
        finally:
            self.slot.release(session)
        return self._done({
            "status": "SESSION_ENDED", "session_id": session.session_id, "final_state": result.final_state.value,
            "stop_reason": result.stop_reason, "ask_reason": result.ask_reason,
            "actions": [{"op": s.op.value, "status": s.status.value if s.status else None,
                         "verification": s.verification, "reason": s.reason, "gate": s.gate,
                         **{k: s.evidence[k] for k in SUMMARY_EVIDENCE if k in s.evidence}} for s in result.steps],
            "audit_failures": orchestrator.audit_failures}, started)

    def _act(self, request, utterance, allowed_apps):
        return self.worker.act(request, utterance=utterance, allowed_apps=allowed_apps)

    def _done(self, summary: Dict[str, Any], started: float) -> Dict[str, Any]:
        summary["seconds"] = round(time.monotonic() - started, 3)
        self.history.append(summary)
        return summary


_RUNTIME: Optional[ComputerControlRuntime] = None


def set_runtime(runtime: Optional[ComputerControlRuntime]) -> None:
    global _RUNTIME
    _RUNTIME = runtime
    from services.computer_control.container import set_container
    if runtime is None:
        set_container(None)


def get_runtime() -> Optional[ComputerControlRuntime]:
    return _RUNTIME


def handle(command: str) -> Dict[str, Any]:
    runtime = get_runtime()
    if runtime is None:
        return {"status": "UNAVAILABLE", "reason": "NO_COMPUTER_CONTROL_RUNTIME"}
    return runtime.run(command)
