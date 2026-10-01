"""
Frontmost cross-check (CC-1a Step 4): agreement of independent readings, or fail closed. Pure.
"""

from cc_fixtures import SAFARI, TEXTEDIT
from services.computer_control.frontmost import FrontmostReading as R, cross_check
from services.computer_control.models import AppIdentity


def test_two_agreeing_sources():
    c = cross_check([R(source="nsworkspace", app=SAFARI), R(source="app_ax_frontmost", app=SAFARI)])
    assert c.agreed and c.app == SAFARI and c.sources == ("nsworkspace", "app_ax_frontmost")


def test_failed_readings_are_recorded_but_not_votes():
    c = cross_check([R(source="nsworkspace", app=SAFARI), R(source="ax_focused_application", error="AX_ERROR:-25204"),
                     R(source="app_ax_frontmost", app=SAFARI)])
    assert c.agreed and len(c.readings) == 3 and c.sources == ("nsworkspace", "app_ax_frontmost")


def test_single_source_is_insufficient():
    c = cross_check([R(source="nsworkspace", app=SAFARI), R(source="ax_focused_application", error="AX_ERROR:-25204")])
    assert not c.agreed and c.app is None and c.diagnostic.startswith("INSUFFICIENT_SOURCES")


def test_disagreement_fails_closed_without_guessing():
    c = cross_check([R(source="nsworkspace", app=SAFARI), R(source="ax_focused_application", app=TEXTEDIT),
                     R(source="app_ax_frontmost", app=SAFARI)])
    assert not c.agreed and c.app is None and "nsworkspace=com.apple.Safari" in c.diagnostic


def test_same_bundle_different_process_disagrees():
    other = AppIdentity(bundle_id=SAFARI.bundle_id, pid=999, name="Safari")
    assert not cross_check([R(source="nsworkspace", app=SAFARI), R(source="app_ax_frontmost", app=other)]).agreed


def test_no_readings():
    assert not cross_check([]).agreed
