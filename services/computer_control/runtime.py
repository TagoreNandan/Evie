"""
Runtime identity and permission-probe results (docs/07_Computer_Control.md Section 14).

Pure data only: no AppKit, PyObjC or ctypes here. The live values are produced inside the
computer-control worker process by macos_probe.py and cross the process boundary as JSON.

Readiness is fail-closed: `ready` is computed, never supplied, and is true only when the
worker started, its own identity (including the responsible app that macOS attributes its
permissions to) is known, Accessibility trust is confirmed for THAT process, and a harmless
functional AX read succeeded. A trust result measured elsewhere (e.g. the Phase 0b scratch
runs under Antigravity IDE) is never reused.
"""

from enum import Enum
from pathlib import Path
from typing import Literal, Optional, Tuple

from pydantic import Field, computed_field, model_validator

from services.computer_control.models import PermissionStatus, _Strict

# Apps that commonly host a development runtime. Used only to label a *known* responsible
# bundle id; bundle ids themselves are never inferred.
DEVELOPMENT_HOST_BUNDLES = frozenset({
    "com.google.antigravity-ide", "com.microsoft.VSCode", "com.todesktop.230313mzl4w4u92",   # Cursor
    "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "com.mitchellh.ghostty",
    "com.jetbrains.pycharm", "com.jetbrains.pycharm.ce", "com.apple.dt.Xcode",
})

PRODUCTION_IDENTITY_NOTE = (
    "Production Evie launch identity is not defined yet (docs/07 Sections 21.7 and 33); this result "
    "verifies only the worker runtime it was measured in."
)

# Generic interpreters/shells: never an Evie identity by themselves, wherever they are installed.
GENERIC_EXECUTABLE_NAMES = frozenset({"python", "python3", "zsh", "bash", "sh", "node", "electron", "uvicorn"})


class SignatureInfo(_Strict):
    """Code signature of a RUNNING process, from Security.framework (macos_signature.py). Never inferred."""
    checked: bool                                              # False = no check was possible (then nothing is known)
    valid: bool = False                                        # intact AND anchored in an Apple-issued certificate
    status: Optional[int] = None                               # OSStatus of the validity check
    identifier: Optional[str] = Field(None, max_length=255)    # signing identifier
    team_id: Optional[str] = Field(None, max_length=32)        # Team ID from the signing certificate
    error: Optional[str] = Field(None, max_length=200)


BundleLookupStatus = Literal[
    "FOUND",                   # NSRunningApplication object with a bundle identifier
    "PROCESS_NOT_FOUND",       # no such process (libproc knows no executable for the pid)
    "NO_RUNNING_APPLICATION",  # the process exists, but NSRunningApplication returned no object for it
    "BUNDLE_ID_ABSENT",        # an NSRunningApplication object exists, but bundleIdentifier() is empty
    "LOOKUP_EXCEPTION",        # the lookup raised (type + sanitised message recorded)
]


class BundleLookup(_Strict):
    """How a process's bundle id was (not) obtained - diagnostics only; never a substitute for the id itself."""
    status: BundleLookupStatus
    error_type: Optional[str] = Field(None, max_length=64)
    error_message: Optional[str] = Field(None, max_length=120)


class ProcessInfo(_Strict):
    pid: int = Field(..., ge=0)
    name: Optional[str] = Field(None, max_length=256)          # None = unknown
    executable: Optional[str] = Field(None, max_length=1024)
    bundle_id: Optional[str] = Field(None, max_length=255)     # only from a real OS lookup
    signature: Optional[SignatureInfo] = None                  # only from a real signature check (CC-1a Step 13)
    bundle_lookup: Optional[BundleLookup] = None               # why bundle_id is (not) known (Step 14B debug-1)


def _drop(data, *names):
    """Computed fields are recomputed on parse; a supplied value (e.g. a forged "ready": true) is ignored."""
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k not in names}
    return data


class RuntimeIdentity(_Strict):
    worker: ProcessInfo
    parent: Optional[ProcessInfo] = None
    responsible: Optional[ProcessInfo] = None                  # the app macOS attributes TCC permissions to
    ancestry: Tuple[ProcessInfo, ...] = Field(default=(), max_length=16)   # parent, grandparent, ...
    responsible_lookup: str = Field("unknown", max_length=64)  # how `responsible` was obtained, or why not

    @model_validator(mode="before")
    @classmethod
    def _ignore_computed(cls, data):
        return _drop(data, "sufficient", "responsible_is_development_host")

    @computed_field
    @property
    def sufficient(self) -> bool:
        """Enough to say whose permissions were measured: the worker and its responsible app."""
        return self.worker.executable is not None and self.responsible is not None

    @computed_field
    @property
    def responsible_is_development_host(self) -> Optional[bool]:
        if self.responsible is None or self.responsible.bundle_id is None:
            return None
        return self.responsible.bundle_id in DEVELOPMENT_HOST_BUNDLES


