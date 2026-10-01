# Requirements: Evie

**Defined:** 2026-09-19
**Core Value:** Provide a unified, single-user personal security and productivity assistant that runs cost-effectively and guarantees credentials are never logged or stored insecurely.

## v1 Requirements

### Security & Keyring Core

- [x] **SEC-01**: GitHub commit secret scanner detects leaked API keys and tokens using regex pattern and entropy analysis without ever logging or persisting raw secret values.
- [x] **SEC-02**: Email breach watcher checks user's registered emails against Have I Been Pwned (HIBP) API on a scheduled interval.
- [x] **SEC-03**: Public exposure auditor checks Google Drive files and Google Calendar events for unintended public or broad domain sharing.
- [x] **SEC-04**: Account 2FA auditor checks 2FA status on Google and GitHub accounts.
- [x] **SEC-05**: All credentials, OAuth tokens, and API keys are stored exclusively in OS Keyring (`keyring` library), never in SQLite or plaintext files.

### Briefing Engine

- [x] **BRIEF-01**: Single unified briefing aggregation function (`services/briefing.py`) generates concise summaries combining overnight GitHub events, calendar, open security findings, and RAG highlights.
- [x] **BRIEF-02**: Real-time webhook listener triggers instant notification upon GitHub events (new commit, PR raised).
- [x] **BRIEF-03**: Wake-triggered command delivers full briefing report upon user greeting.
- [x] **BRIEF-04**: On-demand query endpoint allows user to request fresh briefing reports at any time.

### Calendar Write & Control

- [x] **CAL-01**: User can propose calendar event additions using natural language via `/calendar/propose`.
- [x] **CAL-02**: Calendar mutation requires explicit user confirmation via `/calendar/confirm` before writing to Google Calendar API.

### Journal & RAG Recall

- [x] **MEM-01**: User can log daily journal entries and activity notes.
- [x] **MEM-02**: User can search long-term memory via natural language RAG recall queries across arbitrary time spans (e.g., "what was I doing 6 months ago?").

### Security Health & Remediation

- [x] **SCORE-01**: System calculates an aggregated Security Health Score (0-100) trending over time based on active findings.
- [x] **SCORE-02**: System generates step-by-step remediation guidance for each flagged security risk.
- [x] **SCORE-03**: System conversationally surfaces GitHub Dependabot vulnerability alerts.
- [x] **SCORE-04**: Read-only Gmail pattern scanner flags suspicious incoming email phishing patterns.

### Decision Research Support (DRS) & Tone

- [x] **DRS-01**: DRS research agent performs multi-source web search and LLM synthesis to return evidence-based, sourced research reports (never absolute/blind verdicts).
- [x] **TONE-01**: User can configure conversational tone, humor level, and formality settings.

## v2 Requirements

### Hardware & Voice ("Taz")

- **HW-01**: Physical Raspberry Pi device with microphone, speaker, and small reactive display.
- **HW-02**: Local wake-word detection, STT, and TTS pipeline with cloud LLM reasoning calls.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Multi-tenant cloud hosting | Evie is strictly single-user running on local machine |
| Plaintext secret storage | Violates hard security rules; secrets redacted everywhere |
| Direct unconfirmed calendar edits | Prevents LLM hallucination from modifying schedule unexpectedly |
| Paid APIs beyond HIBP | Cost constraint (< $20/month total) |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| SEC-01 | Phase 1 | Complete |
| SEC-02 | Phase 1 | Complete |
| SEC-03 | Phase 1 | Complete |
| SEC-04 | Phase 1 | Complete |
| SEC-05 | Phase 1 | Complete |
| BRIEF-01 | Phase 2 | Complete |
| BRIEF-02 | Phase 2 | Complete |
| BRIEF-03 | Phase 2 | Complete |
| BRIEF-04 | Phase 2 | Complete |
| CAL-01 | Phase 2 | Complete |
| CAL-02 | Phase 2 | Complete |
| MEM-01 | Phase 3 | Complete |
| MEM-02 | Phase 3 | Complete |
| SCORE-01 | Phase 4 | Complete |
| SCORE-02 | Phase 4 | Complete |
| SCORE-03 | Phase 4 | Complete |
| SCORE-04 | Phase 4 | Complete |
| DRS-01 | Phase 5 | Complete |
| TONE-01 | Phase 5 | Complete |

**Coverage:**
- v1 requirements: 19 total
- Mapped to phases: 19
- Unmapped: 0 ✓

---
*Requirements defined: 2026-09-19*
*Last updated: 2026-09-19 after initial definition*
