# Evie — Technical Requirements Document (TRD)
*Revision 2 — reconciled with actual implementation*

## 1. Actual Current Architecture (as built)

```
                         ┌──────────────────────┐
                         │       EVIE WEB       │
                         │  Dashboard / Chat /   │
                         │  Alerts / Timeline    │
                         └──────────┬───────────┘
                                    │
                         ┌──────────────────────┐
                         │   Evie Application    │
                         │  Intelligence Layer   │
                         │  Security Layer       │
                         │  Memory Layer         │
                         │  Action Layer         │
                         └──────────┬───────────┘
                 ┌──────────────────┼──────────────────┐
                 ↓                  ↓                  ↓
             GitHub              Gmail             Google
          (Activity, Repos,   (Messages,        (Calendar, Drive,
           PRs, Secrets)       Classification)    OAuth)
                 │                  │
                 ↓                  ↓
          Event/Incremental    Sync Checkpoint
             Processing          (partial)
```

Deviations from the original plan, kept because they're working well:
- **Semantic memory:** SQLite + NumPy + dense feature hashing + cosine similarity, **not Chroma**. Lighter, fully local, no extra dependency. Keep this.
- **Dashboard is the primary surface**, not chat. Chat/voice are secondary, exploratory interfaces.
- **GitHub PAT** is confirmed dev/personal-use only; GitHub App is the documented future direction for any real onboarding (Section 4).

## 2. Target Architecture — Tool-Calling Router (replaces the intent classifier)

The current conversational router (categorized: greetings, security score, GitHub repos/commits/PRs/secrets, Gmail summaries/promotional/phishing, calendar, DRS) is a **finite intent classifier** — functionally cleaner than scattered `if/elif`, but still fundamentally "which predefined bucket does this message belong to." This must evolve to:

```
User message (free-form, unconstrained)
      ↓
Small/cheap LLM with a defined tool schema
      ↓
Model decides which tool(s) to call, with what parameters
      ↓
Tool executes (read-only tools return data; action tools require
  Propose→Confirm, or for voice, the speaker-verification gate)
      ↓
Retrieved/result data passed back to the model
      ↓
Natural-language response + structured UI payload
```

**Critical distinction — this is the resolution to the "understand everything vs. safety" tension:**
- **Understanding is never restricted.** The model reads the full, free-form user message exactly as written — no fixed phrase list, no keyword matching.
- **Execution is restricted**, not understanding. The model can only *call* predefined tool functions (`get_github_activity()`, `revoke_secret_finding(id)`, `enable_2fa(service)`, `resolve_finding(id)`, `open_app(name)`, etc.). Each tool independently validates its own inputs. This is standard LLM function/tool-calling, supported natively by all major providers, at negligible extra cost over a plain text completion.

This same router should power **both** the chat interface and the voice-command pipeline — one mechanism, two front doors — resolving the current duplication risk between chat and voice logic.

**Execution boundary (as built):**
- **LLM-selected actions** must pass through the `tool_registry` allow-check, Pydantic argument validation, and the sensitivity/confirmation gates. The LLM is a **planner only**: it runs once per user message and returns a tool name plus arguments as data. It never calls a function directly and never writes the final reply. Tool results are not fed back to it, and responses come from the existing handler templates.
- **Voice-selected actions** must additionally pass the voice security gate (Section 3).
- **Explicit, trusted UI interactions** (a user clicking a button in the dashboard, such as the finding-card Resolve / False positive buttons or the calendar and Gmail confirm dialogs) may keep using the existing direct REST endpoints where they are already implemented. These are not LLM decisions, so they do not go through the planner.

## 3. Voice Control Pipeline (new — the 3rd GCSP feature)

| Step | Mechanism | Cost |
|---|---|---|
| Wake/activate | Push-to-talk button on dashboard (not always-listening) | $0 |
| Speech-to-text | Browser Web Speech API (already implemented in `voice.js`) | $0 |
| Understanding + tool selection | Tool-calling LLM call, sees the full unconstrained message | Fractions of a cent/command |
| Sensitive-action gate | Speaker verification (Resemblyzer or SpeechBrain, local) runs before executing any tool flagged `sensitive: true` (revoke key, change 2FA, delete/modify data) | $0, local |
| Execution | Tool runs; OS-level actions (e.g. `open_app`) go through a per-OS adapter (`osascript` on Mac first; Windows/Linux adapters later) | $0 |
| Spoken response | Web Speech Synthesis API or OS `say` | $0 |

Recommended voice-first use case framing: **security incident response** — "revoke that leaked key," "resolve this finding," "enable 2FA on my GitHub" — with a small allow-list of general commands (open specific apps) as a secondary "wow" demo moment. This ties the voice feature back to the Secure Cyberspace challenge rather than it being a generic Mac-control gimmick.

