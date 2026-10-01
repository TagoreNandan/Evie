"""
Secret Redaction Utility for Evie Assistant.

Implements SEC-01 & SEC-05 redaction fences:
Ensures unredacted secrets are never printed, logged, or returned in findings/logs.
"""

from typing import Any, Dict

SENSITIVE_KEY_PATTERNS = (
    "token",
    "secret",
    "password",
    "api_key",
    "access_key",
    "private_key",
    "credential",
)


def redact_secret(val: str | None) -> str:
    """
    Redact a sensitive string value.
    If val is None, empty, or len(val) <= 4, returns "****".
    Otherwise, returns first 2 chars, '...', and last 2 chars.
    """
    if val is None or not val or len(val) <= 4:
        return "****"
    return f"{val[:2]}...{val[-2:]}"


def is_sensitive_key(key: str) -> bool:
    """
    Check if key contains any sensitive pattern (case-insensitive).
    """
    key_lower = str(key).lower()
    return any(pattern in key_lower for pattern in SENSITIVE_KEY_PATTERNS)


def redact_dict_secrets(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively redact sensitive keys in a dictionary.
    MUST return a new dictionary without mutating the input dictionary `d`.
    """
    if not isinstance(d, dict):
        return d

    result: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = redact_dict_secrets(v)
        elif is_sensitive_key(k) and isinstance(v, str):
            result[k] = redact_secret(v)
        elif is_sensitive_key(k) and v is not None and not isinstance(v, (bool, int, float)):
            result[k] = redact_secret(str(v))
        else:
            result[k] = v
    return result
