# AGENT_INSTRUCTIONS.md — Read This First
*Revision 2 — written against the real, already-substantial codebase (128 passing tests, live GitHub/Gmail integrations). This is not a greenfield build — read the current code before assuming a module doesn't exist.*

## What this project is
Evie: a personal, single-user, dashboard-first security and productivity assistant. One codebase. The GCSP submission is the security-facing subset (GitHub monitoring, Gmail monitoring, voice-controlled incident response, speaker verification, Taz hardware) — DRS and the personality/tone layer exist in code but are intentionally paused. Do not resume them without being explicitly told to. Full context: `01_PRD.md`, `02_TRD.md`, `03_UIUX_Design.md`, `04_Backend_Schema.md`.

## Current state summary (do not rebuild what already works)
Already implemented and tested: keyring credential storage, secret redaction, GitHub secret scanning (pattern + entropy), GitHub activity/PR tracking, GitHub webhook processing (HMAC-verified, idempotent), HIBP breach checks, 2FA audit, Google Drive exposure audit, calendar Propose→Confirm→Execute, SQLite+NumPy journal/semantic memory, security scoring (needs canonicalization, see below), Dependabot integration, live Gmail OAuth + classification, a first-generation conversational router, and a browser-based voice foundation (`voice.js`).

## Immediate priorities, in order
1. **Canonicalize the security score.** One function (`recompute_security_score()`) writes `security_score_current`; every consumer reads only that table. Fix the frontend `score || 100` bug — use explicit null checks.
2. **Fix Gmail phishing/spam conflation.** These must be two distinct classification calls. Integrate Laya's `noul` primitive as a free first-pass phishing filter, gated on its confidence score, escalating uncertain cases to the LLM classifier.
3. **Replace the intent-router with a tool-calling architecture.** Define a `tool_registry` (see Backend Schema Section 4). The LLM reads the full, free-form user message and selects from registered tools — it never executes anything directly, and it is never restricted in what it can *understand*, only in what it can *do*. This same router powers both chat and voice.
4. **Build the voice command pipeline**, framed primarily as security incident response ("revoke that key," "resolve this finding," "enable 2FA"), with a small allow-list of general commands as a secondary demo feature. See TRD Section 3 for the full pipeline.
5. **Build speaker verification** (Resemblyzer or SpeechBrain, fully local) as a gate before any `tool_registry` row marked `is_sensitive = 1` executes. Build the enrollment flow first.
6. **Continue GitHub sync optimization** toward true delta-based processing — the target metric is "how little work when 3 of 1,000 repos changed," not raw scan speed at 25 repos.
7. **Structured UI rendering** — replace raw text-block chat responses with cards/tables/badges as specified in the UI/UX doc.
8. Only after 1–7 are solid: begin Taz hardware integration, mirroring the same voice pipeline on the Pi Zero.

## Computer control (architecture frozen, not yet implemented)
The source of truth is `docs/07_Computer_Control.md`, and the evidence behind it is `.planning/research/computer-control/EVIDENCE.md`. In short:
- **One registered tool:** `computer_control`, which starts a session.
- **The loop lives in `ComputerControlSession`.** AppKit and Accessibility run only in a dedicated worker process.
- **Only validated mechanisms are enabled** (Phase CC-1a).
- **MEDIUM and HIGH actions are blocked.**
- **`open_app` is unchanged.**

Do not enable an operation because its schema exists. Do not treat the scratch validation (run under Antigravity IDE's Accessibility grant) as proof that Evie's own runtime is trusted.

## Hard rules — unchanged, still absolute
1. Never log, print, store, or return a full secret value. Always redact.
2. Never store tokens/passwords/API keys in SQLite. Keyring only. The speaker-verification reference embedding is a local file, never in SQLite, never transmitted.
3. Every sensitive action (calendar writes, Gmail cleanup, voice-triggered security actions) goes through Propose → Confirm → Execute. Voice-triggered sensitive actions additionally require passing speaker verification *before* the confirmation step is even shown.
4. Never fabricate API responses on failure — surface the error.
5. If/when DRS is resumed, its answers must remain evidence-based, never a definitive final verdict.
6. Keep LLM usage cost-aware: tool-calling uses a small/cheap model; never send the full database as context; retrieve only what a given request needs.
7. Do not implement voice-command execution as raw text-to-shell-command. It must go through the tool-calling router and the `tool_registry` allow-boundary, full stop — this is a hard security requirement, not a style preference, given the project's own subject matter.
8. Do not duplicate logic between the chat router and the voice pipeline — both call the same tool-calling core.
9. Google OAuth app stays in "Production" publishing status (not "Testing") for the single-user case, as already configured — do not revert this.

## Coding conventions (confirmed, keep as-is)
- Python, FastAPI backend, SQLite (no heavy ORM), NumPy for embeddings.
- One module per integration/domain, matching what's already built: GitHub, Gmail, Google (Calendar/Drive), security scoring, memory/journal, tool-calling router, voice pipeline (new).
- Split the test suite: fast unit/mocked tests run on every change; explicit live-API tests (GitHub/Gmail/OAuth) run separately so they don't slow normal iteration. Implemented:
  - Fast: `python -m pytest tests -q` (244 tests, ~6s; no real keyring, no outbound network — enforced by `tests/conftest.py`).
  - Live: `python -m pytest tests -m live -q` (3 tests, credential-aware skips).
  - New tests must pass inside the fast boundary. Anything that genuinely needs real credentials or network goes in a `@pytest.mark.live` test. The fast suite should not grow unchecked.

## When something is ambiguous
Flag it rather than silently deciding a significant architectural point (e.g., which model to use for tool-calling, how to structure the GitHub App migration) — propose the most reasonable option but don't build extensively on an unstated assumption.

## Claude Code — recommended use, if/when access is granted
Given the current state of the codebase, these specific tasks are the best fit for Claude Code specifically (large-context, multi-file reasoning, agentic terminal work), rather than smaller incremental prompts:
- **The intent-router → tool-calling migration** (priority 3 above): touches the router, chat endpoint, and voice endpoint simultaneously and needs consistent behavior across all three — a large, cross-cutting refactor.
- **Security score canonicalization** (priority 1): requires finding and correcting every place the score is currently computed or read (dashboard, chat, briefing) and consolidating them — exactly the kind of multi-file consistency sweep Claude Code is well-suited for.
- **The speaker verification module + its test suite**: security-sensitive, benefits from careful, deliberate code review rather than fast iteration.
- **GitHub sync delta-processing work**: requires reasoning about the existing `github_scanned_commits` logic across the sync pipeline without breaking already-passing tests.
- **Splitting the test suite into fast/live tiers**: a structural, repo-wide change touching test configuration and CI-style organization.
