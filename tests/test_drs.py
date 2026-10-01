"""
Unit and Integration Tests for Decision Research Support (DRS) Agent (`drs.py`).
"""

import os
import sqlite3
import pytest
from fastapi.testclient import TestClient

import drs
from main import app

TEST_DB = "test_drs_evie.db"


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    drs.init_drs_table(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def mock_search_success(query: str):
    return [
        {
            "title": "Security Research Paper",
            "snippet": "Analysis shows that multi-factor authentication significantly reduces unauthorized access risk.",
            "url": "https://example.com/research1",
        },
        {
            "title": "Identity Protection Guide",
            "snippet": "Study suggests password managers lower credential reuse frequency.",
            "url": "https://example.com/guide2",
        },
    ]


def mock_search_empty(query: str):
    return []


def test_ask_drs_question_success():
    result = drs.ask_drs_question(
        question="Should I implement 2FA?",
        db_path=TEST_DB,
        search_fn=mock_search_success,
    )
    assert result["status"] == "success"
    assert result["question"] == "Should I implement 2FA?"
    assert "https://example.com/research1" in result["sources"]
    assert result["disclaimer"] == drs.DISCLAIMER_TEXT
    assert len(result["evidence_points"]) == 2

    # Check database persistence
    with sqlite3.connect(TEST_DB) as conn:
        cursor = conn.execute("SELECT question, status FROM drs_queries")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "Should I implement 2FA?"
        assert row[1] == "success"


def test_ask_drs_question_insufficient_evidence():
    result = drs.ask_drs_question(
        question="What is the undisclosed zero-day for non-existent service X?",
        db_path=TEST_DB,
        search_fn=mock_search_empty,
    )
    assert result["status"] == "insufficient_evidence"
    assert result["sources"] == []
    assert result["evidence_points"] == []
    assert result["disclaimer"] == drs.DISCLAIMER_TEXT
    assert "Insufficient search evidence" in result["summary"]


def test_ask_drs_question_empty_question_raises():
    with pytest.raises(ValueError, match="cannot be empty"):
        drs.ask_drs_question("   ", db_path=TEST_DB)


def test_drs_ask_api_endpoint(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    client = TestClient(app)

    response = client.post("/drs/ask", json={"question": "What is security health scoring?"})
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "disclaimer" in data
    assert data["disclaimer"] == drs.DISCLAIMER_TEXT
