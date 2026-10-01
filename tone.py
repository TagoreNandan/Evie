"""
Assistant Personality & Tone Configuration Module for Evie Assistant.

Implements TONE-01 & Section 4 of 02_TRD.md:
- Persists user conversational tone preference in SQLite user_config table (id = 1).
- Supports tone settings: 'neutral', 'casual', 'formal', 'humorous'.
- Dynamic formatting transformation of assistant responses via apply_tone().
"""

import sqlite3
from typing import Optional

VALID_TONES = {"neutral", "casual", "formal", "humorous"}


def init_user_config_table(db_path: str = "evie.db") -> None:
    """
    Initialize user_config SQLite table with default single-row configuration (id = 1).
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                tone_preference TEXT DEFAULT 'neutral',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_config (id, tone_preference) VALUES (1, 'neutral');
            """
        )
        conn.commit()


def get_tone_preference(db_path: str = "evie.db") -> str:
    """
    Retrieve active tone preference from user_config table.
    """
    init_user_config_table(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT tone_preference FROM user_config WHERE id = 1;")
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
    return "neutral"


def set_tone_preference(tone: str, db_path: str = "evie.db") -> str:
    """
    Update tone preference in user_config table.

    Args:
        tone: Tone selection ('neutral', 'casual', 'formal', 'humorous').
        db_path: Path to SQLite database.

    Returns:
        Updated tone preference string.
    """
    init_user_config_table(db_path)
    clean_tone = tone.lower().strip() if tone else ""
    if clean_tone not in VALID_TONES:
        raise ValueError(f"Invalid tone preference. Must be one of: {', '.join(sorted(VALID_TONES))}")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE user_config
            SET tone_preference = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1;
            """,
            (clean_tone,),
        )
        conn.commit()

    return clean_tone


def apply_tone(text: str, tone: Optional[str] = None, db_path: str = "evie.db") -> str:
    """
    Apply conversational tone formatting to target text.

    Args:
        text: Input assistant output string.
        tone: Optional tone override. If None, fetches active preference from db.
        db_path: Path to SQLite database.

    Returns:
        Tone-formatted text string.
    """
    if not text:
        return ""

    target_tone = tone.lower().strip() if tone else get_tone_preference(db_path)
    clean_text = text.strip()

    if target_tone == "casual":
        return f"Hey there! {clean_text} Hope that helps!"
    elif target_tone == "formal":
        return f"Please be advised: {clean_text} Best regards."
    elif target_tone == "humorous":
        return f"Pro tip: {clean_text} (Don't quote me on that!)"
    else:  # neutral
        return clean_text
