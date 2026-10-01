"""
OS Adapter for Evie's general voice/chat commands (02_TRD.md Section 3).

Implements exactly one OS-level action, `open_app`, for an explicit allow-list of applications.
macOS is supported first via `osascript`; Windows/Linux adapters are future work.

Security boundary:
- Callers (and the LLM planner) can only name an application. The name is resolved against a
  fixed allow-list; anything else (paths, shell commands, AppleScript, executables) is rejected.
- The AppleScript is a fixed template. The allow-listed app's bundle identifier is passed as an
  argv argument, never interpolated into script text, and subprocess never uses a shell.
- Installation is checked against fixed, code-defined bundle locations before launching, so an
  unknown app never triggers macOS's "Where is ...?" dialog.
"""

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class AllowedApp:
    canonical: str
    bundle_id: str
    aliases: Tuple[str, ...]
    macos_paths: Tuple[str, ...]


ALLOWED_APPS: Tuple[AllowedApp, ...] = (
    AllowedApp("Safari", "com.apple.Safari", ("safari",), ("/Applications/Safari.app",)),
    AllowedApp("Google Chrome", "com.google.Chrome", ("google chrome", "chrome"),
               ("/Applications/Google Chrome.app", "~/Applications/Google Chrome.app")),
    AllowedApp("Firefox", "org.mozilla.firefox", ("firefox", "mozilla firefox"),
               ("/Applications/Firefox.app", "~/Applications/Firefox.app")),
    AllowedApp("Finder", "com.apple.finder", ("finder",), ("/System/Library/CoreServices/Finder.app",)),
    AllowedApp("Terminal", "com.apple.Terminal", ("terminal",),
               ("/System/Applications/Utilities/Terminal.app", "/Applications/Utilities/Terminal.app")),
    AllowedApp("Visual Studio Code", "com.microsoft.VSCode", ("visual studio code", "vs code", "vscode"),
               ("/Applications/Visual Studio Code.app", "~/Applications/Visual Studio Code.app")),
)

_ALIASES: Dict[str, AllowedApp] = {alias: app for app in ALLOWED_APPS for alias in app.aliases}
ALLOWED_APP_NAMES = tuple(app.canonical for app in ALLOWED_APPS)

# Fixed AppleScript: the bundle id arrives as argv data, never as script text.
MACOS_OPEN_SCRIPT = ("on run argv", "tell application id (item 1 of argv) to activate", "end run")
LAUNCH_TIMEOUT_SECONDS = 10


class OpenAppError(Exception):
    """Carries a safe reason code as its message."""


APP_NOT_ALLOWED = "APP_NOT_ALLOWED"
APP_NOT_INSTALLED = "APP_NOT_INSTALLED"
UNSUPPORTED_OS = "UNSUPPORTED_OS"
LAUNCH_FAILED = "LAUNCH_FAILED"


def resolve_app(name: str) -> Optional[AllowedApp]:
    """Map a requested name to an allow-listed app. Only case/whitespace differences are forgiven."""
    if not isinstance(name, str):
        return None
    normalized = " ".join(name.strip().lower().split())
    return _ALIASES.get(normalized)


_REQUEST_PATTERN = re.compile(
    r"\b(?:open|launch|start)\s+(?:up\s+)?(?:the\s+)?("
    + "|".join(re.escape(a) for a in sorted(_ALIASES, key=len, reverse=True))
    + r")(?:\s+app(?:lication)?)?\b",
    re.IGNORECASE,
)


def find_app_request(message: str) -> Optional[str]:
    """
    Deterministic fallback detection of "open/launch/start <allow-listed app>".
    Returns the canonical app name, or None. Never extracts free text.
    """
    match = _REQUEST_PATTERN.search(message or "")
    if not match:
        return None
    app = resolve_app(match.group(1))
    return app.canonical if app else None


class MacOSAdapter:
    name = "macos"

    def is_installed(self, app: AllowedApp) -> bool:
        return any(os.path.isdir(os.path.expanduser(p)) for p in app.macos_paths)

    def open_app(self, app: AllowedApp) -> None:
        if not self.is_installed(app):
            raise OpenAppError(APP_NOT_INSTALLED)
        cmd = ["osascript"]
        for line in MACOS_OPEN_SCRIPT:
            cmd += ["-e", line]
        cmd.append(app.bundle_id)
        try:
            result = subprocess.run(cmd, shell=False, capture_output=True, timeout=LAUNCH_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError):
            raise OpenAppError(LAUNCH_FAILED)
        if result.returncode != 0:
            raise OpenAppError(LAUNCH_FAILED)  # stderr deliberately not surfaced


class UnsupportedOSAdapter:
    name = "unsupported"

    def open_app(self, app: AllowedApp) -> None:
        raise OpenAppError(UNSUPPORTED_OS)


def get_os_adapter():
    """Windows/Linux adapters are future work (TRD Section 3)."""
    if platform.system() == "Darwin":
        return MacOSAdapter()
    return UnsupportedOSAdapter()


def _launch_blocked() -> bool:
    """The fast test suite must never launch real applications."""
    return "PYTEST_CURRENT_TEST" in os.environ


def open_app(requested_name: str) -> str:
    """
    Open an allow-listed application. Returns its canonical name.
    Raises OpenAppError(APP_NOT_ALLOWED | APP_NOT_INSTALLED | UNSUPPORTED_OS | LAUNCH_FAILED).
    """
    app = resolve_app(requested_name)
    if app is None:
        raise OpenAppError(APP_NOT_ALLOWED)
    if _launch_blocked():
        raise OpenAppError(LAUNCH_FAILED)
    get_os_adapter().open_app(app)
    return app.canonical
