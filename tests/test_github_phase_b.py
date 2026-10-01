"""
Focused Unit Tests for Phase B GitHub Delta Processing & Webhook Event Flow:
- pushed_at repository delta filtering (skipping unchanged repos, processing changed repos)
- 3 of N repos changed / zero-change sync optimization
- Existing SHA-level commit scan deduplication
- Webhook HMAC verification, idempotency, DB activity updates, and raw secret redaction
"""

import hmac
import hashlib
import json
import os
import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from main import app, init_db
import keyring_utils
from integrations.github_watch import CommitScanResult
from services.github_intelligence import (
    init_github_tables,
    sync_github_intelligence,
    get_scanned_commit_shas,
    process_github_webhook_event,
)

client = TestClient(app)
TEST_DB = "test_github_phase_b.db"
WEBHOOK_SECRET = "test_webhook_secret"


def _use_webhook_secret(monkeypatch):
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: WEBHOOK_SECRET if key == "github_webhook_secret" else None)


def _post_signed(payload, event, delivery):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )


@pytest.fixture(autouse=True)
def setup_test_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    init_db(TEST_DB)
    init_github_tables(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_repo_unchanged_pushed_at_is_skipped(monkeypatch):
    """
    Verify repository with unchanged pushed_at is skipped during commit history scanning.
    """
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: "mock_token")

    pushed_time = "2026-09-20T12:00:00Z"
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """
            INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at)
            VALUES ('org/repo1', 'repo1', 'org', 0, 1, 'url', ?, ?)
            """,
            ("2026-09-20T10:00:00Z", pushed_time),
        )
        conn.commit()

    mock_discover = MagicMock(return_value=[{
        "full_name": "org/repo1",
        "name": "repo1",
        "owner": "org",
        "is_private": False,
        "is_org": True,
        "html_url": "url",
        "pushed_at": pushed_time,
    }])
    mock_activity = MagicMock()

    monkeypatch.setattr("services.github_intelligence.discover_user_repositories_detailed", mock_discover)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity", mock_activity)

    res = sync_github_intelligence(TEST_DB)
    assert res["repositories_synced"] == 1
    assert res["commits_synced"] == 0
    mock_activity.assert_not_called()


def test_repo_changed_pushed_at_is_processed(monkeypatch):
    """
    Verify repository with changed (newer) pushed_at timestamp proceeds to commit history processing.
    """
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: "mock_token")

    old_pushed = "2026-09-20T12:00:00Z"
    new_pushed = "2026-09-21T12:00:00Z"

    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            """
            INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at)
            VALUES ('org/repo1', 'repo1', 'org', 0, 1, 'url', ?, ?)
            """,
            ("2026-09-20T10:00:00Z", old_pushed),
        )
        conn.commit()

    mock_discover = MagicMock(return_value=[{
        "full_name": "org/repo1",
        "name": "repo1",
        "owner": "org",
        "is_private": False,
        "is_org": True,
        "html_url": "url",
        "pushed_at": new_pushed,
    }])
    mock_activity = MagicMock(return_value={
        "commits": [{"sha": "c111", "author": "dev", "summary": "feat: new work", "html_url": "url", "timestamp": new_pushed}],
        "pull_requests": [],
    })
    mock_commits_scan = MagicMock(return_value=CommitScanResult([], [], True, False))

    monkeypatch.setattr("services.github_intelligence.discover_user_repositories_detailed", mock_discover)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity", mock_activity)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_commits", mock_commits_scan)

    res = sync_github_intelligence(TEST_DB)
    assert res["repositories_synced"] == 1
    assert res["commits_synced"] == 1
    mock_activity.assert_called_once_with("org/repo1", token_key="github_token")


def test_three_of_n_repos_changed(monkeypatch):
    """
    Verify if 5 repositories are known and only 3 changed pushed_at, only those 3 process activity.
    """
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: "mock_token")

    timestamp_old = "2026-09-20T12:00:00Z"
    timestamp_new = "2026-09-22T12:00:00Z"

    with sqlite3.connect(TEST_DB) as conn:
        for i in range(1, 6):
            conn.execute(
                """
                INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at)
                VALUES (?, ?, 'org', 0, 1, 'url', ?, ?)
                """,
                (f"org/repo{i}", f"repo{i}", "2026-09-20T10:00:00Z", timestamp_old),
            )
        conn.commit()

    # Repos 1, 2, 3 have new pushed_at; Repos 4, 5 remain unchanged
    discovered = []
    for i in range(1, 6):
        pushed = timestamp_new if i <= 3 else timestamp_old
        discovered.append({
            "full_name": f"org/repo{i}",
            "name": f"repo{i}",
            "owner": "org",
            "is_private": False,
            "is_org": True,
            "html_url": "url",
            "pushed_at": pushed,
        })

    mock_discover = MagicMock(return_value=discovered)
    mock_activity = MagicMock(return_value={"commits": [], "pull_requests": []})
    mock_commits_scan = MagicMock(return_value=CommitScanResult([], [], True, False))

    monkeypatch.setattr("services.github_intelligence.discover_user_repositories_detailed", mock_discover)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity", mock_activity)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_commits", mock_commits_scan)

    res = sync_github_intelligence(TEST_DB)
    assert res["repositories_synced"] == 5
    assert mock_activity.call_count == 3


