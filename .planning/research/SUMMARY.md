# Project Research Summary

**Project:** Evie
**Domain:** Single-User Personal Security & AI Assistant
**Researched:** 2026-09-19
**Confidence:** HIGH

## Executive Summary

Evie is designed as a unified single-user personal security monitoring system and AI assistant running locally with FastAPI, Python 3.11+, and SQLite. It combines automated digital risk detection (GitHub credential leakage, HIBP email breach alerts, Drive/Calendar exposure audits, 2FA status checks) with daily productivity tools (daily briefing, confirmation-gated calendar scheduling, long-term RAG journal recall, decision research support).

The primary architectural constraint is maintaining **one single system** — sharing a database, LLM token budget, and FastAPI backend server — rather than splitting security tools and assistant tools into disconnected projects. Crucially, secret values must never be stored or printed unredacted, and credentials must reside in OS keyring storage.

Key risks include accidental logging of unredacted credentials, Google OAuth 7-day token expiration due to "Testing" app status, and LLM calendar write hallucinations. All three are mitigated via strict design patterns (redaction helpers, Production publishing status, two-step `/calendar/propose` confirmation flow).

## Key Findings

### Recommended Stack

- **Core:** Python 3.11+, FastAPI, SQLModel/SQLite, `keyring` (OS vault)
- **Background Jobs:** APScheduler for periodic breach and exposure scans
- **Integrations:** PyGithub, Google API Python Client, HIBP HTTP API
- **AI & RAG:** ChromaDB / SQLite vector embeddings + sentence-transformers

### Expected Features

**Phase 1 (Core Deliverables):**
1. GitHub Secret Scanner (Pattern + Entropy detection, zero unredacted logs)
2. Breach Monitoring (HIBP API email checks)
3. Public Exposure Checker (Drive files & Calendar events)
4. 2FA Audit (Google & GitHub 2FA status)
5. Daily Briefing Engine (Webhook, wake-triggered, on-demand query)
6. Calendar Write (Confirmation flow gated)
7. Journal + RAG Recall (Vector search across arbitrary dates)

**Phase 2 (Extensions):**
8. Security Health Score
9. Remediation Guidance
10. Dependency Vulnerability Alerts
11. Phishing Pattern Detection
12. DRS Research Agent
13. Tone Configuration

### Architecture Approach

Layered single-codebase service model with isolated integration modules, centralized scheduler, unified briefing aggregator, and secure OS keyring persistence for credentials.

### Critical Pitfalls

1. **Secret Leakage:** Prevent by redacting all secret outputs (`redact_secret()`).
2. **Google OAuth Expiry:** Avoid 7-day expiry by ensuring app status is set to "Production".
3. **Unconfirmed Calendar Mutation:** Require `/calendar/propose` -> approval -> `/calendar/confirm`.
4. **Fabricating API Errors:** Surface raw HTTP failures rather than returning fake mock data.

## Implications for Roadmap

### Phase 1: Security Watchers & Keyring Foundations
**Delivers:** Core security modules (Secret Scanner, Breach Watcher, Exposure Checker, 2FA Audit) with keyring token handling and true positive/negative unit tests.

### Phase 2: Unified Briefing & Calendar Confirmation Flow
**Delivers:** Single briefing aggregator (`services/briefing.py`) supporting webhook, wake, and query triggers, plus confirmation-gated calendar write flow (`calendar_actions.py`).

### Phase 3: Journal & Long-Term RAG Recall
**Delivers:** Daily diary ingestion, local vector storage, and natural language recall queries over long timeframes.

### Phase 4: Security Score, Remediation & Dependency Alerts
**Delivers:** Security health score calculation, step-by-step remediation guides, Dependabot alerts, and Gmail phishing pattern scanner.

### Phase 5: DRS Research Agent & Tone Customization
**Delivers:** Multi-source web search & LLM synthesis research agent (evidence-based support) and tone configuration.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Python 3.11 + FastAPI + SQLite + keyring standard stack |
| Features | HIGH | Fully aligned with 01_PRD.md and 02_TRD.md specs |
| Architecture | HIGH | Single backend model defined in 04_Backend_Schema.md |
| Pitfalls | HIGH | Grounded in AGENTS.md hard rules |

**Overall confidence:** HIGH

---
*Research completed: 2026-09-19*
*Ready for roadmap: yes*