class ProductionIdentityContract(_Strict):
    """
    What the Evie runtime identity IS (docs/07 Sections 33-34). Every field must match independently measured
    evidence: the responsible process is the signed Evie launcher, and the worker is the signed interpreter
    inside the same bundle, started under it.
    """
    bundle_id: str = Field(..., min_length=3, max_length=255)          # CFBundleIdentifier == launcher signing id
    bundle_path: str = Field(..., pattern=r"^/.+\.app/$", max_length=1024)   # installed bundle, trailing slash
    team_id: str = Field(..., pattern=r"^[A-Z0-9]{10}$")               # Team ID of the Apple-issued certificate
    launcher_executable: str = Field("Contents/MacOS/Evie", pattern=r"^Contents/MacOS/[A-Za-z0-9._-]+$")
    worker_executable: str = Field("Contents/MacOS/evie-python", pattern=r"^Contents/MacOS/[A-Za-z0-9._-]+$")
    worker_identifier: str = Field(..., min_length=3, max_length=255)  # signing identifier of the worker binary


# The Evie identity app built by packaging/macos/build_evie_app.sh (docs/07 Section 34). Team ZB8SA28XVY is
# the local APPLE DEVELOPMENT certificate available on the development Mac - a development identity, not
# Developer ID / notarized distribution. Installed per user in ~/Applications.
PRODUCTION_IDENTITY_CONTRACT: Optional[ProductionIdentityContract] = ProductionIdentityContract(
    bundle_id="com.evie-assistant.evie",
    bundle_path=f"{Path.home()}/Applications/Evie.app/",
    team_id="ZB8SA28XVY",
    worker_identifier="com.evie-assistant.evie.python",
)


class ProductionIdentityAssessment(_Strict):
    verified: bool
    reasons: Tuple[str, ...] = Field(default=(), max_length=16)        # every unmet condition (empty iff verified)


def _generic(executable: Optional[str]) -> bool:
    base = (executable or "").rsplit("/", 1)[-1].lower()
    return not base or base in GENERIC_EXECUTABLE_NAMES or base.startswith("python")


_LOOKUP_REASONS = {
    "PROCESS_NOT_FOUND": "RESPONSIBLE_PROCESS_NOT_FOUND",
    "NO_RUNNING_APPLICATION": "RESPONSIBLE_NO_RUNNING_APPLICATION",
    "BUNDLE_ID_ABSENT": "RESPONSIBLE_BUNDLE_ID_ABSENT",
    "LOOKUP_EXCEPTION": "RESPONSIBLE_BUNDLE_LOOKUP_EXCEPTION",
}


def _signed_by(p: ProcessInfo, identifier: str, team_id: str, label: str, reasons: list) -> None:
    sig = p.signature
    if sig is None or not sig.checked:
        reasons.append(f"{label}_SIGNATURE_NOT_CHECKED")
    elif not sig.valid:
        reasons.append(f"{label}_SIGNATURE_INVALID")
    else:
        if sig.team_id != team_id:
            reasons.append(f"{label}_TEAM_ID_MISMATCH")
        if sig.identifier != identifier:
            reasons.append(f"{label}_SIGNING_IDENTIFIER_MISMATCH")


def assess_production_identity(result: "RuntimeProbeResult",
                               contract: Optional[ProductionIdentityContract]) -> ProductionIdentityAssessment:
    """
    Fail-closed rule. Verified ONLY if every condition holds:
      1. a production identity contract is declared;
      2. the worker runtime probe is ready (identity known, AX TRUSTED for this process, functional read OK);
      3. the responsible process (whose TCC grant macOS uses) is the contract's launcher: exact executable path
         inside the bundle, bundle id from the OS, and a valid Apple-anchored signature with the contract's
         signing identifier and Team ID;
      4. the worker belongs to that identity: its executable is exactly the contract's interpreter inside the
         bundle, validly signed with the same Team ID, and the responsible process is one of its ancestors
         (or the worker itself);
      5. no development host (IDE / terminal) is the responsible app or anywhere in the ancestry.
    """
    reasons = []
    ident = result.identity
    if contract is None:
        reasons.append("NO_PRODUCTION_IDENTITY_CONTRACT")
    if not result.ready:
        reasons.append("RUNTIME_PROBE_NOT_READY")
    if result.accessibility.status is not AccessibilityStatus.TRUSTED:
        reasons.append("ACCESSIBILITY_NOT_TRUSTED")
    if result.functional.status is not FunctionalStatus.OK:
        reasons.append("FUNCTIONAL_AX_READ_FAILED")
    resp, worker = ident.responsible, ident.worker
    if resp is None:
        reasons.append("RESPONSIBLE_PROCESS_UNKNOWN")
    elif ident.responsible_is_development_host:
        reasons.append("RESPONSIBLE_APP_IS_DEVELOPMENT_HOST")
    if any(p.bundle_id in DEVELOPMENT_HOST_BUNDLES for p in ident.ancestry if p.bundle_id):
        reasons.append("DEVELOPMENT_HOST_IN_ANCESTRY")
    if _generic(worker.executable) and (contract is None or not (worker.executable or "").startswith(
            contract.bundle_path)):
        reasons.append("WORKER_IS_GENERIC_INTERPRETER")
    if contract is not None:
        if resp is not None:
            if resp.bundle_id is None:
                reasons.append("RESPONSIBLE_BUNDLE_ID_UNKNOWN")                 # unchanged: always fail closed
                detail = _LOOKUP_REASONS.get(resp.bundle_lookup.status) if resp.bundle_lookup else None
                if detail:
                    reasons.append(detail)                                     # diagnostic only, never permissive
            elif resp.bundle_id != contract.bundle_id:
                reasons.append("RESPONSIBLE_BUNDLE_ID_MISMATCH")
            if resp.executable != contract.bundle_path + contract.launcher_executable:
                reasons.append("RESPONSIBLE_EXECUTABLE_MISMATCH")
            _signed_by(resp, contract.bundle_id, contract.team_id, "RESPONSIBLE", reasons)
            if resp.pid != worker.pid and resp.pid not in {p.pid for p in ident.ancestry}:
                reasons.append("WORKER_NOT_UNDER_RESPONSIBLE_PROCESS")
        if worker.executable != contract.bundle_path + contract.worker_executable:
            reasons.append("WORKER_EXECUTABLE_MISMATCH")
        _signed_by(worker, contract.worker_identifier, contract.team_id, "WORKER", reasons)
    reasons = tuple(dict.fromkeys(reasons))[:16]
    return ProductionIdentityAssessment(verified=not reasons, reasons=reasons)


