"""
Keyring Vault Wrapper for Evie Assistant.

Implements SEC-05: Credentials and API tokens are stored exclusively in the OS native keyring.
No credentials are saved in SQLite or plaintext files.
"""

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "evie_assistant"


def set_credential(service: str, key: str, value: str) -> None:
    """
    Store a credential in the OS keyring.
    """
    if not service or not key:
        raise ValueError("Service and key must be non-empty strings")
    try:
        keyring.set_password(service, key, value)
    except Exception as e:
        raise RuntimeError(f"Failed to set credential in OS keyring: {e}") from e


def get_credential(service: str, key: str) -> str | None:
    """
    Retrieve a credential from the OS keyring. Returns None if key not found.
    """
    if not service or not key:
        raise ValueError("Service and key must be non-empty strings")
    try:
        return keyring.get_password(service, key)
    except Exception as e:
        raise RuntimeError(f"Failed to get credential from OS keyring: {e}") from e


def delete_credential(service: str, key: str) -> None:
    """
    Delete a credential from the OS keyring.
    """
    if not service or not key:
        raise ValueError("Service and key must be non-empty strings")
    try:
        keyring.delete_password(service, key)
    except KeyringError:
        # Keyring throws PasswordDeleteError if item was not found
        pass
    except Exception as e:
        raise RuntimeError(f"Failed to delete credential from OS keyring: {e}") from e