**General OS command (as built):** `open_app` is a registered, non-sensitive tool. It needs no speaker verification and no confirmation, and it goes through the same router as chat.
- **Allow-list** (`services/os_adapter.py`): Safari, Google Chrome, Firefox, Finder, Terminal, Visual Studio Code. Only case and whitespace differences are forgiven; unknown names are refused.
- **Parameters:** `app_name`, matching `^[A-Za-z0-9 ]+$`, with no extra fields allowed.
- **macOS adapter:** a fixed `osascript` template (`tell application id (item 1 of argv) to activate`). The app's bundle id is passed as argv data and subprocess runs without a shell. Installation is checked only at code-defined bundle paths, so macOS never shows a "Where is…?" prompt.
- **Forbidden by design:** arbitrary shell commands, AppleScript, executable paths, and any other OS action (closing apps, windows, keyboard/mouse, Finder or browser automation).
- This restriction applies to `open_app` and remains in force. The only exception is the separately specified, bounded computer-control capability in Section 3a.
- **Windows/Linux adapters remain future work;** other OSes return "only supported on macOS".

## 3a. Computer Control (bounded, local macOS; full specification in `07_Computer_Control.md`)
Computer control is a **controlled, bounded, local macOS capability**. It is not a general OS-execution channel. It is the only exception to the Section 3 restriction on other OS actions, and only within the scope defined in `07_Computer_Control.md`. Phase CC-1a (native macOS foundation) is limited to:
- a **closed, code-owned action vocabulary**:
  - activate an already-running app;
  - focus inside a validated native app;
  - set or select text in validated Cocoa text areas;
  - press validated known-state native controls;
  - scroll validated native scroll areas;
- a **code-owned safety gate**. MEDIUM and HIGH actions are blocked. Secure fields and Evie's own UI are never targeted;
- **validated mechanisms only** (macOS Accessibility and `NSRunningApplication`). A schema entry never enables an operation by itself;
- **no arbitrary shell** and no AppleScript for UI actions;
- **no destructive or high-risk actions**;
- **no browser, Finder or keyboard automation yet.** These are CC-1b and are each gated by a validation experiment;
- **session and action limits:** at most 40 actions, 3 consecutive no-ops end the session, 180 s wall-clock limit, one active session;
- **cancellation** checked before every action;
- **operation-specific verification** after every action;
- **stale-target protection:** re-resolved against the live accessibility tree immediately before acting.

Computer control enters the router as **one** high-level registered tool, `computer_control`, which starts or queues a session. The multi-step loop lives inside `ComputerControlSession`, not in the planner. So the Section 2 rule that the planner runs once per message and never receives tool results is unchanged. `open_app` is unchanged.

**Enrollment requirement:** before first sensitive-action use, the user records a short reference voice sample (a few seconds) used to generate a speaker embedding, stored locally, never transmitted.

**Voice security rule (as built):** `/voice/command` runs read-only tools without speaker verification. Mutating or external actions — currently `resolve_finding`, calendar writes (`propose_calendar_event`), and Gmail cleanup (`propose_gmail_cleanup`), all registered with `is_sensitive = 1` — run on voice only after local SpeechBrain speaker verification of the sample captured with that command passes. The check happens after the tool is selected and validated and before any proposal is created. Any failure (no sample, not enrolled, wrong speaker, model unavailable, invalid configuration) fails closed, so nothing executes merely because the planner selected it. After verification, confirmation is click-only, as in chat. Replay of a recording and audio/transcript mismatch are accepted residual risks. See Backend Schema Sections 4-5.

## 4. Canonical Security Score (bug fix requirement, high priority)
Currently the dashboard, chat, and briefing can disagree (observed: "0/100 — Grade D" while showing 211 active findings and a 0/0/0/0 breakdown). **There must be exactly one function that computes the security score**, called by every consumer (dashboard, chat, briefing) — no component may calculate its own version. See Backend Schema Section 6 for the canonical table.

Also fix the known frontend bug: `score || 100` treats a genuine `0` score as falsy and silently replaces it with `100`. Use explicit `null`/`undefined` checks, never falsy-coercion, for numeric scores anywhere in the UI.

