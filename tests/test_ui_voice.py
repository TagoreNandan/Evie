"""
Unit and Integration Tests for Evie Web UI & Voice Interface Adapter (`ui/` & `main.py`).
"""

import os
import pytest
from fastapi.testclient import TestClient

from main import app, DB_PATH, init_db

TEST_DB = "test_ui_voice_evie.db"


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    init_db(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_root_endpoint_serves_ui_html():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Evie Assistant" in response.text
    assert "app-container" in response.text


def test_static_assets_serving():
    client = TestClient(app)
    res_css = client.get("/static/styles.css")
    assert res_css.status_code == 200
    assert "glassmorphism" in res_css.text.lower() or "panel-bg" in res_css.text.lower()

    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    assert "fetchSecurityScore" in res_js.text

    res_voice = client.get("/static/voice.js")
    assert res_voice.status_code == 200
    assert "VoiceAdapter" in res_voice.text


def test_voice_adapter_file_integrity():
    voice_js_path = os.path.join("ui", "voice.js")
    assert os.path.exists(voice_js_path)
    with open(voice_js_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "class VoiceAdapter" in content
    assert "startListening" in content
    assert "speak" in content


def test_fastapi_endpoints_accessible_for_ui(monkeypatch):
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    client = TestClient(app)

    # 1. Score
    res_score = client.get("/score")
    assert res_score.status_code == 200

    # 2. Findings
    res_findings = client.get("/findings")
    assert res_findings.status_code == 200

    # 3. Tone Config
    res_tone = client.get("/config/tone")
    assert res_tone.status_code == 200
    assert "tone_preference" in res_tone.json()
