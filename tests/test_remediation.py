"""
Unit tests for Remediation Guidance & Dependabot Watcher (remediation.py & integrations/dependency_watch.py).
Tests SCORE-02, SCORE-03, step-by-step guide generation, Dependabot alert persistence, and FastAPI endpoints.
"""

import sqlite3
import pytest
from unittest.mock import patch, MagicMock

import remediation
import integrations.dependency_watch as dep_watch
from main import app, DB_PATH
from fastapi.testclient import TestClient


@pytest.fixture
def test_db(tmp_path):
    """
    Create temporary SQLite database pre-populated with finding tables.
    """
    db_file = str(tmp_path / "test_remediation_evie.db")

    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS secret_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line_number INTEGER,
                pattern_type TEXT,
                redacted_preview TEXT,
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS breach_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_checked TEXT NOT NULL,
                breach_name TEXT,
                acknowledged INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exposure_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_name TEXT,
                exposure_type TEXT,
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twofa_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                twofa_enabled INTEGER,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dependency_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                package_name TEXT,
                severity TEXT,
                advisory_url TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.commit()

    return db_file


def test_get_remediation_guide_secret(test_db):
    """
    Verify step-by-step remediation guide generation for secret findings.
    """
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO secret_findings (repo_name, commit_sha, file_path, line_number, pattern_type, redacted_preview) VALUES (?, ?, ?, ?, ?, ?)",
            ("Evie", "abc1234", "config.py", 42, "aws_access_key", "AKIA...34"),
        )
        conn.commit()

    guide = remediation.get_remediation_guide("secret", 1, test_db)
    assert guide["finding_type"] == "secret"
    assert guide["finding_id"] == 1
    assert "Remediate Leaked Credential" in guide["title"]
    assert len(guide["steps"]) >= 4
    assert guide["details"]["preview"] == "AKIA...34"


def test_get_remediation_guide_breach_and_exposure(test_db):
    """
    Verify remediation guides for breach and exposure findings.
    """
    with sqlite3.connect(test_db) as conn:
        conn.execute("INSERT INTO breach_checks (email_checked, breach_name, acknowledged) VALUES (?, ?, 0)", ("user@example.com", "TestBreach"))
        conn.execute("INSERT INTO exposure_findings (source, item_id, item_name, exposure_type) VALUES (?, ?, ?, ?)", ("drive", "doc123", "Confidential Spec", "public"))
        conn.commit()

    guide_breach = remediation.get_remediation_guide("breach", 1, test_db)
    assert guide_breach["finding_type"] == "breach"
    assert "user@example.com" in guide_breach["steps"][0]

    guide_exp = remediation.get_remediation_guide("exposure", 1, test_db)
    assert guide_exp["finding_type"] == "exposure"
    assert "Confidential Spec" in guide_exp["title"]


def test_get_remediation_guide_invalid_id(test_db):
    """
    Verify ValueError raised when finding ID is not found.
    """
    with pytest.raises(ValueError, match="not found"):
        remediation.get_remediation_guide("secret", 999, test_db)


def test_fetch_dependabot_alerts(test_db):
    """
    Verify fetching Dependabot alerts from GitHub API and saving to dependency_alerts SQLite table.
    """
    mock_alerts_json = [
        {
            "state": "open",
            "security_vulnerability": {
                "package": {"name": "urllib3"},
                "severity": "high",
            },
            "html_url": "https://github.com/advisories/GHSA-1234",
        }
    ]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_alerts_json
    mock_response.raise_for_status.return_value = None

    mock_http_client = MagicMock()
    mock_http_client.get.return_value = mock_response

    mock_httpx_cls = MagicMock()
    mock_httpx_cls.return_value.__enter__.return_value = mock_http_client

    with patch("keyring_utils.get_credential", return_value="mock_pat"), patch("httpx.Client", mock_httpx_cls):
        alerts = dep_watch.fetch_dependabot_alerts("owner/repo", db_path=test_db)

    assert len(alerts) == 1
    assert alerts[0]["package_name"] == "urllib3"
    assert alerts[0]["severity"] == "high"

    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM dependency_alerts WHERE repo_name = ?", ("owner/repo",))
        row = cur.fetchone()
        assert row is not None
        assert row["package_name"] == "urllib3"


# --- FastAPI Remediation Endpoint Tests ---

client = TestClient(app)


def test_fastapi_get_remediation(test_db, monkeypatch):
    """
    Test GET /remediation/{finding_type}/{finding_id} FastAPI route.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)

    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO twofa_audit (provider, twofa_enabled) VALUES ('github', 0)"
        )
        conn.commit()

    res = client.get("/remediation/twofa/1")
    assert res.status_code == 200
    data = res.json()
    assert data["finding_type"] == "twofa"
    assert "github" in data["title"].lower()
    assert len(data["steps"]) >= 4
