"""
Read-only code-signature check of a RUNNING process (docs/07_Computer_Control.md Section 34; CC-1a Step 13).

Isolated on purpose: this is the only module that calls Security.framework, through ctypes (no new
dependency). For a pid it asks the kernel/Security for that process's code object (dynamic check, not a
file on disk), validates it against the requirement `anchor apple generic` (a real Apple-issued signing
certificate: ad-hoc and self-signed code fail), and returns the signing identifier and Team ID.
It never signs, modifies, launches or grants anything.

Run only inside the worker process (via macos_probe.collect_identity). Blocked under pytest unless the
explicit live test sets EVIE_LIVE_MACOS=1.
"""

import ctypes
import ctypes.util
import os
from typing import Optional

from services.computer_control.runtime import SignatureInfo

APPLE_ANCHOR_REQUIREMENT = b"anchor apple generic"
LIVE_ENV = "EVIE_LIVE_MACOS"
_UTF8 = 0x08000100                     # kCFStringEncodingUTF8
_CF_NUMBER_INT = 9                     # kCFNumberIntType
_SIGNING_INFORMATION = 1 << 1          # kSecCSSigningInformation


def _blocked_under_test() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ and os.environ.get(LIVE_ENV) != "1"


class _Frameworks:
    def __init__(self):
        self.cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        self.sec = ctypes.CDLL(ctypes.util.find_library("Security"))
        vp, i32, u32 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32
        self.cf.CFNumberCreate.restype, self.cf.CFNumberCreate.argtypes = vp, [vp, ctypes.c_long, vp]
        self.cf.CFDictionaryCreate.restype = vp
        self.cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        self.cf.CFDictionaryGetValue.restype, self.cf.CFDictionaryGetValue.argtypes = vp, [vp, vp]
        self.cf.CFStringCreateWithCString.restype = vp
        self.cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, u32]
        self.cf.CFRelease.argtypes = [vp]
        self.sec.SecCodeCopyGuestWithAttributes.restype = i32
        self.sec.SecCodeCopyGuestWithAttributes.argtypes = [vp, vp, u32, ctypes.POINTER(vp)]
        self.sec.SecRequirementCreateWithString.restype = i32
        self.sec.SecRequirementCreateWithString.argtypes = [vp, u32, ctypes.POINTER(vp)]
        self.sec.SecCodeCheckValidity.restype, self.sec.SecCodeCheckValidity.argtypes = i32, [vp, u32, vp]
        self.sec.SecCodeCopySigningInformation.restype = i32
        self.sec.SecCodeCopySigningInformation.argtypes = [vp, u32, ctypes.POINTER(vp)]

    def const(self, lib, name: str) -> ctypes.c_void_p:
        return ctypes.c_void_p.in_dll(lib, name)

    def string(self, ref) -> Optional[str]:
        if not ref:
            return None
        buf = ctypes.create_string_buffer(1024)
        return buf.value.decode() if self.cf.CFStringGetCString(ref, buf, 1024, _UTF8) else None


def process_signature(pid: int, frameworks: Optional[_Frameworks] = None) -> SignatureInfo:
    """Signature facts for a running pid. Never raises; anything not established stays None/False."""
    if _blocked_under_test():
        return SignatureInfo(checked=False, error="BLOCKED_UNDER_TEST")
    try:
        fw = frameworks or _Frameworks()
    except OSError as e:
        return SignatureInfo(checked=False, error=f"FRAMEWORK_UNAVAILABLE: {e}"[:200])
    owned = []
    try:
        value = ctypes.c_int(pid)
        number = fw.cf.CFNumberCreate(None, _CF_NUMBER_INT, ctypes.byref(value))
        owned.append(number)
        keys = (ctypes.c_void_p * 1)(fw.const(fw.sec, "kSecGuestAttributePid"))
        values = (ctypes.c_void_p * 1)(number)
        attrs = fw.cf.CFDictionaryCreate(None, keys, values, 1,
                                         ctypes.addressof(ctypes.c_char.in_dll(fw.cf, "kCFTypeDictionaryKeyCallBacks")),
                                         ctypes.addressof(ctypes.c_char.in_dll(fw.cf,
                                                                               "kCFTypeDictionaryValueCallBacks")))
        owned.append(attrs)
        code = ctypes.c_void_p()
        status = fw.sec.SecCodeCopyGuestWithAttributes(None, attrs, 0, ctypes.byref(code))
        if status != 0 or not code:
            return SignatureInfo(checked=True, valid=False, status=status, error="NO_CODE_OBJECT_FOR_PID")
        owned.append(code)
        requirement = ctypes.c_void_p()
        req_text = fw.cf.CFStringCreateWithCString(None, APPLE_ANCHOR_REQUIREMENT, _UTF8)
        owned.append(req_text)
        rstatus = fw.sec.SecRequirementCreateWithString(req_text, 0, ctypes.byref(requirement))
        if rstatus != 0:
            return SignatureInfo(checked=True, valid=False, status=rstatus, error="REQUIREMENT_UNAVAILABLE")
        owned.append(requirement)
        vstatus = fw.sec.SecCodeCheckValidity(code, 0, requirement)       # signature intact AND Apple-anchored
        info = ctypes.c_void_p()
        istatus = fw.sec.SecCodeCopySigningInformation(code, _SIGNING_INFORMATION, ctypes.byref(info))
        identifier = team = None
        if istatus == 0 and info:
            owned.append(info)
            identifier = fw.string(fw.cf.CFDictionaryGetValue(info, fw.const(fw.sec, "kSecCodeInfoIdentifier")))
            team = fw.string(fw.cf.CFDictionaryGetValue(info, fw.const(fw.sec, "kSecCodeInfoTeamIdentifier")))
        return SignatureInfo(checked=True, valid=vstatus == 0, status=vstatus, identifier=identifier, team_id=team,
                             error=None if vstatus == 0 else "SIGNATURE_NOT_VALID_OR_NOT_APPLE_ANCHORED")
    except Exception as e:                                                  # never raise into the probe
        return SignatureInfo(checked=False, error=f"{type(e).__name__}: {e}"[:200])
    finally:
        for ref in reversed(owned):
            if ref:
                fw.cf.CFRelease(ref)