def test_zero_change_sync_makes_no_unnecessary_calls(monkeypatch):
    """
    Verify a zero-change sync (all repos unchanged) makes zero activity or commit scanning calls.
    """
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: "mock_token")

    timestamp_same = "2026-09-20T12:00:00Z"

    with sqlite3.connect(TEST_DB) as conn:
        for i in range(1, 4):
            conn.execute(
                """
                INSERT INTO github_repo_cache (full_name, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at)
                VALUES (?, ?, 'org', 0, 1, 'url', ?, ?)
                """,
                (f"org/repo{i}", f"repo{i}", "2026-09-20T10:00:00Z", timestamp_same),
            )
        conn.commit()

    discovered = [
        {
            "full_name": f"org/repo{i}",
            "name": f"repo{i}",
            "owner": "org",
            "is_private": False,
            "is_org": True,
            "html_url": "url",
            "pushed_at": timestamp_same,
        }
        for i in range(1, 4)
    ]

    mock_discover = MagicMock(return_value=discovered)
    mock_activity = MagicMock()
    mock_commits_scan = MagicMock()

    monkeypatch.setattr("services.github_intelligence.discover_user_repositories_detailed", mock_discover)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity", mock_activity)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_commits", mock_commits_scan)

    res = sync_github_intelligence(TEST_DB)
    assert res["repositories_synced"] == 3
    mock_activity.assert_not_called()
    mock_commits_scan.assert_not_called()


def test_existing_sha_level_scan_deduplication():
    """
    Verify scanned commit SHAs recorded in github_scanned_commits are retrieved and deduplicated.
    """
    with sqlite3.connect(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO github_scanned_commits (repo_name, commit_sha) VALUES ('org/repo1', 'abc1234567')"
        )
        conn.commit()

    scanned = get_scanned_commit_shas(TEST_DB, "org/repo1")
    assert "abc1234567" in scanned
    assert "abc1234" in scanned  # 7-char short SHA


def test_valid_push_webhook_updates_github_activity(monkeypatch):
    """
    Verify valid push webhook immediately updates github_activity_log table.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    _use_webhook_secret(monkeypatch)

    payload = {
        "repository": {"full_name": "acme/web-app", "name": "web-app"},
        "sender": {"login": "octocat"},
        "commits": [
            {
                "id": "commit_sha_999",
                "author": {"name": "octocat"},
                "message": "fix: update dependency",
                "url": "https://github.com/acme/web-app/commit/commit_sha_999",
            }
        ],
        "ref": "refs/heads/main",
    }

    res = _post_signed(payload, "push", "delivery-unique-101")
    assert res.status_code == 200
    assert res.json()["status"] == "processed"

    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT repo_name, identifier, summary, author FROM github_activity_log WHERE repo_name = 'acme/web-app'")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "acme/web-app"
        assert row[1] == "commit_sha_999"
        assert "fix: update dependency" in row[2]


def test_duplicate_webhook_delivery_idempotent(monkeypatch):
    """
    Verify duplicate webhook delivery ID is rejected by idempotency check and creates zero duplicate activity rows.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    _use_webhook_secret(monkeypatch)

    payload = {
        "repository": {"full_name": "acme/web-app", "name": "web-app"},
        "sender": {"login": "octocat"},
        "commits": [{"id": "sha_dup_123", "message": "First commit"}],
    }

    # Delivery 1
    res1 = _post_signed(payload, "push", "delivery-dup-555")
    assert res1.status_code == 200
    assert res1.json()["status"] == "processed"

    # Delivery 2 (Duplicate delivery ID)
    res2 = _post_signed(payload, "push", "delivery-dup-555")
    assert res2.status_code == 200
    assert res2.json()["status"] == "already_processed"

    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM github_activity_log WHERE repo_name = 'acme/web-app'")
        assert cursor.fetchone()[0] == 1


def test_invalid_hmac_webhook_rejected(monkeypatch):
    """
    Verify webhook request with invalid HMAC signature raises HTTP 401 Unauthorized.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: "my_secret_key" if key == "github_webhook_secret" else None)

    res = client.post(
        "/webhooks/github",
        json={"repository": {"name": "test"}},
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "deliv-999",
            "X-Hub-Signature-256": "sha256=invalid_signature_hash",
        },
    )
    assert res.status_code == 401
    assert "UNAUTHORIZED_WEBHOOK" in res.json()["detail"]


def test_unsupported_webhook_event_handled_safely(monkeypatch):
    """
    Verify unsupported webhook event (e.g. 'ping', 'fork', 'star') completes safely without error.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    _use_webhook_secret(monkeypatch)

    payload = {
        "repository": {"full_name": "acme/web-app", "name": "web-app"},
        "sender": {"login": "octocat"},
        "zen": "Non-blocking is better than blocking.",
    }

    res = _post_signed(payload, "ping", "delivery-ping-777")
    assert res.status_code == 200
    assert res.json()["status"] == "processed"
    assert "ping" in res.json()["event_summary"]


def test_webhook_triggered_secret_scanning_never_stores_raw_secrets(monkeypatch):
    """
    Verify commit payload containing raw secret (e.g., AWS Key) triggers secret scanning,
    persists finding in secret_findings with redacted preview, and NEVER stores raw secret string.
    """
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: None)

    raw_secret = "AKIA1234567890123456"
    payload = {
        "repository": {"full_name": "acme/web-app", "name": "web-app"},
        "event_type": "push",
        "commits": [
            {
                "id": "commit_sha_secret_77",
                "author": {"name": "developer"},
                "message": "Add AWS config",
                "patch": f"+ AWS_KEY={raw_secret}\n+ print('configured')",
            }
        ],
    }

    res = process_github_webhook_event(payload, TEST_DB)
    assert res["status"] == "processed"
    assert res["secrets_found"] >= 1

    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT redacted_preview, commit_sha FROM secret_findings WHERE repo_name = 'acme/web-app'")
        row = cursor.fetchone()
        assert row is not None
        redacted = row[0]
        assert redacted.startswith("AK") and "..." in redacted
        assert raw_secret not in redacted  # Full raw secret string must be redacted!
        assert len(redacted) < len(raw_secret) or redacted != raw_secret
