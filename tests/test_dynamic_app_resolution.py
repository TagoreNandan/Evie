"""
Unit tests for Dynamic Installed-App Discovery & Normalization in Computer Control.
"""

import pytest
from services.computer_control.chooser import AppRef
from services.computer_control.fast_path import resolve_app, _clean_slug
from services.computer_control.gate import derive_allowed_apps, evaluate, context_for, GateOutcome, RiskLevel
from services.computer_control.models import AppIdentity, ActivateAppArgs, Op, Observation


def test_clean_slug_normalization():
    assert _clean_slug("TextEdit") == "textedit"
    assert _clean_slug("text edit") == "textedit"
    assert _clean_slug("text-edit") == "textedit"
    assert _clean_slug("Photo Booth") == "photobooth"
    assert _clean_slug("photo booth") == "photobooth"
    assert _clean_slug("photo-booth") == "photobooth"


def test_running_app_resolves_successfully():
    textedit = AppRef(bundle_id="com.apple.TextEdit", name="TextEdit")
    status, value = resolve_app("TextEdit", running=[textedit], installed=[textedit])
    assert status == "OK"
    assert value == "com.apple.TextEdit"


def test_installed_not_running_app_resolves_successfully():
    photobooth = AppRef(bundle_id="com.apple.PhotoBooth", name="Photo Booth")
    status, value = resolve_app("Photo Booth", running=[], installed=[photobooth])
    assert status == "OK"
    assert value == "com.apple.PhotoBooth"


def test_textedit_and_text_edit_resolve_equivalently():
    textedit = AppRef(bundle_id="com.apple.TextEdit", name="TextEdit")
    
    # 1. "TextEdit"
    status1, value1 = resolve_app("TextEdit", running=[textedit], installed=[textedit])
    
    # 2. "text edit" (space variation)
    status2, value2 = resolve_app("text edit", running=[textedit], installed=[textedit])
    
    # 3. "text-edit" (hyphen variation)
    status3, value3 = resolve_app("text-edit", running=[textedit], installed=[textedit])
    
    assert status1 == status2 == status3 == "OK"
    assert value1 == value2 == value3 == "com.apple.TextEdit"


def test_unknown_new_app_resolves_without_alias():
    new_app = AppRef(bundle_id="com.thirdparty.customapp", name="Custom App")
    status, value = resolve_app("custom app", running=[], installed=[new_app])
    assert status == "OK"
    assert value == "com.thirdparty.customapp"


def test_nonexistent_app_returns_unknown_app():
    status, value = resolve_app("Nonexistent Application XYZ", running=[], installed=[])
    assert status == "ASK"
    assert value == "UNKNOWN_APP"


def test_ambiguous_apps_return_ask():
    photo_one = AppRef(bundle_id="com.test.photoone", name="Photo One")
    photo_two = AppRef(bundle_id="com.test.phototwo", name="Photo Two")
    status, value = resolve_app("photo", running=[], installed=[photo_one, photo_two])
    assert status == "ASK"
    assert value == "AMBIGUOUS_APP"


def test_derive_allowed_apps_includes_installed_apps():
    photobooth = AppRef(bundle_id="com.apple.PhotoBooth", name="Photo Booth")
    allowed = derive_allowed_apps("open photo booth", frontmost=None, known_apps=[], installed_apps=[photobooth])
    assert "com.apple.PhotoBooth" in allowed


from services.computer_control.models import SettleInfo, SettleStatus


def test_safety_gate_remains_intact_for_allowed_installed_app():
    photobooth_id = "com.apple.PhotoBooth"
    allowed = frozenset({photobooth_id})
    obs = Observation(
        obs_id="obs_1",
        app=AppIdentity(bundle_id="com.apple.finder", pid=100),
        taken_at=1000.0,
        settle=SettleInfo(status=SettleStatus.SETTLED, elapsed_ms=100.0),
        fingerprint="fp123",
    )
    ctx = context_for(Op.ACTIVATE_APP, ActivateAppArgs(bundle_id=photobooth_id), None, obs, allowed)
    gate_decision = evaluate(ctx)
    assert gate_decision.outcome == GateOutcome.ALLOW
    assert gate_decision.risk == RiskLevel.LOW