class AccessibilityStatus(str, Enum):
    TRUSTED = "TRUSTED"
    NOT_TRUSTED = "NOT_TRUSTED"
    UNAVAILABLE = "UNAVAILABLE"      # probe could not run or errored


class FunctionalStatus(str, Enum):
    OK = "OK"
    FAILED = "FAILED"                # an AX read was attempted and did not succeed
    UNAVAILABLE = "UNAVAILABLE"      # no safe target running, PyObjC missing, or blocked under test


class AccessibilityProbe(_Strict):
    status: AccessibilityStatus
    error: Optional[str] = Field(None, max_length=200)


class FunctionalProbe(_Strict):
    status: FunctionalStatus
    target_bundle_id: Optional[str] = Field(None, max_length=255)
    target_pid: Optional[int] = Field(None, ge=0)
    app_role_ax_error: Optional[int] = None
    app_role: Optional[str] = Field(None, max_length=64)       # expected "AXApplication"
    window_role_ax_error: Optional[int] = None                 # AXFocusedWindow read (NoValue is normal)
    window_role: Optional[str] = Field(None, max_length=64)
    elapsed_ms: Optional[float] = Field(None, ge=0)
    error: Optional[str] = Field(None, max_length=200)


class RuntimeProbeResult(_Strict):
    probed_at: float = Field(..., ge=0)
    worker_started: bool
    identity: RuntimeIdentity
    accessibility: AccessibilityProbe
    functional: FunctionalProbe
    diagnostics: Tuple[str, ...] = Field(default=(), max_length=32)
    identity_scope: Literal["worker_runtime"] = "worker_runtime"

    @model_validator(mode="wrap")
    @classmethod
    def _recompute(cls, data, handler):
        """Computed fields are always recomputed from the evidence. A supplied production_identity_verified (the
        worker serialises it) is accepted only if it equals the recomputed value; any other claim is rejected."""
        claimed = data.get("production_identity_verified") if isinstance(data, dict) else None
        result = handler(_drop(data, "ready", "production_identity_verified", "production_identity_reasons"))
        if claimed is not None and claimed is not result.production_identity_verified:
            raise ValueError("production_identity_verified does not match the evidence")
        return result

    @computed_field
    @property
    def production_identity_verified(self) -> bool:
        """True only if assess_production_identity passes against the declared contract (None in CC-1a)."""
        return assess_production_identity(self, PRODUCTION_IDENTITY_CONTRACT).verified

    @computed_field
    @property
    def production_identity_reasons(self) -> Tuple[str, ...]:
        return assess_production_identity(self, PRODUCTION_IDENTITY_CONTRACT).reasons

    @computed_field
    @property
    def ready(self) -> bool:
        return (self.worker_started and self.identity.sufficient
                and self.accessibility.status is AccessibilityStatus.TRUSTED
                and self.functional.status is FunctionalStatus.OK)

    def to_permission_status(self) -> PermissionStatus:
        """What ComputerControlSession.start() consumes; ok == ready."""
        return PermissionStatus(
            ax_trusted=self.worker_started and self.identity.sufficient
            and self.accessibility.status is AccessibilityStatus.TRUSTED,
            ax_functional=self.functional.status is FunctionalStatus.OK,
            worker_pid=self.identity.worker.pid,
            responsible_bundle=self.identity.responsible.bundle_id if self.identity.responsible else None,
        )
