# Technology Stack

**Analysis Date:** 2026-09-19

## Overview
Evie is a personal, single-user AI assistant designed to run locally as a unified Python 3.11+ system. The application relies on a lightweight FastAPI backend, SQLite for structured storage, Chroma for vector embeddings, and direct integrations with key third-party APIs (GitHub, Google Workspace, HIBP, LLM providers).

---

## Core Technologies

### Languages & Runtimes
- **Python 3.11+**: Primary runtime for the Evie backend application, services, background workers, and CLI/API integration.
- **Node.js 24.21.0**: Node runtime used for repository workflow tools, GSD core agent execution, and web tool shims (`.nvmrc`).
- **HTML/JS (Vanilla)**: Lightweight single-page frontend interface running locally in the browser (Phase 1–2).

### Frameworks & Libraries
- **FastAPI**: Asynchronous web framework providing internal REST API endpoints and GitHub webhook handlers (`/webhooks/github`).
- **SQLite (`sqlite3` / `sqlmodel`)**: Lightweight, zero-config relational database for application state, findings, and logs.
- **Chroma (`chromadb`)**: In-process vector database managing `journal_embeddings` collection for RAG memory search.
- **APScheduler**: Task scheduler running background jobs (nightly journal aggregation, HIBP breach checks, exposure audits).
- **OS Keyring (`keyring`) / `cryptography` (Fernet)**: Secure storage mechanism for OAuth tokens, API keys, and credentials.

---

## Configuration & Environment Management

- **Config Architecture**: Single-file configuration pattern (`config.py` or `config.yaml`) for non-secret operational settings (polling intervals, threshold scores, tone defaults).
- **Secret Isolation**: Zero secrets in SQLite or source code. Secrets sourced strictly from OS keyring or runtime environment variables (`LLM_API_KEY`, `HIBP_API_KEY`).
- **Development Tooling**: GSD Core v1.14.0 workflows located in `.agents/` for agentic planning, codebase mapping, and execution.

---

*Stack analysis: 2026-09-19*
<!-- refreshed: 2026-09-19 -->
