# Roadmap: Evie

## Overview

Evie is built as a single, unified personal security and productivity assistant. The roadmap begins by establishing the secure credential infrastructure (OS Keyring) and core security detection modules (Phase 1), followed by the central briefing generator and confirmation-gated calendar actions (Phase 2). Next, long-term memory and RAG recall are added (Phase 3), followed by security scoring, remediation guidance, and phishing detection (Phase 4), and evidence-based DRS research engine and tone customization (Phase 5). Phase 6 connects real external integrations for GCSP validation, Phase 7 builds the local web application and browser voice interface, Phase 8 expands DRS into a general-purpose research engine, culminating in Phase 9 hardware display and voice integration ("Taz").

## Phases

- [x] **Phase 1: Security Watchers & Keyring Foundations** - Build keyring credential manager and core security detection engines (Secret Scanner, Breach Watcher, Exposure Auditor, 2FA Audit).
- [x] **Phase 2: Briefing Engine & Calendar Confirmation Flow** - Implement unified briefing aggregator (webhook, wake, query) and two-step confirmation calendar writes.
- [x] **Phase 3: Journal & Long-Term RAG Recall System** - Implement daily diary logging, vector indexing, and natural-language memory recall over arbitrary dates.
- [x] **Phase 4: Security Score, Remediation Guidance & Dependabot Alerts** - Aggregate security metrics into a trending health score, provide remediation guides, Dependabot alerts, and Gmail phishing scan.
- [x] **Phase 5: DRS Research Agent & Personality Configuration** - Build evidence-based web research agent with source attribution and configurable conversational tone.
- [x] **Phase 6: GCSP Real Integration & End-to-End Validation** - Connect Evie's completed security capabilities to their real external integrations and validate the complete security/briefing flow end-to-end.
- [x] **Phase 7: Evie Web UI & Voice Interface** - Build local web application interface (glassmorphic dark design system, chat, briefing triggers, score gauge, findings drawer, calendar confirmation modal) and pluggable browser voice interface layer.
- [x] **Phase 8: GitHub + Gmail Personal & Work Intelligence** - Build complete repository discovery, commit time-series analytics, unreviewed PR tracking, read-only Gmail metadata ingestion, categorization, sender frequency analytics, and proposal-confirmation gated cleanup.
- [ ] **Phase 9: General-Purpose DRS Research Engine** - Evolve Evie's DRS foundation into a general-purpose research system capable of conversational web research and longer-running deep research with sourced reports.
- [ ] **Phase 10: Hardware & Voice Integration ("Taz")** - Implement local wake-word detection, audio STT/TTS processing pipeline, and Raspberry Pi hardware display integration.

## Phase Details

### Phase 1: Security Watchers & Keyring Foundations
**Goal**: Establish zero-secret-leakage credential storage via OS keyring and implement primary security scanners with synthetic test fixtures.
**Depends on**: Nothing
**Requirements**: SEC-01, SEC-02, SEC-03, SEC-04, SEC-05
**Success Criteria** (what must be TRUE):
  1. Secret scanner accurately detects synthetic true positives and rejects true negatives without ever writing raw secret values to logs or SQLite database.
  2. Scheduled HIBP breach monitoring, Drive/Calendar public exposure auditor, and account 2FA status inspector return structured findings.
  3. All API keys and OAuth credentials are saved and loaded strictly from OS Keyring.
**Plans**: 4 plans

Plans:
- [x] 01-01: Keyring vault integration & secret redaction module
- [x] 01-02: GitHub commit secret scanner with pattern & entropy detection
- [x] 01-03: HIBP breach watch & account 2FA audit integration
- [x] 01-04: Drive & Calendar public exposure auditor

### Phase 2: Briefing Engine & Calendar Confirmation Flow
**Goal**: Build single unified briefing aggregation module supporting webhook, wake-triggered, and query modes, alongside confirmation-gated calendar scheduler.
**Depends on**: Phase 1
**Requirements**: BRIEF-01, BRIEF-02, BRIEF-03, BRIEF-04, CAL-01, CAL-02
**Success Criteria** (what must be TRUE):
  1. All briefing triggers (webhook event, wake greeting, on-demand query) invoke `services/briefing.py` for consistent report generation.
  2. Calendar scheduling proposals (`/calendar/propose`) require explicit user confirmation (`/calendar/confirm`) before calling Google Calendar API.
**Plans**: 3 plans

