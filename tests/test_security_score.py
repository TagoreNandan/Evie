"""
Unit tests for Security Health Score Aggregator (security_score.py).
Tests SCORE-01, score baseline, penalty deductions, score floor clamping at 0, historical trend logging, and FastAPI GET /score endpoint.
"""

import sqlite3
import pytest
import security_score
from main import app, DB_PATH
from fastapi.testclient import TestClient


@pytest.fixture
def test_db(tmp_path):
    """
    Create temporary SQLite database pre-populated with Phase 1-4 tables.
    """
    db_file = str(tmp_path / "test_score_evie.db")
    security_score.init_score_history_table(db_file)

    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS secret_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS breach_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_checked TEXT NOT NULL,
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
                status TEXT DEFAULT 'open'
            );
            """
        )
        conn.commit()

    return db_file


def test_compute_security_score_baseline(test_db):
    """
    Verify baseline score = 100 when no open findings exist.
    """
    res = security_score.compute_security_score(test_db)
    assert res["score"] == 100
    assert res["trend"] == "stable"
    assert res["breakdown"] == {"secrets": 0, "breaches": 0, "exposures": 0, "twofa": 0, "dependencies": 0}


def test_compute_security_score_deductions(test_db):
    """
    Verify deductions: 1 open secret (-15), 1 breach (-10) -> score 75.
    """
    with sqlite3.connect(test_db) as conn:
        conn.execute("INSERT INTO secret_findings (repo_name, commit_sha, file_path, status) VALUES ('repo', 'sha', 'file', 'open')")
        conn.execute("INSERT INTO breach_checks (email_checked, acknowledged) VALUES ('user@example.com', 0)")
        conn.commit()

    res = security_score.compute_security_score(test_db)
    assert res["score"] == 75
    assert res["breakdown"]["secrets"] == -15
    assert res["breakdown"]["breaches"] == -10


def test_compute_security_score_floor(test_db):
    """
    Verify score floor clamping at 0 despite multiple active findings.
    """
    with sqlite3.connect(test_db) as conn:
        # Insert 10 open secrets (-45 cap)
        for i in range(10):
            conn.execute("INSERT INTO secret_findings (repo_name, commit_sha, file_path, status) VALUES ('repo', 'sha', 'file', 'open')")
        # Insert 10 breaches (-30 cap)
        for i in range(10):
            conn.execute("INSERT INTO breach_checks (email_checked, acknowledged) VALUES ('user@example.com', 0)")
        # Insert 10 exposures (-30 cap)
        for i in range(10):
            conn.execute("INSERT INTO exposure_findings (source, item_id, status) VALUES ('drive', 'item', 'open')")
        conn.commit()

    res = security_score.compute_security_score(test_db)
    assert res["score"] == 0


def test_compute_security_score_trend(test_db):
    """
    Verify trend detection (improving, declining, stable).
    """
    # First computation: score 100
    res1 = security_score.compute_security_score(test_db, log_history=True)
    assert res1["score"] == 100

    # Insert finding -> score drops to 85 -> declining
    with sqlite3.connect(test_db) as conn:
        conn.execute("INSERT INTO secret_findings (repo_name, commit_sha, file_path, status) VALUES ('repo', 'sha', 'file', 'open')")
        conn.commit()

    res2 = security_score.compute_security_score(test_db, log_history=True)
    assert res2["score"] == 85
    assert res2["trend"] == "declining"

    # Resolve finding -> score increases to 100 -> improving
    with sqlite3.connect(test_db) as conn:
        conn.execute("UPDATE secret_findings SET status = 'resolved'")
        conn.commit()

    res3 = security_score.compute_security_score(test_db, log_history=True)
    assert res3["score"] == 100
    assert res3["trend"] == "improving"


# --- FastAPI Score Endpoint Test ---

client = TestClient(app)


def test_fastapi_get_score(test_db, monkeypatch):
    """
    Test GET /score FastAPI route.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)

    res = client.get("/score")
    assert res.status_code == 200
    data = res.json()
    assert "score" in data
    assert "current_score" in data
    assert "grade" in data
    assert "active_findings_count" in data
    assert "breakdown" in data
    assert "trend" in data
    assert data["score"] == data["current_score"]
    assert data["grade"] == "A"


def test_score_grade_mathematical_consistency(test_db):
    """
    Verify score, grade, and active_findings_count remain strictly consistent.
    """
    with sqlite3.connect(test_db) as conn:
        # 5 secrets (-45 deduction cap)
        for i in range(5):
            conn.execute("INSERT INTO secret_findings (repo_name, commit_sha, file_path, status) VALUES ('repo', 'sha', 'file', 'open')")
        conn.commit()

    res = security_score.compute_security_score(test_db)
    assert res["score"] == 55
    assert res["current_score"] == 55
    assert res["grade"] == "D"
    assert res["active_findings_count"] == 5

