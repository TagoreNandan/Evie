"""
Shared Memory & RAG Search Engine for Evie Assistant.

Implements MEM-01, MEM-02 & Section 3 of 02_TRD.md:
- Daily journal entry ingestion & SQLite storage (journal_entries).
- Vector embedding generation & indexing (journal_embeddings).
- Natural-language hybrid RAG recall & date-range filtering.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
import numpy as np

import redaction


def init_memory_tables(db_path: str = "evie.db") -> None:
    """
    Ensure journal_entries and journal_embeddings tables exist in SQLite database per 04_Backend_Schema.md.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date DATE NOT NULL,
                content TEXT NOT NULL,
                source_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_embeddings (
                entry_id INTEGER PRIMARY KEY,
                entry_date DATE NOT NULL,
                embedding_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()


def compute_embedding(text: str, dim: int = 128) -> list[float]:
    """
    Deterministically compute a dense vector embedding for text using term & n-gram feature hashing.
    """
    words = re.findall(r"\w+", text.lower())
    if not words:
        return [0.0] * dim

    vec = np.zeros(dim, dtype=np.float32)
    for word in words:
        # Word hashing
        h = abs(hash(word)) % dim
        vec[h] += 1.0
        # Character 3-gram hashing
        for i in range(len(word) - 2):
            tri = word[i : i + 3]
            h_tri = abs(hash(tri)) % dim
            vec[h_tri] += 0.5

    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm

    return vec.tolist()


def add_journal_entry(
    entry_date: str,
    content: str,
    source_data: dict | None = None,
    db_path: str = "evie.db",
) -> dict:
    """
    Store a daily journal entry and index its vector embedding (MEM-01).
    """
    init_memory_tables(db_path)

    # 1. Validation
    if not entry_date or not isinstance(entry_date, str):
        raise ValueError("entry_date must be a valid ISO string (YYYY-MM-DD).")
    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError("Invalid entry_date format. Expected YYYY-MM-DD.") from e

    if not content or not isinstance(content, str) or len(content.strip()) == 0:
        raise ValueError("Journal content must be a non-empty string.")

    # 2. Secret Redaction Safety Check
    safe_content = content
    if redaction.is_sensitive_key("content"):
        safe_content = redaction.redact_secret(content)
    # If source_data provided, redact secrets recursively
    safe_source_data = redaction.redact_dict_secrets(source_data) if source_data else None

    source_data_json = json.dumps(safe_source_data) if safe_source_data else None

    # 3. Database Insertion
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO journal_entries (entry_date, content, source_data)
            VALUES (?, ?, ?)
            """,
            (entry_date, safe_content, source_data_json),
        )
        entry_id = cursor.lastrowid
        conn.commit()

    # 4. Vector Embedding Generation & Indexing
    embedding = compute_embedding(safe_content)
    embedding_json = json.dumps(embedding)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO journal_embeddings (entry_id, entry_date, embedding_json)
            VALUES (?, ?, ?)
            """,
            (entry_id, entry_date, embedding_json),
        )
        conn.commit()

    return {
        "id": entry_id,
        "entry_date": entry_date,
        "content": safe_content,
        "source_data": safe_source_data,
        "status": "created",
    }


def search_journal_entries(
    query: str,
    top_k: int = 5,
    start_date: str | None = None,
    end_date: str | None = None,
    db_path: str = "evie.db",
) -> list[dict]:
    """
    Search journal entries using vector cosine similarity and date filtering (MEM-02).
    """
    init_memory_tables(db_path)

    if not query or not isinstance(query, str) or len(query.strip()) == 0:
        raise ValueError("Search query must be a non-empty string.")

    # 1. Date Validation if provided
    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError("Invalid start_date format. Expected YYYY-MM-DD.") from e
    if end_date:
        try:
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError("Invalid end_date format. Expected YYYY-MM-DD.") from e

    query_vec = np.array(compute_embedding(query), dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)

    # 2. SQL Query Execution with Date Filters
    sql = """
        SELECT e.id, e.entry_date, e.content, e.source_data, em.embedding_json
        FROM journal_entries e
        JOIN journal_embeddings em ON e.id = em.entry_id
        WHERE 1=1
    """
    params = []
    if start_date:
        sql += " AND e.entry_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND e.entry_date <= ?"
        params.append(end_date)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return []

    # 3. Cosine Similarity Ranking
    candidates = []
    for row in rows:
        stored_vec = np.array(json.loads(row["embedding_json"]), dtype=np.float32)
        stored_norm = np.linalg.norm(stored_vec)

        if query_norm > 0 and stored_norm > 0:
            score = float(np.dot(query_vec, stored_vec) / (query_norm * stored_norm))
        else:
            score = 0.0

        source_data = json.loads(row["source_data"]) if row["source_data"] else None
        candidates.append(
            {
                "id": row["id"],
                "entry_date": row["entry_date"],
                "content": row["content"],
                "source_data": source_data,
                "score": round(score, 4),
            }
        )

    # Sort descending by score
    candidates.sort(key=lambda x: x["score"], reverse=True)

    return candidates[:top_k]
