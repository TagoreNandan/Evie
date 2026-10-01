"""
Decision Research Support (DRS) Agent Module for Evie Assistant.

Implements DRS-01 & Section 8 of 02_TRD.md:
- Evidence-based research query synthesis with web link attribution.
- Mandatory non-definitive disclaimer framing on all responses.
- Handling of empty/failed search results without hallucination (status="insufficient_evidence").
- SQLite persistence in drs_queries table.
"""

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional

DISCLAIMER_TEXT = "Note: This analysis represents evidence-based support from available sources, not a definitive verdict."
ABSOLUTE_WORDS = ["definitely", "undoubtedly", "100% proven", "unquestionably", "certainly"]


def init_drs_table(db_path: str = "evie.db") -> None:
    """
    Initialize drs_queries SQLite table per 04_Backend_Schema.md.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drs_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer_summary TEXT,
                sources TEXT,
                status TEXT DEFAULT 'success',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


def _sanitize_framing(text: str) -> str:
    """
    Ensure no absolute words are present in generated output strings.
    """
    sanitized = text
    for word in ABSOLUTE_WORDS:
        sanitized = sanitized.replace(word, "indicates potential")
        sanitized = sanitized.replace(word.capitalize(), "Indicates potential")
    return sanitized


def ask_drs_question(
    question: str,
    db_path: str = "evie.db",
    search_fn: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    Execute evidence-based Decision Research Support query.

    Args:
        question: User query string.
        db_path: Path to SQLite database.
        search_fn: Optional custom web search function returning list of source dicts
                   e.g. [{"title": ..., "snippet": ..., "url": ...}].

    Returns:
        Structured response dictionary with disclaimer and cited sources.
    """
    if not question or not question.strip():
        raise ValueError("Question cannot be empty.")

    init_drs_table(db_path)
    clean_question = question.strip()

    # 1. Retrieve search evidence
    search_results: List[Dict[str, Any]] = []
    if search_fn is not None:
        try:
            search_results = search_fn(clean_question) or []
        except Exception:
            search_results = []

    # 2. Handle empty or missing search evidence (no hallucinations)
    if not search_results:
        summary_text = "Insufficient search evidence found to answer this question."
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO drs_queries (question, answer_summary, sources, status)
                VALUES (?, ?, ?, ?)
                """,
                (clean_question, summary_text, json.dumps([]), "insufficient_evidence"),
            )
            conn.commit()

        return {
            "question": clean_question,
            "status": "insufficient_evidence",
            "summary": summary_text,
            "evidence_points": [],
            "sources": [],
            "disclaimer": DISCLAIMER_TEXT,
        }

    # 3. Synthesize evidence & extract sources
    evidence_points = []
    sources = []
    for item in search_results:
        snippet = item.get("snippet") or item.get("summary", "")
        url = item.get("url") or item.get("link", "")
        if snippet:
            evidence_points.append(_sanitize_framing(snippet))
        if url and url not in sources:
            sources.append(url)

    summary_raw = f"Based on {len(search_results)} available source(s), evidence indicates: " + " ".join(evidence_points[:2])
    summary = _sanitize_framing(summary_raw)

    # 4. Log successful query
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO drs_queries (question, answer_summary, sources, status)
            VALUES (?, ?, ?, ?)
            """,
            (clean_question, summary, json.dumps(sources), "success"),
        )
        conn.commit()

    return {
        "question": clean_question,
        "status": "success",
        "summary": summary,
        "evidence_points": evidence_points,
        "sources": sources,
        "disclaimer": DISCLAIMER_TEXT,
    }
