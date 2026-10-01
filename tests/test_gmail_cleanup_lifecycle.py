"""
Gmail cleanup proposal lifecycle (Acceptance Criteria 5.2 and 5.3): 15-minute expiry and
atomic, single-winner confirmation, matching the calendar proposal pattern.
"""

import datetime
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import services.gmail_intelligence as gi
from main import app
from services.gmail_intelligence import confirm_gmail_cleanup_action, init_gmail_tables, propose_gmail_cleanup_action

client = TestClient(app)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "gmail_cleanup.db")
    init_gmail_tables(path)
    monkeypatch.setattr(main, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def no_external_gmail_action(monkeypatch):
    """The lifecycle change must never reach Gmail (or any HTTP API)."""
    def forbidden(*args, **kwargs):
        raise AssertionError("external HTTP call attempted by cleanup confirmation")
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def _status(db, token):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT status FROM gmail_cleanup_proposals WHERE proposal_token = ?", (token,)).fetchone()[0]


def _age(db, token, minutes):
    created = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE gmail_cleanup_proposals SET created_at = ? WHERE proposal_token = ?", (created, token))
        conn.commit()


def test_fresh_proposal_can_be_confirmed(db):
    prop = propose_gmail_cleanup_action(db, "news.example.com")
    assert prop["status"] == "pending" and "expires_at" in prop
    res = confirm_gmail_cleanup_action(db, prop["proposal_token"], confirmed=True)
    assert res["success"] is True and res["status"] == "confirmed"
    assert _status(db, prop["proposal_token"]) == "confirmed"


def test_proposal_just_inside_window_is_still_confirmable(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    _age(db, token, 14)
    assert confirm_gmail_cleanup_action(db, token, confirmed=True)["success"] is True


def test_proposal_older_than_15_minutes_is_rejected_and_marked_expired(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    _age(db, token, 16)
    res = confirm_gmail_cleanup_action(db, token, confirmed=True)
    assert res == {"success": False, "error": "PROPOSAL_EXPIRED", "status": "expired",
                   "message": "Proposal expired. Please request a new proposal."}
    assert _status(db, token) == "expired"


def test_expired_proposal_cannot_be_confirmed_later(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    _age(db, token, 30)
    confirm_gmail_cleanup_action(db, token, confirmed=True)
    again = confirm_gmail_cleanup_action(db, token, confirmed=True)
    assert again["success"] is False and again["error"] == "PROPOSAL_ALREADY_PROCESSED"
    assert _status(db, token) == "expired"


def test_expired_proposal_cannot_be_rejected_either(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    _age(db, token, 16)
    assert confirm_gmail_cleanup_action(db, token, confirmed=False)["error"] == "PROPOSAL_EXPIRED"


def test_already_confirmed_proposal_cannot_be_confirmed_again(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    assert confirm_gmail_cleanup_action(db, token, confirmed=True)["success"] is True
    second = confirm_gmail_cleanup_action(db, token, confirmed=True)
    assert second["success"] is False and second["error"] == "PROPOSAL_ALREADY_PROCESSED"


def test_cancelled_proposal_cannot_be_confirmed(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    rejected = confirm_gmail_cleanup_action(db, token, confirmed=False)
    assert rejected["success"] is True and rejected["status"] == "rejected"
    res = confirm_gmail_cleanup_action(db, token, confirmed=True)
    assert res["success"] is False and res["error"] == "PROPOSAL_ALREADY_PROCESSED"
    assert _status(db, token) == "rejected"


def test_unknown_token_is_rejected(db):
    assert confirm_gmail_cleanup_action(db, "no-such-token", confirmed=True)["error"] == "PROPOSAL_NOT_FOUND"


def test_concurrent_confirmations_have_exactly_one_winner(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    barrier = threading.Barrier(8)
    results = []

    def attempt():
        barrier.wait()
        results.append(confirm_gmail_cleanup_action(db, token, confirmed=True))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    winners = [r for r in results if r["success"]]
    losers = [r for r in results if not r["success"]]
    assert len(winners) == 1 and len(losers) == 7
    assert {r["error"] for r in losers} == {"PROPOSAL_ALREADY_PROCESSED"}
    assert _status(db, token) == "confirmed"


def test_claim_is_decided_by_guarded_update_not_prior_select(db, monkeypatch):
    """Deterministic race: a competing confirmation lands after the pre-check but before the claim."""
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    real_check = gi._cleanup_proposal_expired

    def competing_claim_then_check(created_at, now):
        with sqlite3.connect(db) as conn:  # the other request wins in between
            conn.execute("UPDATE gmail_cleanup_proposals SET status = 'confirmed' WHERE proposal_token = ?", (token,))
            conn.commit()
        return real_check(created_at, now)

    monkeypatch.setattr(gi, "_cleanup_proposal_expired", competing_claim_then_check)
    loser = confirm_gmail_cleanup_action(db, token, confirmed=True)
    assert loser["success"] is False
    assert loser["error"] == "PROPOSAL_ALREADY_PROCESSED"
    assert loser["message"] == "Proposal is already confirmed"


def test_endpoint_contract_and_no_external_action(db):
    token = propose_gmail_cleanup_action(db, "news.example.com")["proposal_token"]
    ok = client.post("/gmail/confirm_cleanup", json={"proposal_token": token, "confirmed": True})
    assert ok.status_code == 200 and ok.json()["success"] is True
    dup = client.post("/gmail/confirm_cleanup", json={"proposal_token": token, "confirmed": True})
    assert dup.status_code == 200 and dup.json()["error"] == "PROPOSAL_ALREADY_PROCESSED"

    stale = propose_gmail_cleanup_action(db, "old.example.com")["proposal_token"]
    _age(db, stale, 20)
    expired = client.post("/gmail/confirm_cleanup", json={"proposal_token": stale, "confirmed": True})
    assert expired.status_code == 200 and expired.json()["error"] == "PROPOSAL_EXPIRED"
