"""
Dedicated local computer-control worker process (docs/07_Computer_Control.md Sections 4 and 14).

Threading/process decision (as built, CC-1a Step 2):
- AppKit/Accessibility live only in a separate Python process started as
  `python -m services.computer_control.worker` (fixed argv, shell=False, no user input). All
  PyObjC work happens on that process's MAIN thread - the conditions every Phase 0b validation
  ran under. The FastAPI process never imports AppKit and never holds AX objects.
- multiprocessing was not used: its macOS "spawn" start method re-imports the parent's __main__
  (main.py, the FastAPI app) in the child. A plain `-m` child starts clean.
- The parent side uses no threads: it reads the child's stdout with select() and a timeout.

Minimal protocol (JSON lines; unknown fields rejected):
  child -> parent: {"type": "hello", "protocol": 1, "pid": N}
                   {"type": "probe_result", "result": RuntimeProbeResult}
                   {"type": "observation", "result": ObserveResult}          (CC-1a Step 3, read-only)
                   {"type": "action_result", "result": ActionResult}         (CC-1a Step 4)
                   {"type": "stopped"} | {"type": "error", "error": "..."}
  parent -> child: {"type": "probe"} | {"type": "observe", ...scope} |
                   {"type": "act", "request": ActionRequest, "utterance": str, "allowed_apps": [...]} |
                   {"type": "stop"}
`act` takes only a closed, gate-approved ActionRequest (no raw AX operations exist in the protocol);
the worker re-checks freshness and the gate itself and executes only the validated CC-1a primitives
(executor.py). `observe` and `act` are refused unless the worker's own latest runtime probe is ready.
"""

import json
import os
import select
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Union

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from services.computer_control.models import (
    BUNDLE_ID_PATTERN, ActionRequest, ActionResult, ActionStatus, AppIdentity, ExecutionStatus, VerificationStatus, _Strict,
)
from services.computer_control.observer import ObserveResult
from services.computer_control.runtime import RuntimeProbeResult

PROTOCOL_VERSION = 1
WORKER_MODULE = "services.computer_control.worker"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_LINE_BYTES = 1024 * 1024        # bounded; a large app's observation (150 targets + semantic graph) fits


# --- protocol messages (closed) ---

class Hello(_Strict):
    type: Literal["hello"] = "hello"
    protocol: Literal[1] = PROTOCOL_VERSION
    pid: int = Field(..., ge=1)


class ProbeResultMsg(_Strict):
    type: Literal["probe_result"] = "probe_result"
    result: RuntimeProbeResult


class ObservationMsg(_Strict):
    type: Literal["observation"] = "observation"
    result: ObserveResult


class ActionResultMsg(_Strict):
    type: Literal["action_result"] = "action_result"
    result: ActionResult


class InstalledAppsResultMsg(_Strict):
    type: Literal["installed_apps_result"] = "installed_apps_result"
    apps: List[AppIdentity] = Field(default_factory=list)


class Stopped(_Strict):
    type: Literal["stopped"] = "stopped"


class WorkerActionError(RuntimeError):
    """The worker reported an exception (by type name only) while running an action; the outcome is unknown."""
    def __init__(self, worker_error: str):
        super().__init__(f"worker error during an action: {worker_error}; outcome unknown")
        self.worker_error = worker_error


class WorkerErrorMsg(_Strict):
    type: Literal["error"] = "error"
    error: str = Field(..., max_length=200)


class ProbeCommand(_Strict):
    type: Literal["probe"] = "probe"


class InstalledAppsCommand(_Strict):
    type: Literal["installed_apps"] = "installed_apps"


class ObserveCommand(_Strict):
    """Read-only observation of ONE already-running app: exactly one scope field."""
    type: Literal["observe"] = "observe"
    bundle_id: Optional[str] = Field(None, pattern=BUNDLE_ID_PATTERN)
    pid: Optional[int] = Field(None, ge=1)
    frontmost: bool = False
    settle: bool = True

    @model_validator(mode="after")
    def _one_scope(self):
        if sum((self.bundle_id is not None, self.pid is not None, self.frontmost)) != 1:
            raise ValueError("exactly one of bundle_id, pid or frontmost")
        return self


