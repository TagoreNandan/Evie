"""
`computer_control_steps` storage (docs/04 Backend Schema Section 4): one redacted row per attempted
computer-control decision, written by the orchestrator's audit port. Audit data only - never read back to decide
anything. Records are built redacted in services/computer_control/audit.py; secret-named keys are redacted again
here. Never stored: AX references, screenshots, typed text, document contents, secure-field anything.
"""

import json
import sqlite3

import redaction
from services.computer_control.audit import StepAuditRecord

DDL = """
CREATE TABLE IF NOT EXISTS computer_control_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    op TEXT,
    decision_source TEXT NOT NULL,
    target_summary TEXT,
    gate_decision TEXT NOT NULL,
    status TEXT NOT NULL,
    execution TEXT,
    verification TEXT,
    mechanism TEXT,
    ax_error INTEGER,
    latency_ms REAL,
    state_layers TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
COLUMNS = ("session_id", "step", "op", "decision_source", "target_summary", "gate_decision", "status", "execution",
           "verification", "mechanism", "ax_error", "latency_ms", "state_layers", "tokens_in", "tokens_out",
           "cost_usd")


def init_computer_control_steps_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(DDL)


class SqliteStepLog:
    """The orchestrator's audit port: ready() before any dispatch, write() once per attempted decision."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        init_computer_control_steps_table(db_path)

    def ready(self) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("SELECT 1 FROM computer_control_steps LIMIT 1").fetchall()
        return True

    def write(self, record: StepAuditRecord) -> None:
        row = record.model_dump()
        summary = row["target_summary"]
        row["target_summary"] = json.dumps(redaction.redact_dict_secrets(summary), sort_keys=True) \
            if summary is not None else None
        row["state_layers"] = json.dumps(row["state_layers"])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"INSERT INTO computer_control_steps ({', '.join(COLUMNS)}) "
                         f"VALUES ({', '.join('?' for _ in COLUMNS)})", [row[c] for c in COLUMNS])