Plans:
- [x] 02-01: Shared Briefing Aggregator Engine (`services/briefing.py`)
- [x] 02-02: Briefing Triggers & FastAPI Endpoints (`main.py`)
- [x] 02-03: Confirmation-Gated Calendar Actions (`integrations/calendar_actions.py`)

### Phase 3: Journal & Long-Term RAG Recall System
**Goal**: Enable daily diary entry logging, local vector indexing, and natural-language recall across long historical timeframes.
**Depends on**: Phase 2
**Requirements**: MEM-01, MEM-02
**Success Criteria** (what must be TRUE):
  1. User can store daily journal entries and activity logs.
  2. RAG recall queries return relevant historical entries with exact source dates.
**Plans**: 2 plans

Plans:
- [x] 03-01: Daily Journal Ingestion & Shared Memory Engine (`memory.py`)
- [x] 03-02: Vector Embedding Index & Hybrid RAG Recall Engine (`memory.py`)

### Phase 4: Security Score, Remediation Guidance & Dependabot Alerts
**Goal**: Aggregate security metrics into a trending health score, generate step-by-step fix guides, surface Dependabot alerts, and scan Gmail phishing patterns.
**Depends on**: Phase 1, Phase 2
**Requirements**: SCORE-01, SCORE-02, SCORE-03, SCORE-04
**Success Criteria** (what must be TRUE):
  1. Dynamic 0-100 Security Health Score accurately reflects active security findings over time.
  2. System provides clear, actionable remediation guides for flagged issues.
  3. Dependabot alerts and incoming email phishing patterns are surfaced conversationally.
**Plans**: 3 plans

Plans:
- [x] 04-01: Security Health Score Aggregator (`security_score.py`)
- [x] 04-02: Remediation Guidance & Dependabot Watcher (`remediation.py` & `integrations/dependency_watch.py`)
- [x] 04-03: Read-Only Gmail Phishing Pattern Analyzer (`integrations/phishing_scan.py`)

### Phase 5: DRS Research Agent & Personality Configuration
**Goal**: Build evidence-based Decision Research Support engine with web search synthesis and source attribution, plus tone customization.
**Depends on**: Phase 3
**Requirements**: DRS-01, TONE-01
**Success Criteria** (what must be TRUE):
  1. DRS research queries produce sourced, non-definitive research support summaries with verified web links.
  2. Conversational tone, humor, and formality settings dynamically update assistant responses.
**Plans**: 2 plans

Plans:
- [x] 05-01: DRS Research Agent (`drs.py`)
- [x] 05-02: Assistant Personality & Tone Configuration (`tone.py`)

### Phase 6: GCSP Real Integration & End-to-End Validation

**Goal**: Connect Evie's completed security capabilities to their real external integrations and validate the complete security/briefing flow end-to-end.
**Depends on**: Phase 1, Phase 2, Phase 4
**Requirements**: SEC-06, BRIEF-05, SCORE-05
**Success Criteria** (what must be TRUE):

1. Real GitHub, HIBP, Google Drive, Google Calendar, and Gmail integrations operate with the intended permissions and credential handling.
2. Unified security briefing and Security Health Score produce correct results from real integration data.
3. Webhook delivery, OAuth/token refresh, rate limits, errors, and realistic external-service edge cases are handled safely.
4. No raw credentials, secrets, tokens, or sensitive external data are leaked through logs, findings, errors, or API responses.

**Plans**: 2 plans

Plans:

- [x] 06-01: Real Integration Adapters & Google OAuth Refresh (`services/real_integrations.py`)
- [x] 06-02: End-to-End Security Briefing & Health Score Aggregation Validation (`tests/test_real_integrations.py`)

### Phase 7: Evie Web UI & Voice Interface

**Goal**: Build local web application interface (glassmorphic dark design system, chat, briefing triggers, score gauge, findings drawer, calendar confirmation modal) and pluggable browser voice interface layer.
**Depends on**: Phase 6
**Requirements**: UI-01, UI-02
**Success Criteria** (what must be TRUE):

1. User can interact with Evie conversationally in a local web application UI that displays real-time security score, briefings, findings, remediation guides, and calendar proposal confirmations.
2. Pluggable browser voice interface captures microphone audio STT and speaks assistant text TTS using a clean adapter abstraction.
3. All calendar mutations strictly require explicit user confirmation card clicks (`/calendar/confirm`).

**Plans**: 2 plans

Plans:

