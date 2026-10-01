"""
Unit and Integration Tests for Phase D: Dashboard-First Web UI & Structured Chat Rendering.
"""

import os
import pytest
from fastapi.testclient import TestClient
from main import app, DB_PATH, init_db

TEST_DB = "test_ui_phase_d_evie.db"
client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    monkeypatch.setattr("main.DB_PATH", TEST_DB)
    init_db(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def test_index_html_dashboard_first_structure():
    """
    Verify index.html includes dashboard-first sections, status banner, voice button, and secondary chat.
    """
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    assert "app-container" in html
    assert "overallStatusBanner" in html
    assert "overallStatusBadge" in html
    assert "micBtn" in html
    assert "Security Voice Control" in html
    assert "githubRepoCount" in html
    assert "gmailTotalScanned" in html
    assert "activityTimeline" in html
    assert "chatMessages" in html


def test_app_js_dashboard_and_score_zero_handling():
    """
    Verify app.js loads /dashboard/state and handles score 0 explicitly without converting to 100.
    """
    res = client.get("/static/app.js")
    assert res.status_code == 200
    js = res.text

    assert "fetchDashboardState" in js
    assert "fetchSecurityScore" in js
    assert "renderDashboardUI" in js
    assert "scoreVal" in js
    assert "renderStructuredChatMessage" in js
    # Confirm zero-fallback pattern (score || 100) is absent
    assert "score || 100" not in js


def test_static_css_glassmorphism_and_dashboard_styles():
    """
    Verify styles.css includes glassmorphism, dashboard grid, status badges, and timeline list.
    """
    res = client.get("/static/styles.css")
    assert res.status_code == 200
    css = res.text

    assert "glassmorphism" in css.lower() or "panel-bg" in css.lower()
    assert "dashboard-grid" in css
    assert "badge-good" in css
    assert "badge-attention" in css
    assert "badge-critical" in css
    assert "timeline-list" in css


def test_phishing_language_in_dashboard_ui():
    """
    Verify dashboard state output includes heuristic disclaimer wording and separates promotional emails.
    """
    res = client.get("/dashboard/state")
    assert res.status_code == 200
    data = res.json()

    assert "phishing_disclaimer" in data
    assert "heuristic" in data["phishing_disclaimer"].lower()
    assert "not" in data["phishing_disclaimer"].lower() and "proof" in data["phishing_disclaimer"].lower()
    assert "promotional_summary" in data
    assert "github_summary" in data
    assert "gmail_summary" in data
