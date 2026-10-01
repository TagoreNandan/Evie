"""
Shared builders for the computer-control unit tests (pure data; no OS access).

Kept outside conftest.py so the fast/live boundary module (Protected Area #4) is untouched.
"""

from services.computer_control.models import AppIdentity, Observation, SettleInfo, SettleStatus, WindowKey, title_hash

TEXTEDIT = AppIdentity(bundle_id="com.apple.TextEdit", pid=100, name="TextEdit")
CHROME = AppIdentity(bundle_id="com.google.Chrome", pid=200, name="Google Chrome")
SAFARI = AppIdentity(bundle_id="com.apple.Safari", pid=300, name="Safari")
TERMINAL = AppIdentity(bundle_id="com.apple.Terminal", pid=400, name="Terminal")
KEYCHAIN = AppIdentity(bundle_id="com.apple.keychainaccess", pid=500, name="Keychain Access")
RUNNING = (TEXTEDIT, CHROME, SAFARI, TERMINAL)

UTTERANCE = "type hello world"


def window(pid=100, doc="doc-a", main=True, focused=True, title=None):
    return WindowKey(pid=pid, document_hash=doc, title_hash=title_hash(title) if title else None,
                     role="AXWindow", subrole="AXStandardWindow", is_main=main, is_focused=focused)


TEXT_AREA = {"role": "AXTextArea", "identifier": "First Text View", "focused": True,
             "actions": ("AXShowMenu",), "settable": ("AXSelectedText", "AXValue", "AXSelectedTextRange")}
SCROLL_AREA = {"role": "AXScrollArea", "actions": ("AXScrollDownByPage", "AXScrollUpByPage")}
ALIGN_LEFT = {"role": "AXCheckBox", "subrole": "AXSegment", "label": "align left", "enabled": True,
              "value_summary": "1", "actions": ("AXPress",), "context": ("AXWindow", "AXGroup")}
ALIGN_CENTER = {"role": "AXCheckBox", "subrole": "AXSegment", "label": "align center", "enabled": True,
                "value_summary": "0", "actions": ("AXPress",), "context": ("AXWindow", "AXGroup")}
PLAIN_BUTTON = {"role": "AXButton", "label": "OK", "enabled": True, "actions": ("AXPress",)}

DEFAULT_TARGETS = (TEXT_AREA, SCROLL_AREA, ALIGN_LEFT, ALIGN_CENTER, PLAIN_BUTTON)


def observation(targets=DEFAULT_TARGETS, *, obs_id="obs-1", taken_at=0.0, frontmost=TEXTEDIT, app=TEXTEDIT,
                win="default", running=RUNNING, settle=SettleStatus.SETTLED, fingerprint="fp-1", **extra):
    built = [dict(t, index=i, obs_id=obs_id) for i, t in enumerate(targets)]
    return Observation(
        obs_id=obs_id, taken_at=taken_at, frontmost=frontmost, app=app,
        window=window(pid=app.pid) if win == "default" else win,
        targets=built, running_apps=running, settle=SettleInfo(status=settle, elapsed_ms=250),
        fingerprint=fingerprint, **extra,
    )


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
