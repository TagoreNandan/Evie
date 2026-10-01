# External Integrations

**Analysis Date:** 2026-09-19

## Overview
Evie integrates with external services for code security monitoring, account breach checking, cloud storage audit, calendar scheduling, memory retrieval, and research assistance.

---

## Service Integrations

### 1. GitHub API & Webhooks
- **Purpose**: Commit secret scanning, Dependabot alert monitoring, and real-time push/PR notification delivery.
- **Authentication**: Fine-grained Personal Access Token (PAT) with read-only `repo` and `security_events` scopes.
- **Rate Limits**: 5,000 requests/hour (ample for single-user scale).
- **Webhook Endpoint**: `POST /webhooks/github` (receives push, pull_request, and issue events).

### 2. Google OAuth 2.0 (Calendar & Drive)
- **Publishing Status**: Required to be **Production** (not Testing) to prevent 7-day refresh token expiration for the single user.
- **Google Calendar API**:
  - Scopes: `calendar.readonly` (exposure checks), `calendar.events` (scoped write).
  - Safety Contract: Write actions enforce two-step confirmation (`POST /calendar/propose` → user confirmation → `POST /calendar/confirm`). Direct unconfirmed mutation is prohibited.
- **Google Drive API**:
  - Scope: `drive.metadata.readonly` (sharing metadata audit for public exposure checks).
- **Gmail API (Phase 2)**:
  - Scope: `gmail.readonly` (phishing pattern detection).

### 3. Have I Been Pwned (HIBP) API
- **Purpose**: Email breach auditing and credential exposure checks.
- **Endpoints & Auth**: Breach API requires a paid API key (~$3.50/mo); Pwned Passwords endpoint uses k-Anonymity model without API keys.
- **Storage**: Findings recorded in `breach_checks` SQLite table; deduplicated across checks.

### 4. LLM API Providers
- **Usage Strategy**: Cost-aware dual-tier architecture.
  - **Routine Tier**: Low-cost model for daily briefings, summarization, and routine responses.
  - **Research Tier**: High-reasoning model for Decision/Research Support System (DRS) queries.
- **Auth**: API key supplied via `LLM_API_KEY` environment variable.

---

## Data Flow & Authentication Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Evie Service Layer                       │
└──────┬──────────────┬──────────────┬──────────────┬─────────┘
       │              │              │              │
┌──────▼──────┐┌──────▼──────┐┌──────▼──────┐┌──────▼──────┐
│ GitHub PAT  ││ Google OAuth││  HIBP Key   ││ LLM Key     │
│ (OS Keyring)││ (OS Keyring)││ (OS Keyring)││ (Env Var)   │
└─────────────┘└─────────────┘└─────────────┘└─────────────┘
```

---

*Integrations analysis: 2026-09-19*
<!-- refreshed: 2026-09-19 -->