- [x] 07-01: Evie Web Application Frontend & Design System (`ui/` & `main.py`)
- [x] 07-02: Browser Voice Interface Adapter Layer (`ui/voice.js` & `tests/test_ui_voice.py`)

### Phase 8: GitHub + Gmail Personal & Work Intelligence

**Goal**: Build complete repository discovery, commit time-series analytics, unreviewed PR tracking, read-only Gmail metadata ingestion, categorization, sender frequency analytics, and proposal-confirmation gated cleanup.
**Depends on**: Phase 6, Phase 7
**Requirements**: SEC-07, MAIL-01
**Success Criteria** (what must be TRUE):

1. Auto-discover personal, private, and accessible org repositories and track commit time-series activity and unreviewed PRs.
2. Ingest read-only Gmail metadata headers, categorize emails, analyze sender frequency, and generate proposal-confirmation gated cleanup recommendations.
3. Integrate intelligence analytics into daily briefing generator and resolve exact factual conversational queries in `/chat`.

**Plans**: 3 plans

Plans:

- [x] 08-01: GitHub Discovery, Activity Tracking & Analytics Engine (`integrations/github_watch.py`, `services/github_intelligence.py`)
- [x] 08-02: Read-Only Gmail Intelligence, Categorization & Recommendation Engine (`integrations/phishing_scan.py`, `services/gmail_intelligence.py`)
- [x] 08-03: Briefing & Conversational Intelligence Router Integration (`services/briefing.py`, `main.py`, `tests/test_github_gmail_intelligence.py`)

### Phase 9: General-Purpose DRS Research Engine

**Goal**: Evolve Evie's DRS foundation into a general-purpose research system capable of conversational web research and longer-running deep research with sourced reports.
**Depends on**: Phase 5
**Requirements**: DRS-02, DRS-03
**Success Criteria** (what must be TRUE):

1. Evie can answer arbitrary research questions using multiple web/source adapters with attributed evidence.
2. Quick conversational research and longer-running deep research use a shared retrieval, extraction, deduplication, and evidence-synthesis foundation.
3. Deep research produces a structured report with source attribution and clear evidence limitations.
4. Source adapters can be added without redesigning the core research engine.

**Plans**: 2 plans

Plans:

- [ ] 09-01: Pluggable Source Adapter Infrastructure & Retrieval Engine (`drs.py`)
- [ ] 09-02: Deep Research Engine & Structured Multi-Section Report Generator (`drs.py`, `main.py`, `tests/test_drs_deep_research.py`)

### Phase 10: Hardware & Voice Integration ("Taz")

**Goal**: Implement local wake-word detection, audio STT/TTS processing pipeline, and Raspberry Pi hardware display integration.
**Depends on**: Phase 2, Phase 5, Phase 7
**Requirements**: HW-01, HW-02
**Success Criteria** (what must be TRUE):

1. Local wake-word audio detector triggers voice capture pipeline cleanly.
2. STT/TTS pipeline enables voice interaction with Evie briefing, DRS, and calendar endpoints.
3. Hardware display controller updates status visuals reactively.

**Plans**: 2 plans

Plans:

- [ ] 10-01: Voice Pipeline & Audio Processing Engine (`voice_pipeline.py`)
- [ ] 10-02: Hardware Display Controller & Audio Route Integration (`hardware_display.py`)

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Security Watchers & Keyring Foundations | 4/4 | Complete | 2026-09-19 |
| 2. Briefing Engine & Calendar Confirmation Flow | 3/3 | Complete | 2026-09-19 |
| 3. Journal & Long-Term RAG Recall System | 2/2 | Complete | 2026-09-19 |
| 4. Security Score, Remediation Guidance & Dependabot Alerts | 3/3 | Complete | 2026-09-19 |
| 5. DRS Research Agent & Personality Configuration | 2/2 | Complete | 2026-09-19 |
| 6. GCSP Real Integration & End-to-End Validation | 2/2 | Complete | 2026-09-19 |
| 7. Evie Web UI & Voice Interface | 2/2 | Complete | 2026-09-21 |
| 8. GitHub + Gmail Personal & Work Intelligence | 3/3 | Complete | 2026-09-21 |
| 9. General-Purpose DRS Research Engine | 0/2 | Planned | - |
| 10. Hardware & Voice Integration ("Taz") | 0/2 | Future | - |
---
*Roadmap created: 2026-09-19*
*Last updated: 2026-09-21 after Phase 8 completion*

