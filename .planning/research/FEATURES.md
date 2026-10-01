# Feature Research

**Domain:** Single-User Personal Security & AI Assistant
**Researched:** 2026-09-19
**Confidence:** HIGH

## Feature Landscape

### Table Stakes (Users Expect These)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| GitHub Secret Scanner | Automatic detection of API keys and credentials in code commits | MEDIUM | Redact all secrets before logging/storing |
| Breach Monitoring | Alerts when email appears in Have I Been Pwned breaches | LOW | Scheduled polling via HIBP API |
| Public Exposure Checker | Audits Drive files and Calendar events shared publicly or externally | MEDIUM | Uses Google APIs with proper scope checks |
| 2FA Audit | Checks status of 2FA on primary personal accounts (Google, GitHub) | LOW | API-based security status audit |
| Daily Briefing Generator | Aggregates overnight events, schedule, and security alerts | MEDIUM | Unified logic across webhook, wake, and query |
| Calendar Write | Proposes and books events into calendar | MEDIUM | **Requires explicit confirmation flow** |

### Differentiators (Competitive Advantage)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Journal & RAG Recall | Searchable long-term memory across arbitrary timeframes ("what did I do 6 months ago?") | HIGH | Vector embeddings + SQLite keyword hybrid search |
| DRS (Decision Support) | Sourced, evidence-based research reports (never blind verdicts) | HIGH | Multi-source search + LLM synthesis |
| Unified Single-System Architecture | One backend, database, and LLM budget for both security and productivity | MEDIUM | Prevents double billing and fragmented data |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Automatic Unconfirmed Calendar Edits | Convenience | LLM hallucination risks creating/modifying wrong events | `/calendar/propose` -> User Confirmation -> `/calendar/confirm` |
| Plaintext Secret Persistence | Detailed security audit logs | Massively violates security principles if DB is breached | First/last 2 chars redaction, no secret storage in SQLite |
| Cloud Multi-Tenant Service | Sharing with friends/team | Huge auth complexity, secret storage risk, high cloud hosting costs | Single-user local runtime with OS keyring auth |

## Feature Dependencies

```
[Database & Keyring Storage]
    └──requires──> [Configuration & Credentials]

[Secret Scanner] ───────┐
[Breach Monitoring] ────┼──feed into──> [Daily Briefing Engine] & [Security Health Score]
[Public Exposure] ──────┤
[2FA Audit] ────────────┘

[Calendar Write] ──requires──> [User Confirmation Flow]
[DRS Research Agent] ──requires──> [Web Search & LLM Synthesis]
[RAG Recall] ──requires──> [Vector Storage & Journal Ingestion]
```

## MVP Definition

### Launch With (Phase 1)

- [ ] **GitHub Secret Scanner** — Pattern + entropy detection on commits
- [ ] **Breach Monitoring** — HIBP integration for email breach checks
- [ ] **Public Exposure Checker** — Drive/Calendar sharing auditor
- [ ] **2FA Audit** — Account 2FA status check
- [ ] **Daily Briefing Generator** — Webhook, wake-triggered, on-demand modes
- [ ] **Calendar Write** — Confirmation-gated calendar event scheduling
- [ ] **Journal + RAG Recall** — Daily diary & natural-language recall

### Add After Validation (Phase 2)

- [ ] **Security Health Score** — Aggregated score trending over time
- [ ] **Remediation Guidance** — Step-by-step fix guides
- [ ] **Dependency Vulnerability Alerts** — GitHub Dependabot integration
- [ ] **Phishing Pattern Detection** — Read-only Gmail scanner
- [ ] **DRS Research Agent** — Sourced web research assistant
- [ ] **Tone Configuration** — Conversational personality adjustment

### Future Consideration (Phase 3)

- [ ] **Hardware Raspberry Pi Voice Assistant ("Taz")** — Physical wake-word & display device

---
*Feature research for: Evie Personal Security & AI Assistant*
*Researched: 2026-09-19*