class ActCommand(_Strict):
    """One closed, gate-approved action. Text for set_text is derived in the worker from `utterance`."""
    type: Literal["act"] = "act"
    request: ActionRequest
    utterance: str = Field(..., min_length=1, max_length=2000)
    allowed_apps: List[str] = Field(..., min_length=1, max_length=50)


class StopCommand(_Strict):
    type: Literal["stop"] = "stop"


_FROM_WORKER = TypeAdapter(Union[Hello, ProbeResultMsg, ObservationMsg, ActionResultMsg, InstalledAppsResultMsg, Stopped, WorkerErrorMsg])
_TO_WORKER = TypeAdapter(Union[ProbeCommand, InstalledAppsCommand, ObserveCommand, ActCommand, StopCommand])


# --- parent side ---

class WorkerState(str, Enum):
    NEW = "NEW"
    STARTING = "STARTING"
    READY = "READY"            # started and the runtime probe says ready
    NOT_READY = "NOT_READY"    # started, but fail-closed: probe says not ready
    FAILED = "FAILED"          # could not start / protocol failure / died
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class WorkerStatus(_Strict):
    state: WorkerState
    pid: Optional[int] = None
    probe: Optional[RuntimeProbeResult] = None
    failure: Optional[str] = Field(None, max_length=200)
    exit_code: Optional[int] = None

    @property
    def ready(self) -> bool:
        return self.state is WorkerState.READY and self.probe is not None and self.probe.ready


class WorkerHandle(Protocol):
    pid: int

    def send(self, message: Dict[str, Any]) -> None: ...
    def receive(self, timeout_s: float) -> Optional[Dict[str, Any]]: ...   # None = timed out
    def poll(self) -> Optional[int]: ...
    def wait(self, timeout_s: float) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class WorkerExited(RuntimeError):
    pass


