"""
Canonical Security Score tests (TRD Section 4, Backend Schema Section 3, Acceptance Criteria Section 1).

recompute_security_score() is the single calculation path and the only writer of
security_score_current; dashboard, chat, briefing, and GET /score must read that row.
"""

import inspect
import json
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import security_score
import services.briefing as briefing
import services.conversational_router as conversational_router
import services.dashboard_state as dashboard_state
from main import app, init_db
from services.github_intelligence import (
    init_github_tables,
    process_github_webhook_event,
    sync_github_intelligence,
)

client = TestClient(app)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "canonical_score_evie.db")
    init_db(db_file)
    init_github_tables(db_file)
    security_score.init_score_history_table(db_file)
    monkeypatch.setattr("main.DB_PATH", db_file)
    return db_file


def _insert_secret(db_file, file_path="config.py", status="open"):
    with sqlite3.connect(db_file) as conn:
        cur = conn.execute(
            "INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status) "
            "VALUES ('org/repo', ?, ?, 'API Key', '[REDACTED]', ?)",
            (f"sha-{file_path}", file_path, status),
        )
        conn.commit()
        return cur.lastrowid


def _insert_zero_score_findings(db_file):
    """3 secrets (-45) + 3 breaches (-30) + 3 exposures (-30) -> floor 0."""
    with sqlite3.connect(db_file) as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO secret_findings (repo_name, commit_sha, file_path, status) VALUES ('org/repo', ?, 'f.py', 'open')",
                (f"sha{i}",),
            )
            conn.execute(
                "INSERT INTO breach_checks (email_checked, breach_name, acknowledged) VALUES ('u@example.com', ?, 0)",
                (f"Breach{i}",),
            )
            conn.execute(
                "INSERT INTO exposure_findings (source, item_id, item_name, exposure_type, status) VALUES ('drive', ?, 'doc', 'public', 'open')",
                (f"item{i}",),
            )
        conn.commit()


def _current_row(db_file):
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM security_score_current").fetchall()


def _history_count(db_file):
    with sqlite3.connect(db_file) as conn:
        return conn.execute("SELECT COUNT(*) FROM security_score_history").fetchone()[0]


def _briefing(db_file):
    with patch("keyring_utils.get_credential", return_value="mock_token"), patch(
        "services.briefing.fetch_today_calendar_events", return_value=[]
    ):
        return briefing.generate_briefing(db_file, trigger="on_demand")


def _chat_score(db_file):
    resp = client.post("/chat", json={"message": "What is my security score?"})
    assert resp.status_code == 200
    return resp.json()


