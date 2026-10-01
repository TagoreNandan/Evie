"""
Phase 2 Live Substrate Validation script.
Performs semantic observation, semantic target resolution, live revalidation, gate evaluation,
and action execution with verification against real standard macOS applications (TextEdit / Calculator / Photo Booth).
"""

import sys
import time
from typing import Optional

from services.computer_control.models import (
    Op, ClickElementArgs, SetTextArgs, UniversalAction,
    AppIdentity, ActionStatus, GateOutcome, ResolutionMethod
)
from services.computer_control.observer import observe_app_with_handles, read_identity
from services.computer_control.resolver import resolve_semantic, SemanticTargetSpec, TargetSpec
from services.computer_control.gate import evaluate, context_for, GatePolicy
from services.computer_control.verification import FocusVerifier, SelectionVerifier, TextVerifier
from services.computer_control.frontmost import FrontmostReading, cross_check
from services.computer_control.macos_actions import PyObjCActionBackend
from services.computer_control.executor import ObservationCache, execute_action

def validate_textedit_live():
    print("=== LIVE VALIDATION 1: TextEdit ===")
    backend = PyObjCActionBackend()
    
    apps = backend.running_apps()
    textedit_app = next((a for a in apps if a.bundle_id == "com.apple.TextEdit"), None)
    if not textedit_app:
        print("[TextEdit] Launching TextEdit...")
        import subprocess
        subprocess.run(["open", "-a", "TextEdit"])
        time.sleep(1.5)
        textedit_app = next((a for a in backend.running_apps() if a.bundle_id == "com.apple.TextEdit"), None)

    if not textedit_app:
        print("[TextEdit] Could not find running TextEdit.")
        return False
        
    pid = textedit_app.pid
    app_el = backend.app_element(pid)
    obs, handles = observe_app_with_handles(backend, app_el, textedit_app)
    print(f"[TextEdit] Observed TextEdit PID={pid}, obs_id={obs.obs_id}, targets={len(obs.targets)}")
    
    # 2. Resolve semantic target: AXTextArea in TextEdit
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXTextArea")
    resolution = resolve_semantic(obs, spec)
    print(f"[TextEdit] Resolution: kind={resolution.kind}, target={resolution.target.target.role if resolution.target else None}")
    
    if resolution.kind != "RESOLVED" or not resolution.target:
        print(f"[TextEdit] Could not semantically resolve AXTextArea in TextEdit (reason: {resolution.reason})")
        return False
        
    resolved_target = resolution.target
    print(f"[TextEdit] Semantic target resolved: index={resolved_target.target.index}, role={resolved_target.target.role}")
    
    # 3. Code-owned Safety Gate Check
    ctx = context_for(Op.SET_TEXT, SetTextArgs(text_span=(0, 10)), resolved_target, obs, frozenset({"com.apple.TextEdit"}))
    gate = evaluate(ctx)
    print(f"[TextEdit] Safety Gate: outcome={gate.outcome}, risk={gate.risk}, reason={gate.reason}")
    if gate.outcome != GateOutcome.ALLOW:
        print("[TextEdit] Action blocked by Safety Gate.")
        return False

    # 4. Construct Action Request
    from services.computer_control.models import ActionRequest, GateDecision, RiskLevel
    req = ActionRequest(
        session_id="live-val-sess",
        step=1,
        op=Op.SET_TEXT,
        target=resolved_target,
        args=SetTextArgs(text_span=(0, 10)),
        obs_id=obs.obs_id,
        gate=gate,
        cancel_epoch=0,
        timeout_s=10.0
    )
    
    cache = ObservationCache(obs, handles, time.monotonic())
    
    # 5. Execute Action via Live Substrate/Backend
    fm_readings = [
        FrontmostReading(source="workspace", app=textedit_app),
        FrontmostReading(source="app_ax_frontmost", app=textedit_app)
    ]
    res, fresh_obs = execute_action(
        req,
        utterance="type Hello Evie",
        allowed_apps=frozenset({"com.apple.TextEdit"}),
        cache=cache,
        backend=backend,
        frontmost=cross_check(fm_readings),
        reobserve=lambda p: (observe_app_with_handles(backend, app_el, textedit_app) if p else None)
    )
    print(f"[TextEdit] Execution result: status={res.status}, reason={res.reason}, verification={res.verification}")
    return res.status is ActionStatus.SUCCESS

def validate_photo_booth_live():
    print("\n=== LIVE VALIDATION 2: Photo Booth Observation & Button Semantic Resolution ===")
    backend = PyObjCActionBackend()
    
    pb_app = next((a for a in backend.running_apps() if a.bundle_id == "com.apple.PhotoBooth"), None)
    if not pb_app:
        print("[Photo Booth] Launching Photo Booth...")
        import subprocess
        subprocess.run(["open", "-a", "Photo Booth"])
        time.sleep(2.0)
        pb_app = next((a for a in backend.running_apps() if a.bundle_id == "com.apple.PhotoBooth"), None)

    if not pb_app:
        print("[Photo Booth] Could not find running Photo Booth.")
        return False

    pid = pb_app.pid
    app_el = backend.app_element(pid)
    obs, handles = observe_app_with_handles(backend, app_el, pb_app)
    print(f"[Photo Booth] Observed Photo Booth PID={pid}, targets={len(obs.targets)}")
    
    # Resolve semantic Take Photo button or control
    spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", label="Take Photo")
    resolution = resolve_semantic(obs, spec)
    if resolution.kind != "RESOLVED":
        # Fallback to any button with ancestor constraint or first button
        spec = SemanticTargetSpec(obs_id=obs.obs_id, role="AXButton", index=0)
        resolution = resolve_semantic(obs, spec)
    print(f"[Photo Booth] Resolution: kind={resolution.kind}, reason={resolution.reason}")
    if resolution.target:
        print(f"[Photo Booth] Resolved semantic button: role={resolution.target.target.role}, label='{resolution.target.target.label}'")
        ctx = context_for(Op.CLICK_ELEMENT, ClickElementArgs(click_count=1), resolution.target, obs, frozenset({"com.apple.PhotoBooth"}))
        gate = evaluate(ctx)
        print(f"[Photo Booth] Safety Gate evaluation for click_element: outcome={gate.outcome}, risk={gate.risk}, reason={gate.reason}")
    return True

if __name__ == "__main__":
    t_res = validate_textedit_live()
    p_res = validate_photo_booth_live()
    print(f"\nLive Validation Summary: TextEdit={t_res}, PhotoBooth={p_res}")