class _PopenHandle:
    """The real worker process. Fixed argv, no shell; stderr discarded so it cannot block the pipe."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(PROJECT_ROOT), shell=False, close_fds=True,
        )
        self.pid = self.proc.pid
        self._buf = b""

    def send(self, message: Dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def receive(self, timeout_s: float) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout_s
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                raise WorkerExited("worker closed its output")
            self._buf += chunk
            if len(self._buf) > MAX_LINE_BYTES:
                raise ValueError("worker message too large")
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def wait(self, timeout_s: float) -> Optional[int]:
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> None:
        self.proc.terminate()

    def kill(self) -> None:
        self.proc.kill()


def _protocol_error(e: Exception) -> str:
    """PROTOCOL_ERROR plus the first validation message (no message payload, bounded) for diagnosis."""
    detail = str(e)
    if isinstance(e, ValidationError) and e.errors():
        errors = e.errors()                  # a union reports every branch; skip the "wrong type tag" noise
        mismatched = {x["loc"][0] for x in errors if x.get("type") == "literal_error" and x.get("loc")}
        real = [x for x in errors if x.get("loc") and x["loc"][0] not in mismatched] or errors
        detail = f"{'.'.join(str(p) for p in real[0].get('loc', ()))}: {real[0].get('msg', '')}"
    return f"PROTOCOL_ERROR:{type(e).__name__}:{detail}"[:200]


class ComputerControlWorker:
    """Lifecycle of the one worker process: start -> READY / NOT_READY / FAILED -> stop -> STOPPED."""

    def __init__(self, *, launcher: Callable[[], WorkerHandle] = _PopenHandle, start_timeout_s: float = 20.0,
                 probe_timeout_s: float = 20.0, stop_timeout_s: float = 5.0, observe_timeout_s: float = 60.0,
                 act_timeout_s: float = 60.0):
        self._launcher = launcher
        self.start_timeout_s = start_timeout_s
        self.probe_timeout_s = probe_timeout_s
        self.observe_timeout_s = observe_timeout_s   # > settle budget (20 s) + walk budget (15 s)
        self.act_timeout_s = act_timeout_s           # action + settle + the post-action re-observation
        self.stop_timeout_s = stop_timeout_s
        self._handle: Optional[WorkerHandle] = None
        self._status = WorkerStatus(state=WorkerState.NEW)

    @property
    def status(self) -> WorkerStatus:
        return self._status

    def _set(self, state: WorkerState, **fields) -> WorkerStatus:
        self._status = self._status.model_copy(update={"state": state, **fields})
        return self._status

    def _fail(self, reason: str) -> WorkerStatus:
        status = self._set(WorkerState.FAILED, failure=reason[:200])
        self._reap()
        return status

    def _receive(self, timeout_s: float):
        raw = self._handle.receive(timeout_s)
        if raw is None:
            return None
        return _FROM_WORKER.validate_python(raw)

    def _await_probe(self, timeout_s: float) -> WorkerStatus:
        try:
            msg = self._receive(timeout_s)
        except WorkerExited:
            return self._fail("WORKER_EXITED")
        except (ValidationError, ValueError) as e:
            return self._fail(_protocol_error(e))
        if msg is None:
            return self._fail("PROBE_TIMEOUT")
        if isinstance(msg, WorkerErrorMsg):
            return self._fail(f"WORKER_ERROR:{msg.error}")
        if not isinstance(msg, ProbeResultMsg):
            return self._fail("PROTOCOL_ERROR")
        if msg.result.identity.worker.pid != self._handle.pid:
            return self._fail("PROBE_IDENTITY_MISMATCH")     # the result must describe THIS worker process
        return self._set(WorkerState.READY if msg.result.ready else WorkerState.NOT_READY, probe=msg.result,
                         failure=None)

    def start(self) -> WorkerStatus:
        if self._status.state is not WorkerState.NEW:
            raise RuntimeError(f"worker already used (state {self._status.state.value}); create a new one")
        self._set(WorkerState.STARTING)
        try:
            self._handle = self._launcher()
        except OSError as e:
            return self._set(WorkerState.FAILED, failure=f"LAUNCH_FAILED:{type(e).__name__}")
        self._set(WorkerState.STARTING, pid=self._handle.pid)
        try:
            hello = self._receive(self.start_timeout_s)
        except WorkerExited:
            return self._fail("WORKER_EXITED")
        except (ValidationError, ValueError) as e:
            return self._fail(_protocol_error(e))
        if hello is None:
            return self._fail("START_TIMEOUT")
        if not isinstance(hello, Hello) or hello.pid != self._handle.pid:
            return self._fail("PROTOCOL_ERROR")
        return self._await_probe(self.probe_timeout_s)

    def probe(self) -> WorkerStatus:
        """Re-run the runtime probe in the worker (e.g. before a session starts)."""
        if self._status.state not in (WorkerState.READY, WorkerState.NOT_READY):
            raise RuntimeError(f"cannot probe in state {self._status.state.value}")
        try:
            self._handle.send(_TO_WORKER.dump_python(ProbeCommand()))
        except (OSError, ValueError):
            return self._fail("WORKER_EXITED")
        return self._await_probe(self.probe_timeout_s)

    def observe(self, *, bundle_id: Optional[str] = None, pid: Optional[int] = None, frontmost: bool = False,
                settle: bool = True) -> ObserveResult:
        """Read-only observation through the worker. Refused (fail-closed) unless the worker is READY."""
        command = ObserveCommand(bundle_id=bundle_id, pid=pid, frontmost=frontmost, settle=settle)
        if self._status.state is not WorkerState.READY:
            return ObserveResult(status="NOT_READY", reason=f"WORKER_{self._status.state.value}")
        try:
            self._handle.send(_TO_WORKER.dump_python(command))
            msg = self._receive(self.observe_timeout_s)
        except (OSError, WorkerExited):
            self._fail("WORKER_EXITED")
            return ObserveResult(status="ERROR", reason="WORKER_EXITED")
        except (ValidationError, ValueError):
            self._fail("PROTOCOL_ERROR")
            return ObserveResult(status="ERROR", reason="PROTOCOL_ERROR")
        if msg is None:
            self._fail("OBSERVE_TIMEOUT")
            return ObserveResult(status="ERROR", reason="OBSERVE_TIMEOUT")
        if isinstance(msg, WorkerErrorMsg):
            return ObserveResult(status="ERROR", reason=msg.error)
        if not isinstance(msg, ObservationMsg):
            self._fail("PROTOCOL_ERROR")
            return ObserveResult(status="ERROR", reason="PROTOCOL_ERROR")
        return msg.result

    def installed_apps(self) -> List[AppIdentity]:
        """Fetch installed macOS application metadata through the worker."""
        if self._status.state not in (WorkerState.READY, WorkerState.NOT_READY):
            return []
        if self._handle is None:
            return []
        try:
            self._handle.send(_TO_WORKER.dump_python(InstalledAppsCommand()))
            msg = self._receive(self.observe_timeout_s)
            if isinstance(msg, InstalledAppsResultMsg):
                return msg.apps
        except Exception:
            pass
        return []

    def act(self, request: ActionRequest, *, utterance: str, allowed_apps) -> ActionResult:
        """Send ONE closed action. Refused (not executed) unless the worker is READY. Never retried."""
        def refused(reason: str) -> ActionResult:
            return ActionResult(session_id=request.session_id, step=request.step, op=request.op,
                                status=ActionStatus.BLOCKED, execution=ExecutionStatus.NOT_EXECUTED,
                                verification=VerificationStatus.NOT_APPLICABLE, requested_at=time.time(),
                                reason=reason)
        if self._status.state is not WorkerState.READY:
            return refused(f"WORKER_{self._status.state.value}")
        command = ActCommand(request=request, utterance=utterance, allowed_apps=sorted(allowed_apps))
        try:
            self._handle.send(json.loads(command.model_dump_json()))
            msg = self._receive(self.act_timeout_s)
        except (OSError, WorkerExited):
            self._fail("WORKER_EXITED")
            raise RuntimeError("worker exited during an action; outcome unknown") from None
        except (ValidationError, ValueError):
            self._fail("PROTOCOL_ERROR")
            raise RuntimeError("protocol error during an action; outcome unknown") from None
        if msg is None:
            self._fail("ACT_TIMEOUT")
            raise TimeoutError("no action result; outcome unknown (the action may have run)")
        if isinstance(msg, WorkerErrorMsg):
            self._fail(f"WORKER_ERROR:{msg.error}")
            raise WorkerActionError(msg.error)
        if not isinstance(msg, ActionResultMsg) or msg.result.step != request.step:
            self._fail("PROTOCOL_ERROR")
            raise RuntimeError("unexpected reply to an action; outcome unknown")
        return msg.result

    def stop(self) -> WorkerStatus:
        if self._status.state in (WorkerState.NEW, WorkerState.STOPPED):
            return self._set(WorkerState.STOPPED)
        if self._handle is None or self._status.state is WorkerState.FAILED:
            self._reap()
            return self._set(WorkerState.STOPPED)
        self._set(WorkerState.STOPPING)
        clean = False
        try:
            self._handle.send(_TO_WORKER.dump_python(StopCommand()))
            msg = self._receive(self.stop_timeout_s)
            clean = isinstance(msg, Stopped)
        except (OSError, ValueError, ValidationError, WorkerExited):
            clean = False
        code = self._handle.wait(self.stop_timeout_s)
        if code is None:
            self._reap()
            code = self._handle.poll()
            clean = False
        return self._set(WorkerState.STOPPED, exit_code=code,
                         failure=None if clean and code == 0 else "UNCLEAN_SHUTDOWN")

    def _reap(self) -> None:
        if self._handle is None or self._handle.poll() is not None:
            return
        self._handle.terminate()
        if self._handle.wait(self.stop_timeout_s) is None:
            self._handle.kill()
            self._handle.wait(self.stop_timeout_s)

    def __enter__(self) -> "ComputerControlWorker":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


# --- child side: `python -m services.computer_control.worker` ---

def _run_probe() -> RuntimeProbeResult:
    from services.computer_control.macos_probe import probe_runtime   # PyObjC only ever loads here
    return probe_runtime(worker_started=True)


_RUNTIME = None


def _runtime():
    """One runtime per worker process: it owns the latest observation and its live handles."""
    global _RUNTIME
    if _RUNTIME is None:
        from services.computer_control.macos_actions import WorkerRuntime   # PyObjC only ever loads here
        _RUNTIME = WorkerRuntime()
    return _RUNTIME


def _run_observe(command: "ObserveCommand") -> ObserveResult:
    return _runtime().observe(bundle_id=command.bundle_id, pid=command.pid, frontmost=command.frontmost,
                              settle=command.settle)


def _run_act(command: "ActCommand") -> ActionResult:
    return _runtime().act(command.request, command.utterance, command.allowed_apps)


def serve(stdin=None, stdout=None, probe: Callable[[], RuntimeProbeResult] = _run_probe,
          observe: Callable[[ObserveCommand], ObserveResult] = _run_observe,
          act: Callable[[ActCommand], ActionResult] = _run_act) -> int:
    """Worker main loop, run on the process main thread. Returns the exit code."""
    stdin = stdin or sys.stdin
    out = stdout or sys.stdout

    def emit(message: _Strict) -> None:
        out.write(message.model_dump_json() + "\n")
        out.flush()

    emit(Hello(pid=os.getpid()))
    latest = probe()
    emit(ProbeResultMsg(result=latest))
    for line in stdin:
        try:
            command = _TO_WORKER.validate_json(line)
        except ValidationError:
            emit(WorkerErrorMsg(error="UNKNOWN_COMMAND"))
            continue
        if isinstance(command, StopCommand):
            emit(Stopped())
            return 0
        if isinstance(command, InstalledAppsCommand):
            if hasattr(_runtime(), "installed_apps"):
                emit(InstalledAppsResultMsg(apps=_runtime().installed_apps()))
            else:
                emit(InstalledAppsResultMsg(apps=[]))
            continue
        if isinstance(command, ObserveCommand):
            if not latest.ready:                                  # fail closed on this process's own probe
                emit(ObservationMsg(result=ObserveResult(status="NOT_READY", reason="RUNTIME_PROBE_NOT_READY")))
            else:
                emit(ObservationMsg(result=observe(command)))
            continue
        if isinstance(command, ActCommand):
            if not latest.ready:
                r = command.request
                emit(ActionResultMsg(result=ActionResult(
                    session_id=r.session_id, step=r.step, op=r.op, status=ActionStatus.BLOCKED,
                    execution=ExecutionStatus.NOT_EXECUTED, verification=VerificationStatus.NOT_APPLICABLE,
                    requested_at=time.time(), reason="RUNTIME_PROBE_NOT_READY")))
            else:
                emit(ActionResultMsg(result=act(command)))
            continue
        latest = probe()
        emit(ProbeResultMsg(result=latest))
    return 0   # parent closed stdin: exit quietly


def main() -> int:
    protocol_out = sys.stdout
    sys.stdout = sys.stderr   # nothing but protocol lines may reach the parent's pipe
    try:
        return serve(sys.stdin, protocol_out)
    except Exception as e:     # report, then exit non-zero: the parent fails closed
        protocol_out.write(WorkerErrorMsg(error=f"{type(e).__name__}"[:200]).model_dump_json() + "\n")
        protocol_out.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
