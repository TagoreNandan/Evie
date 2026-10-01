"""
Redacted per-attempt audit records for `computer_control_steps` (docs/04 Backend Schema Section 4; docs/07 Section 37).

Pure: builds one record per attempted decision. Observational only - it never influences what executes. Storage
is outside this package (services/computer_control_log.py). Never recorded: AX element references, screenshots,
typed text or text spans, document contents, secure-field anything, or labels of text-entry controls.
"""

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from services.computer_control.models import ActionRequest, ActionResult, AppIdentity, ObservedTarget, _Strict

DecisionSource = Literal["fast_path", "chooser", "plan"]
TEXT_ENTRY_ROLES = frozenset({"AXTextArea", "AXTextField", "AXSecureTextField", "AXComboBox", "AXSearchField"})
_SECRET_WORDS = ("password", "passcode", "token", "secret", "api key", "apikey", "credential", "private key")
_TOKEN_LIKE = re.compile(r"\S{24,}")
MAX_LABEL = 64


class StepAuditRecord(_Strict):
    """One row of computer_control_steps (column names and meanings from the Backend Schema)."""
    session_id: str = Field(..., max_length=64)
    step: int = Field(..., ge=1)
    op: Optional[str] = Field(None, max_length=32)             # NULL for DONE/ASK/BLOCKED/STOP decisions
    decision_source: DecisionSource
    target_summary: Optional[Dict[str, Any]] = None            # role, subrole, safe label, app bundle id (+ key)
    gate_decision: str = Field(..., max_length=120)
    status: Literal["SUCCESS", "FAILED", "BLOCKED", "STALE", "NOT_VERIFIABLE", "CANCELLED", "TIMEOUT"]
    execution: Optional[str] = None
    verification: Optional[str] = None
    mechanism: Optional[str] = Field(None, max_length=64)
    ax_error: Optional[int] = None
    latency_ms: Optional[float] = None
    state_layers: List[str] = Field(default_factory=list)
    tokens_in: Optional[int] = None                            # no model calls in CC-1a
    tokens_out: Optional[int] = None
    cost_usd: Optional[float] = None


def safe_label(target: Optional[ObservedTarget]) -> Optional[str]:
    """A control's label only if it cannot carry user content or secrets."""
    if target is None or target.label is None or target.is_secure or target.role in TEXT_ENTRY_ROLES:
        return None
    label = " ".join(target.label.split())[:MAX_LABEL]
    lowered = label.lower()
    if any(w in lowered for w in _SECRET_WORDS) or _TOKEN_LIKE.search(label):
        return "[REDACTED]"
    return label


def target_summary(target: Optional[ObservedTarget], app: Optional[AppIdentity],
                   key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if target is None and app is None and key is None:
        return None
    summary: Dict[str, Any] = {"app_bundle_id": app.bundle_id if app else None}
    if target is not None:
        summary.update(role=target.role, subrole=target.subrole, label=safe_label(target),
                       secure=target.is_secure or None)
    if key is not None:
        summary["key"] = key                                    # closed enum (RIGHT_ARROW / LEFT_ARROW) only
    return {k: v for k, v in summary.items() if v is not None}


def record_for_result(request: ActionRequest, result: ActionResult, source: DecisionSource, step: int,
                      app: Optional[AppIdentity]) -> StepAuditRecord:
    target = request.target.target if request.target is not None else None
    key = getattr(request.args, "key", None)
    activated = getattr(request.args, "bundle_id", None)
    summary_app = AppIdentity(bundle_id=activated, pid=0) if activated else app
    return StepAuditRecord(
        session_id=request.session_id, step=step, op=request.op.value, decision_source=source,
        target_summary=target_summary(target, summary_app, key),
        gate_decision=f"{request.gate.outcome.value}:{request.gate.risk.value}:{request.gate.reason}",
        status=result.status.value, execution=result.execution.value, verification=result.verification.value,
        mechanism=result.mechanism, ax_error=result.ax_error, latency_ms=result.latency_ms,
        state_layers=sorted(layer.value for layer in result.state_layers))


def record_for_decision(session_id: str, step: int, source: DecisionSource, op: Optional[str],
                        target: Optional[ObservedTarget], app: Optional[AppIdentity], state: str,
                        reason: Optional[str], key: Optional[str] = None) -> StepAuditRecord:
    """A decision that did not reach the worker: ASK / BLOCKED / DONE / STOP / refused by resolver or gate."""
    status = {"DONE_VERIFIED": "SUCCESS", "DONE_UNVERIFIED": "NOT_VERIFIABLE", "CANCELLED": "CANCELLED",
              "ERROR": "FAILED"}.get(state, "STALE" if reason and "STALE" in reason.upper() else "BLOCKED")
    return StepAuditRecord(
        session_id=session_id, step=step, op=op, decision_source=source,
        target_summary=target_summary(target, app, key),
        gate_decision=f"DECISION:{state}:{reason or ''}"[:120], status=status, execution="NOT_EXECUTED",
        verification="NOT_APPLICABLE")
