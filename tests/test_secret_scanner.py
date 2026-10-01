"""
Unit tests for SecretScanner and github_watch module.
"""

from unittest.mock import patch, MagicMock
import sys
import pytest

from integrations.github_watch import (
    SecretScanner,
    calculate_shannon_entropy,
    scan_local_commit,
    scan_github_repository_commits,
)


def test_calculate_shannon_entropy():
    # Low entropy (repeated char)
    assert calculate_shannon_entropy("aaaaaaaaaaaaaaaaaaaa") == 0.0
    # High entropy (random diverse chars)
    high_entropy = calculate_shannon_entropy("abcdefghijklmnopqrstuvwxyz0123456789!@")
    assert high_entropy > 4.5


def test_synthetic_secret_patterns_detection():
    scanner = SecretScanner()

    synthetic_diff = """
    + ghp_123456789012345678901234567890123456
    + github_pat_11AAAAAAA01234567890_1234567890abcdefghijklmnopqrstuvwxyz1234567890abcdefghijklm
    + sk-1234567890abcdef1234567890abcdef
    + sk-ant-api03-1234567890abcdef1234567890abcdef
    + AKIAIOSFODNN7EXAMPLE
    + https://slack.com/fake-test-webhook
    """

    findings = scanner.scan_diff(synthetic_diff)
    assert len(findings) >= 6

    types = [f["secret_type"] for f in findings]
    assert "GitHub Personal Access Token" in types
    assert "GitHub Fine-Grained PAT" in types
    assert "OpenAI API Key" in types
    assert "Anthropic API Key" in types
    assert "AWS Access Key ID" in types
    assert "Slack Webhook URL" in types

    # Verify zero raw secrets leak in findings
    for f in findings:
        val = f["redacted_value"]
        assert "123456789012345678901234567890123456" not in val
        assert "sk-1234567890abcdef1234567890abcdef" not in val
        assert val.startswith("gh") or val.startswith("sk") or val.startswith("AK") or val.startswith("ht") or val.startswith("gi")


def test_shannon_entropy_candidate_detection():
    scanner = SecretScanner(entropy_threshold=4.5, min_token_len=20)
    # High entropy random string token
    diff = "+ random_token = 'abcdefghijklmnopqrstuvwxyz0123456789!@'"
    findings = scanner.scan_diff(diff)

    assert len(findings) >= 1
    assert findings[0]["secret_type"] == "High Entropy Candidate Secret"
    assert "abcdefghijklmnopqrstuvwxyz0123456789!@" not in findings[0]["redacted_value"]


def test_true_negatives():
    scanner = SecretScanner()
    diff = """
    + def calculate_total_amount(price, tax_rate):
    +     # This is a normal python code comment
    +     return price * (1.0 + tax_rate)
    + import os, sys, logging
    """
    findings = scanner.scan_diff(diff)
    assert len(findings) == 0


def test_scan_local_commit_subprocess_mock():
    mock_diff_output = "+ ghp_123456789012345678901234567890123456"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=mock_diff_output, returncode=0)

        findings = scan_local_commit("/fake/repo/path", "abc1234")

        # Verify subprocess was called with array list and shell=False
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd == ["git", "diff", "abc1234^!", "--unified=0"]
        assert kwargs.get("shell") is False
        assert kwargs.get("cwd") == "/fake/repo/path"

        assert len(findings) == 1
        assert findings[0]["secret_type"] == "GitHub Personal Access Token"


def test_scan_github_repository_commits_mocked():
    mock_github_module = MagicMock()
    mock_github_cls = MagicMock()
    mock_github_module.Github = mock_github_cls

    with patch.dict(sys.modules, {"github": mock_github_module}):
        with patch("integrations.github_watch.get_credential", return_value="ghp_fake_token_123456"):
            mock_github = MagicMock()
            mock_repo = MagicMock()
            mock_commit = MagicMock()
            mock_commit.sha = "1234567890"
            mock_file = MagicMock()
            mock_file.filename = "config.py"
            mock_file.patch = "+ sk-1234567890abcdef1234567890abcdef"
            mock_commit.files = [mock_file]
            mock_repo.get_commits.return_value = [mock_commit]
            mock_github.get_repo.return_value = mock_repo
            mock_github_cls.return_value = mock_github

            findings = scan_github_repository_commits("user/repo")
            assert len(findings) == 1
            assert findings[0]["secret_type"] == "OpenAI API Key"
            assert findings[0]["redacted_value"] == "sk...ef"
            assert findings[0]["filename"] == "config.py"