## 5. GitHub Integration — Current State and Required Fixes
- **Working:** repository discovery (personal/private/org), activity tracking, secret scanning (pattern + entropy), PR monitoring, webhook processing (HMAC-SHA256 verified, delivery-ID idempotent).
- **Known issue, partially fixed:** sync time was 176s → 128s for 25 repos after removing duplicate scans and adding `github_scanned_commits` tracking (skip already-processed SHAs). Continue this direction — the real scalability target is **"how little work is done when 3 of 1,000 repos changed,"** not "how fast can 25 repos be scanned."
- **Delta processing (as built, Protected Area #5):**
  - An unchanged `pushed_at` skips all per-repository API work.
  - A changed repository is secret-scanned newest-first until the first already-scanned SHA, with a hard cap of 100 commits per repository per sync.
  - `github_scanned_commits` records only SHAs whose content was actually scanned. Webhooks never mark SHAs as scanned, since push payloads have no diffs; the next sync scans them.
  - A failed or incomplete scan does not advance `last_pushed_at`, so the delta is retried. Successful scans newer than a failed commit are not recorded, so they cannot hide it.
  - A finding already marked `false_positive` for the same repo, file, line and pattern is not re-flagged.
  - The sync reports `repositories_changed`, `repositories_skipped` and `repositories_incomplete`.
  - Webhooks fail closed without a configured `github_webhook_secret`.
- **Required for real onboarding (deferred but documented):** replace PAT with a GitHub App — install → choose account/repos → approve narrow read-only permissions → webhook-driven updates. Needed before supporting company/org repositories at all, since org owners control app installation approval.
- **Localhost limitation (acknowledged, not urgent):** GitHub cannot deliver webhooks to `127.0.0.1`. Fine for current local development; a reachable endpoint (tunnel or eventual hosting) is required only when real event delivery is needed — not the current priority.

## 6. Gmail Integration — Current State and Required Fixes
- **Working:** live OAuth (desktop client, refresh token in keyring), metadata-only fetching (not full bodies), classification into important/transactional/promotional/phishing-alert, cleanup proposals (Detect → Propose → Confirm, never silent).
- **Required fix:** phishing and promotional/spam are being conflated in some responses — these are different questions and must be two distinct classification calls/tools, never one substituting for the other.
- **Required addition:** integrate **Laya** (`noul` primitive) as a free, local, fast first-pass phishing classifier. Gate on its confidence score; escalate low-confidence/ambiguous cases to the LLM-based classifier rather than trusting Laya alone at zero-shot accuracy (its own benchmarks show ~0.36 zero-shot vs. ~0.77 fine-tuned — treat as a first-pass filter, not a final verdict, until fine-tuned on labeled data).
- **`phishing_alert` is a heuristic classification, not proof of malice** — preserve this framing in all user-facing copy.
- **Incremental sync:** `gmail_sync_checkpoints` table exists; needs to evolve into a complete incremental history mechanism (avoid re-downloading previously seen messages).

## 7. Calendar — Confirmed Pattern, Keep As-Is
Propose → Confirm → Execute, with temporary proposals, 15-minute expiry, atomic claiming, confirmation endpoints. This is the correct safety pattern and should be the template for how voice-triggered sensitive actions are confirmed too (see Section 3).

## 8. Non-Functional Requirements (updated)
- **Cost:** near-zero recurring (small-model tool-calling calls only; no continuous audio-streaming API such as OpenAI Realtime — deliberately rejected after evaluating HeyClicky's architecture).
- **Test suite (split implemented, Protected Area #4):** fast unit/mocked tests (run on every change) vs. explicit live-API tests (GitHub/Gmail/OAuth), so live tests don't slow normal development iteration.
  - **Fast (default):** `python -m pytest tests -q` — 244 tests in ~6s. `tests/conftest.py` gives every non-live test a boundary:
    - the real OS keyring returns nothing, and writes are refused;
    - outbound sockets and DNS are blocked, and any attempt fails the test even if production code swallows the error;
    - speaker-verification paths point at temporary directories.
  - **Live (explicit):** `python -m pytest tests -m live -q` — 3 read-only tests in `tests/test_live_integrations.py`: a Google OAuth refresh, GitHub repository discovery, and one Gmail metadata fetch. They use real keyring credentials and network, and skip when credentials are absent.
  - The `live` marker and default deselection are configured in `pyproject.toml` (`[tool.pytest.ini_options]`).
  - Use `python -m pytest`: the standalone `pytest` executable in the current conda environment has a broken interpreter path, and `uv run` hits the pre-existing packaging error.
  - Before the split, the single suite was 236 tests in ~5 minutes and made real OAuth, GitHub, and Gmail calls with the developer's keyring credentials.
- **Scalability:** delta-based processing is the core non-functional requirement, not raw scan speed at small repo counts.
- **Portability:** voice execution adapters are OS-specific by design (`osascript` for Mac now; Windows/Linux adapters added later behind the same tool interface) — do not attempt all three simultaneously. Computer control (Section 3a) is macOS-only. It uses the Accessibility API in a dedicated local worker process, not `osascript`.

## 9. Structured Chat/Dashboard Rendering (required, currently raw text)
Chat and dashboard responses must render as UI components — cards, tables, status badges, timelines, expandable details — generated from structured data the model returns, not long bullet-text blocks. The language model produces the explanation; the UI layer handles presentation.

## 10. Open Architectural Items (carried from status report, still unresolved)
- Event-based cross-source time queries ("what changed today," "while I was away") must resolve the time expression independently of entity extraction and return actual events for that window, not aggregate counts.
- Real-time dashboard updates need to consume the existing webhook foundation end-to-end (webhook → process delta → update DB → push to UI), not just process the event server-side.
