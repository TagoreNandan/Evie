"""
Unit and Integration Tests for Assistant Personality & Tone Manager (`tone.py`).
"""

import os
import pytest
from fastapi.testclient import TestClient

import tone
from main import app

TEST_DB = "test_tone_evie.db"


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    tone.init_user_config_table(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_default_tone_preference_is_neutral():
    pref = tone.get_tone_preference(TEST_DB)
    assert pref == "neutral"


def test_set_and_get_tone_preference():
    updated = tone.set_tone_preference("casual", TEST_DB)
    assert updated == "casual"
    assert tone.get_tone_preference(TEST_DB) == "casual"

    tone.set_tone_preference("humorous", TEST_DB)
    assert tone.get_tone_preference(TEST_DB) == "humorous"


def test_set_invalid_tone_raises():
    with pytest.raises(ValueError, match="Invalid tone preference"):
        tone.set_tone_preference("sarcastic", TEST_DB)


def test_apply_tone_variations():
    base_text = "Your security score is 95."

    assert tone.apply_tone(base_text, tone="neutral", db_path=TEST_DB) == "Your security score is 95."
    assert "Hey there!" in tone.apply_tone(base_text, tone="casual", db_path=TEST_DB)
    assert "Please be advised:" in tone.apply_tone(base_text, tone="formal", db_path=TEST_DB)
    assert "Pro tip:" in tone.apply_tone(base_text, tone="humorous", db_path=TEST_DB)


def test_tone_config_api_endpoints(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    client = TestClient(app)

    # 1. GET initial tone
    res_get = client.get("/config/tone")
    assert res_get.status_code == 200
    assert res_get.json()["tone_preference"] == "neutral"

    # 2. POST update tone to formal
    res_post = client.post("/config/tone", json={"tone_preference": "formal"})
    assert res_post.status_code == 200
    assert res_post.json()["tone_preference"] == "formal"

    # 3. GET verify update
    res_get2 = client.get("/config/tone")
    assert res_get2.status_code == 200
    assert res_get2.json()["tone_preference"] == "formal"

    # 4. POST invalid tone -> 400 Bad Request
    res_bad = client.post("/config/tone", json={"tone_preference": "robotic"})
    assert res_bad.status_code == 400
