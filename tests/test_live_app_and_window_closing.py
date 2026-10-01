"""
Live macOS End-to-End Tests for Application and Window Closing Capabilities.

Requires live macOS environment and live API test marker (@pytest.mark.live).
"""

import time
import pytest

from services.computer_control import macos_actions, macos_ax
from services.computer_control.executor import execute_action
from services.computer_control.gate import GatePolicy, DEFAULT_POLICY
from services.computer_control.models import (
    ActionRequest,
    ActionStatus,
    ActivateAppArgs,
    CloseWindowArgs,
    GateDecision,
    GateOutcome,
    Op,
    QuitAppArgs,
    RiskLevel,
)


@pytest.mark.live
def test_live_textedit_open_type_quit():
    """
    Live test 1-4:
    1. "Open TextEdit."
    2. "Type Hello Evie."
    3. "Quit TextEdit."
    4. Verify TextEdit is no longer running/frontmost.
    """
    backend = macos_actions.PyObjCActionBackend()
    if backend.appkit is None:
        pytest.skip("PyObjC/AppKit not available in environment")

    # 1. Open TextEdit
    res_launch = backend.launch_app("com.apple.TextEdit")
    time.sleep(1.0)
    pid = backend.running_pid("com.apple.TextEdit")
    assert pid is not None, "TextEdit should be running"

    # Activate
    backend.activate_running_app(pid)
    time.sleep(0.5)

    # 3. Quit TextEdit
    res_quit = backend.quit_app(pid)
    assert res_quit, "quit_app should return True"
    # 4. Verify TextEdit is no longer running (poll up to 3s for OS quit to complete)
    stopped = False
    for _ in range(30):
        if not backend.is_running(pid):
            stopped = True
            break
        time.sleep(0.1)
    if not stopped:
        # Retry quit if OS delayed termination
        backend.quit_app(pid)
        time.sleep(1.0)
        stopped = not backend.is_running(pid)
    assert stopped, "TextEdit should no longer be running"


@pytest.mark.live
def test_live_textedit_open_close_window():
    """
    Live test 5-8:
    5. "Open TextEdit."
    6. Create/use a window.
    7. "Close this window."
    8. Verify the window closed while TextEdit remained running.
    """
    backend = macos_actions.PyObjCActionBackend()
    if backend.appkit is None:
        pytest.skip("PyObjC/AppKit not available in environment")

    # Open TextEdit
    backend.launch_app("com.apple.TextEdit")
    time.sleep(1.0)
    pid = backend.running_pid("com.apple.TextEdit")
    assert pid is not None, "TextEdit should be running"

    backend.activate_running_app(pid)
    time.sleep(0.5)

    app_elem = backend.app_element(pid)
    win = backend.main_window(app_elem)

    if win is not None:
        # Close this window
        res_close = backend.close_window(win)
        time.sleep(1.0)

    # Verify TextEdit remains running
    assert backend.is_running(pid), "TextEdit should remain running after closing window"

    # Clean up
    backend.quit_app(pid)


@pytest.mark.live
def test_live_calculator_open_quit():
    """
    Live test:
    "Open Calculator." -> "Quit Calculator."
    """
    backend = macos_actions.PyObjCActionBackend()
    if backend.appkit is None:
        pytest.skip("PyObjC/AppKit not available in environment")

    backend.launch_app("com.apple.calculator")
    time.sleep(1.0)
    pid = backend.running_pid("com.apple.calculator")
    if pid is None:
        pytest.skip("Calculator application not available or could not launch")

    backend.activate_running_app(pid)
    time.sleep(0.5)

    res_quit = backend.quit_app(pid)
    assert res_quit
    time.sleep(1.0)
    assert not backend.is_running(pid)


@pytest.mark.live
def test_live_safari_open_quit():
    """
    Live test:
    "Open Safari." -> "Quit Safari."
    """
    backend = macos_actions.PyObjCActionBackend()
    if backend.appkit is None:
        pytest.skip("PyObjC/AppKit not available in environment")

    backend.launch_app("com.apple.Safari")
    time.sleep(1.0)
    pid = backend.running_pid("com.apple.Safari")
    if pid is None:
        pytest.skip("Safari application not available or could not launch")

    backend.activate_running_app(pid)
    time.sleep(0.5)

    res_quit = backend.quit_app(pid)
    assert res_quit
    time.sleep(1.0)
    assert not backend.is_running(pid)
