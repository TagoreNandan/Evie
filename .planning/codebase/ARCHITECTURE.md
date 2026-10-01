# System Architecture

**Analysis Date:** 2026-09-19

## System Pattern Overview

Evie is designed as a single-process, local-first Python application combining a FastAPI HTTP backend, a background job scheduler (`scheduler.py`), a local SQLite database, a Chroma vector engine for RAG memory, and a lightweight web chat interface.

```
┌─────────────────────────────────────────────────────────────┐
│                        Evie Backend                          │
│                    (Python 3.11+ FastAPI)                   │
│                                                                │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ github_watch │  │ breach_watch │  │  memory.py          │  │
│  │ exposure_    │  │ twofa_audit  │  │  (SQLite + Chroma)  │  │
│  │ watch        │  │ calendar_    │  │                    │  │
│  └──────┬───────┘  └──────┬───────┘  └─────────┬──────────┘  │
│         │                  │                     │             │
│         └──────────┬───────┴──────────┬──────────┘             │
│                     │                  │                        │
│              ┌──────▼──────────────────▼──────┐                │
│              │          Orchestrator          │                │
│              │  (Aggregator, LLM dispatch,    │                │
│              │   DRS, Conversation Engine)    │                │
│              └──────┬──────────────────┬──────┘                │
│                     │                  │                         │
│           ┌─────────▼──────┐   ┌───────▼─────────┐               │
│           │ SQLite DB      │   │  LLM Client     │               │
│           │ (findings, logs│   │  (Cost-aware    │               │
│           │  scores, state)│   │   dual-tier)    │               │
│           └────────────────┘   └─────────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

---

## Core Architectural Modules

### 1. Watch Modules (Domain Integrations)
- `github_watch.py`: Performs pattern and Shannon entropy analysis on GitHub commits. Redacts detected secret values prior to persistence.
- `breach_watch.py`: Polls Have I Been Pwned (HIBP) API for email breach records; deduplicates checks against past findings.
- `exposure_watch.py`: Audits Google Drive metadata and Google Calendar public visibility settings.
- `twofa_audit.py`: Checks 2FA enforcement state for Google and GitHub accounts.
- `calendar_actions.py`: Implements two-phase calendar mutation (`/calendar/propose` → `/calendar/confirm`).

### 2. Memory & Recall Service (`memory.py`)
- Acts as a unified data layer consumed by briefings, DRS, and conversation engines.
- Manages dual-persistence: raw entries in SQLite `journal_entries` table and semantic vectors in Chroma collection `journal_embeddings`.

### 3. Orchestrator & Aggregator
- Provides unified reporting logic for three trigger paths:
  1. **Real-time**: FastAPI webhook endpoint (`/webhooks/github`) pushes notifications instantly.
  2. **Wake-triggered**: User greeting ("good morning") invokes shared `get_full_report()` aggregation function.
  3. **On-demand**: Direct query ("what's the report") queries current SQLite state directly.

### 4. Scheduler & Storage
- `scheduler.py`: Single centralized background scheduler handling periodic watch executions.
- `keyring_store.py`: Keyring abstraction storing PATs and OAuth refresh tokens in OS keyring or Fernet-encrypted local storage.

---

*Architecture analysis: 2026-09-19*
<!-- refreshed: 2026-09-19 -->
