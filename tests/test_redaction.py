"""
Unit tests for redaction module.
"""

from redaction import redact_secret, redact_dict_secrets


def test_redact_secret_none_and_empty():
    assert redact_secret(None) == "****"
    assert redact_secret("") == "****"


def test_redact_secret_short_strings():
    assert redact_secret("a") == "****"
    assert redact_secret("12") == "****"
    assert redact_secret("1234") == "****"


def test_redact_secret_normal_strings():
    assert redact_secret("ghp_1234567890abcdef") == "gh...ef"
    assert redact_secret("sk-proj-987654321") == "sk...21"


def test_redact_dict_secrets_case_insensitive_and_immutability():
    original = {
        "user": "alice",
        "GitHub_Token": "ghp_synthetic_token_12345",
        "user_PASSWORD": "SuperSecretPassword123",
        "API_KEY_VAL": "sk-1234567890abcdef",
        "my_secret_data": "secret_data_value",
        "Access_Key_ID": "AKIAIOSFODNN7EXAMPLE",
        "PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----",
        "user_CREDENTIAL": "my_credential_val",
        "nested": {
            "normal_key": "safe_value",
            "db_password": "nested_db_pass_1234"
        }
    }

    # Make a copy to test immutability
    original_copy = dict(original)
    original_nested_copy = dict(original["nested"])

    result = redact_dict_secrets(original)

    # Verify input dictionary was not mutated
    assert original == original_copy
    assert original["nested"] == original_nested_copy
    assert result is not original

    # Verify redaction results
    assert result["user"] == "alice"
    assert result["GitHub_Token"] == "gh...45"
    assert result["user_PASSWORD"] == "Su...23"
    assert result["API_KEY_VAL"] == "sk...ef"
    assert result["my_secret_data"] == "se...ue"
    assert result["Access_Key_ID"] == "AK...LE"
    assert result["PRIVATE_KEY_PEM"] == "--...--"
    assert result["user_CREDENTIAL"] == "my...al"
    assert result["nested"]["normal_key"] == "safe_value"
    assert result["nested"]["db_password"] == "ne...34"
