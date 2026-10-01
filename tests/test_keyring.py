"""
Unit tests for keyring_utils module.
Mocks the keyring backend to prevent mutating native OS Keychain credentials.
"""

from unittest.mock import patch
import pytest
from keyring.errors import PasswordDeleteError

from keyring_utils import set_credential, get_credential, delete_credential, SERVICE_NAME


def test_set_and_get_credential_mocked():
    vault = {}

    def mock_set_password(service, username, password):
        vault[(service, username)] = password

    def mock_get_password(service, username):
        return vault.get((service, username))

    with patch("keyring.set_password", side_effect=mock_set_password), \
         patch("keyring.get_password", side_effect=mock_get_password):
        set_credential(SERVICE_NAME, "github_token", "ghp_synthetic_token_12345")
        retrieved = get_credential(SERVICE_NAME, "github_token")
        assert retrieved == "ghp_synthetic_token_12345"


def test_get_nonexistent_credential_mocked():
    with patch("keyring.get_password", return_value=None):
        retrieved = get_credential(SERVICE_NAME, "nonexistent_key")
        assert retrieved is None


def test_delete_credential_mocked():
    vault = {(SERVICE_NAME, "temp_key"): "secret_val"}

    def mock_delete_password(service, username):
        if (service, username) in vault:
            del vault[(service, username)]
        else:
            raise PasswordDeleteError("Password not found")

    def mock_get_password(service, username):
        return vault.get((service, username))

    with patch("keyring.delete_password", side_effect=mock_delete_password), \
         patch("keyring.get_password", side_effect=mock_get_password):
        delete_credential(SERVICE_NAME, "temp_key")
        assert get_credential(SERVICE_NAME, "temp_key") is None


def test_delete_nonexistent_credential_does_not_raise():
    with patch("keyring.delete_password", side_effect=PasswordDeleteError("Not found")):
        # Should complete without throwing an exception
        delete_credential(SERVICE_NAME, "missing_key")


def test_keyring_failure_raises_runtime_error():
    with patch("keyring.set_password", side_effect=Exception("Keyring service unavailable")):
        with pytest.raises(RuntimeError, match="Failed to set credential in OS keyring"):
            set_credential(SERVICE_NAME, "test_key", "val")


def test_invalid_arguments_raise_value_error():
    with pytest.raises(ValueError):
        set_credential("", "key", "val")
    with pytest.raises(ValueError):
        get_credential("service", "")