# 1. Canonical score is written to security_score_current
def test_recompute_writes_single_current_row(db):
    _insert_secret(db)
    result = security_score.recompute_security_score(db)

    rows = _current_row(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == 1
    assert row["score"] == 85 == result["score"]
    assert row["grade"] == result["grade"] == "A"
    assert row["secrets_deduction"] == -15
    assert row["breaches_deduction"] == 0
    assert row["exposure_deduction"] == 0
    assert row["twofa_deduction"] == 0
    assert row["dependencies_deduction"] == 0
    assert row["active_findings_count"] == 1
    assert json.loads(row["counts"])["open_secrets_count"] == 1

    # Recomputing updates the same row in place
    security_score.recompute_security_score(db)
    assert len(_current_row(db)) == 1


# 2-5. Dashboard, chat, briefing, GET /score read the canonical score and agree
def test_all_surfaces_report_identical_canonical_score(db):
    _insert_secret(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO breach_checks (email_checked, breach_name, acknowledged) VALUES ('u@example.com', 'B', 0)")
        conn.commit()
    canonical = security_score.recompute_security_score(db)
    assert canonical["score"] == 75
    assert canonical["grade"] == "B"

    score_api = client.get("/score").json()
    dashboard = client.get("/dashboard/state").json()
    chat = _chat_score(db)
    brief = _briefing(db)

    assert score_api["score"] == canonical["score"]
    assert score_api["grade"] == canonical["grade"]

    assert dashboard["security_score"]["score"] == canonical["score"]
    assert dashboard["security_score"]["grade"] == canonical["grade"]
    assert dashboard["security_score"]["breakdown"] == canonical["breakdown"]
    assert dashboard["overall_status"] == dashboard_state.derive_overall_status(canonical["score"])
    assert dashboard["security_findings_summary"]["open_secrets_count"] == 1
    assert dashboard["security_findings_summary"]["unacknowledged_breaches_count"] == 1
    assert dashboard["security_findings_summary"]["total_open_findings"] == canonical["active_findings_count"]

    assert chat["score"]["score"] == canonical["score"]
    assert chat["score"]["grade"] == canonical["grade"]
    assert f"{canonical['score']}/100 (Grade: {canonical['grade']})" in chat["reply"]
    assert "Secrets: 1" in chat["reply"]
    assert "Breaches: 1" in chat["reply"]

    sec = brief["sections"]["security_findings"]
    assert sec["score"] == canonical["score"]
    assert sec["grade"] == canonical["grade"]

    report = client.post("/chat", json={"message": "What is the daily report?"}).json()
    assert f"Security Score: {canonical['score']}/100 (Grade: {canonical['grade']})" in report["reply"]

    with patch("keyring_utils.get_credential", return_value="mock_token"), patch(
        "services.briefing.fetch_today_calendar_events", return_value=[]
    ):
        wake = client.post("/chat", json={"message": "Good morning Evie"}).json()
    assert wake["briefing"]["sections"]["security_findings"]["score"] == canonical["score"]
    assert f"Security Score: {canonical['score']}/100 (Grade: {canonical['grade']})" in wake["reply"]


# 6. Score 0 remains 0 everywhere
def test_zero_score_is_preserved_on_every_surface(db):
    _insert_zero_score_findings(db)
    canonical = security_score.recompute_security_score(db)
    assert canonical["score"] == 0

    assert _current_row(db)[0]["score"] == 0
    assert client.get("/score").json()["score"] == 0

    dashboard = client.get("/dashboard/state").json()
    assert dashboard["security_score"]["score"] == 0
    assert dashboard["overall_status"] == "Critical"

    chat = _chat_score(db)
    assert chat["score"]["score"] == 0
    assert "0/100" in chat["reply"]

    assert _briefing(db)["sections"]["security_findings"]["score"] == 0


def test_frontend_uses_canonical_grade_and_explicit_null_checks():
    js = client.get("/static/app.js").text
    assert "score || 100" not in js
    assert "secScore.score !== null && secScore.score !== undefined" in js
    assert "secScore.grade" in js
    # No client-side grade derivation from the numeric score
    assert "scoreVal >= 95" not in js


# 7. Finding creation triggers recomputation
def test_webhook_secret_finding_creation_recomputes_score(db):
    security_score.recompute_security_score(db)
    assert _current_row(db)[0]["score"] == 100

    fake_finding = [{"file_path": "app.py", "line_number": 3, "secret_type": "API Key", "redacted_value": "[REDACTED]"}]
    with patch("integrations.github_watch.SecretScanner.scan_diff", return_value=fake_finding):
        res = process_github_webhook_event(
            {
                "event_type": "push",
                "repository": {"full_name": "org/repo"},
                "commits": [{"id": "abc123", "message": "add config", "author": {"name": "dev"}}],
            },
            db,
        )
    assert res["secrets_found"] == 1
    row = _current_row(db)[0]
    assert row["score"] == 85
    assert row["active_findings_count"] == 1


# 8. Finding resolution triggers recomputation
def test_resolve_endpoint_recomputes_score(db):
    finding_id = _insert_secret(db)
    security_score.recompute_security_score(db)
    assert _current_row(db)[0]["score"] == 85

    resp = client.post(f"/findings/{finding_id}/resolve", json={"finding_type": "secret", "status": "resolved"})
    assert resp.status_code == 200
    assert resp.json()["score"]["score"] == 100
    assert _current_row(db)[0]["score"] == 100
    assert client.get("/dashboard/state").json()["security_score"]["score"] == 100


def test_resolve_endpoint_rejects_unknown_finding(db):
    resp = client.post("/findings/9999/resolve", json={"finding_type": "secret"})
    assert resp.status_code == 404
    resp = client.post("/findings/1/resolve", json={"finding_type": "twofa"})
    assert resp.status_code == 400


def test_sync_stale_finding_resolution_recomputes_score(db):
    _insert_secret(db, file_path="package-lock.json")
    security_score.recompute_security_score(db)
    assert _current_row(db)[0]["score"] == 85

    with patch("services.github_intelligence.discover_user_repositories_detailed", return_value=[]):
        sync_github_intelligence(db)

    assert _current_row(db)[0]["score"] == 100


# 9. History is recorded per recompute, in schema shape
def test_history_recorded_per_recompute(db):
    security_score.recompute_security_score(db)
    _insert_secret(db)
    result = security_score.recompute_security_score(db)

    assert _history_count(db) == 2
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        last = conn.execute("SELECT * FROM security_score_history ORDER BY id DESC LIMIT 1").fetchone()
    assert last["score"] == 85
    assert json.loads(last["breakdown"]) == result["breakdown"]
    assert result["trend"] == "declining"

    # Reads never append history
    client.get("/score")
    client.get("/dashboard/state")
    _chat_score(db)
    _briefing(db)
    assert _history_count(db) == 2


# 10. No consumer independently recalculates the score
def test_consumers_read_stored_row_and_never_recalculate(db, monkeypatch):
    security_score.recompute_security_score(db)
    assert _current_row(db)[0]["score"] == 100

    # Change finding state WITHOUT recomputing; consumers must keep reporting the stored row.
    _insert_secret(db)

    def _forbidden(*args, **kwargs):
        raise AssertionError("consumer attempted to recalculate the score")

    monkeypatch.setattr(security_score, "recompute_security_score", _forbidden)
    monkeypatch.setattr(security_score, "compute_security_score", _forbidden)

    assert client.get("/score").json()["score"] == 100
    assert client.get("/dashboard/state").json()["security_score"]["score"] == 100
    assert _chat_score(db)["score"]["score"] == 100
    assert _briefing(db)["sections"]["security_findings"]["score"] == 100


def test_consumer_sources_do_not_call_score_calculation():
    for module in (dashboard_state, briefing, conversational_router):
        src = inspect.getsource(module)
        assert "compute_security_score(" not in src, module.__name__
        assert "get_current_security_score(" in src, module.__name__
