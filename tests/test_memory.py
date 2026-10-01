"""
Unit tests for Shared Memory & RAG Search Engine (memory.py).
Tests MEM-01, MEM-02, journal ingestion, vector embedding indexing, semantic search, date filtering, and FastAPI endpoints.
"""

import sqlite3
import pytest
import memory
from main import app, DB_PATH
from fastapi.testclient import TestClient


@pytest.fixture
def test_db(tmp_path):
    """
    Create a temporary SQLite database for memory testing.
    """
    db_file = str(tmp_path / "test_memory_evie.db")
    memory.init_memory_tables(db_file)
    return db_file


def test_add_journal_entry_success(test_db):
    """
    Verify creating a daily journal entry creates rows in journal_entries and journal_embeddings.
    """
    res = memory.add_journal_entry(
        entry_date="2026-09-19",
        content="Configured OS Keyring credential manager for storing API keys securely.",
        source_data={"topic": "keyring"},
        db_path=test_db,
    )

    assert res["status"] == "created"
    assert res["entry_date"] == "2026-09-19"
    entry_id = res["id"]

    with sqlite3.connect(test_db) as conn:
        conn.row_factory = sqlite3.Row
        cur1 = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (entry_id,))
        row1 = cur1.fetchone()
        assert row1 is not None
        assert row1["entry_date"] == "2026-09-19"
        assert "OS Keyring" in row1["content"]

        cur2 = conn.execute("SELECT * FROM journal_embeddings WHERE entry_id = ?", (entry_id,))
        row2 = cur2.fetchone()
        assert row2 is not None
        assert row2["entry_date"] == "2026-09-19"
        assert row2["embedding_json"] is not None


def test_add_journal_entry_input_validation(test_db):
    """
    Verify input validation rejects bad date formats and empty content.
    """
    with pytest.raises(ValueError, match="Invalid entry_date format"):
        memory.add_journal_entry("2026/09/19", "Content", db_path=test_db)

    with pytest.raises(ValueError, match="non-empty string"):
        memory.add_journal_entry("2026-09-19", "   ", db_path=test_db)


def test_search_journal_entries_semantic_similarity(test_db):
    """
    Verify vector similarity ranking returns the most relevant historical entries.
    """
    memory.add_journal_entry("2026-09-15", "Configured OS Keyring credential manager for API tokens.", db_path=test_db)
    memory.add_journal_entry("2026-09-18", "Built Google Calendar proposal and confirmation flow.", db_path=test_db)
    memory.add_journal_entry("2026-09-19", "Implemented daily briefing aggregator and GitHub webhooks.", db_path=test_db)

    # Query 1: Keyring credentials
    res1 = memory.search_journal_entries("Keyring credential storage", top_k=2, db_path=test_db)
    assert len(res1) > 0
    assert res1[0]["entry_date"] == "2026-09-15"
    assert "Keyring" in res1[0]["content"]

    # Query 2: Calendar scheduling
    res2 = memory.search_journal_entries("Google Calendar scheduling", top_k=2, db_path=test_db)
    assert len(res2) > 0
    assert res2[0]["entry_date"] == "2026-09-18"
    assert "Calendar" in res2[0]["content"]


def test_search_journal_entries_date_range_filtering(test_db):
    """
    Verify start_date and end_date strictly filter candidates.
    """
    memory.add_journal_entry("2026-09-10", "Initial setup and repository structure.", db_path=test_db)
    memory.add_journal_entry("2026-09-18", "Google Calendar integration completed.", db_path=test_db)
    memory.add_journal_entry("2026-09-19", "FastAPI RAG search endpoints created.", db_path=test_db)

    # Search with date restriction 2026-09-18 to 2026-09-19
    results = memory.search_journal_entries(
        query="setup and integration",
        start_date="2026-09-18",
        end_date="2026-09-19",
        db_path=test_db,
    )

    dates = [r["entry_date"] for r in results]
    assert "2026-09-10" not in dates
    assert "2026-09-18" in dates or "2026-09-19" in dates


# --- FastAPI Memory Endpoint Tests ---

client = TestClient(app)


def test_fastapi_journal_entry_and_search(test_db, monkeypatch):
    """
    Test POST /journal/entry and GET /journal/search FastAPI endpoints.
    """
    monkeypatch.setattr("main.DB_PATH", test_db)

    # 1. Post entry endpoint
    post_payload = {
        "entry_date": "2026-09-19",
        "content": "Tested FastAPI RAG memory recall system.",
        "source_data": {"author": "somespecies"},
    }
    res_post = client.post("/journal/entry", json=post_payload)
    assert res_post.status_code == 200
    data_post = res_post.json()
    assert data_post["status"] == "created"
    assert data_post["entry_date"] == "2026-09-19"

    # 2. Search endpoint
    res_search = client.get("/journal/search?q=RAG+memory+recall")
    assert res_search.status_code == 200
    data_search = res_search.json()
    assert isinstance(data_search, list)
    assert len(data_search) > 0
    assert data_search[0]["entry_date"] == "2026-09-19"
    assert "RAG memory" in data_search[0]["content"]
