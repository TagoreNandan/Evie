"""
Voice command audit log (04_Backend_Schema.md Section 4, 06_Acceptance_Criteria.md Section 7).

Every /voice/command request is recorded in voice_command_log with its resolution outcome.
Stored: credential-redacted transcript, resolved tool, validated parameters, speaker
verification outcome, executed flag, rejection reason. Never stored: audio, embeddings,
or similarity scores.
"""

import json
import sqlite3
from typing import Any, Dict

from services.llm_client import redact_text


def init_voice_command_log_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_command_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_transcript TEXT NOT NULL,
                resolved_tool TEXT,
                resolved_parameters TEXT,
                speaker_verified INTEGER,
                executed INTEGER DEFAULT 0,
                rejected_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


def log_voice_command(db_path: str, transcript: str, result: Dict[str, Any], audit: Dict[str, Any]) -> None:
    tool = result.get("tool") or {}
    status = tool.get("status")
    parameters = audit.get("resolved_parameters")
    init_voice_command_log_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO voice_command_log
                (raw_transcript, resolved_tool, resolved_parameters, speaker_verified, executed, rejected_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                redact_text(transcript),
                tool.get("name"),
                redact_text(json.dumps(parameters)) if parameters is not None else None,
                audit.get("speaker_verified"),
                1 if status == "executed" else 0,
                tool.get("reason") if status in ("rejected", "blocked", "failed") else None,
            ),
        )
        conn.commit()
