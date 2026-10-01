# Evie — Backend Schema Document
*Revision 2 — reconciled with actual implementation, canonical score fix, voice/speaker-verification additions*

## 1. Storage Overview (confirmed as built)
- **SQLite** for all structured data — findings, activity, scores, journal, checkpoints.
- **SQLite + NumPy (dense feature hashing + cosine similarity)** for semantic memory search — **not Chroma**, this was a deliberate, good simplification. Keep it.
- **OS keyring** for all credentials (GitHub PAT, Google OAuth tokens, HIBP key, LLM key). Confirmed working, including a completed live Gmail OAuth refresh test.
- **Local file** for the speaker-verification reference embedding (new, Section 7) — never transmitted off-device.

## 2. Core Tables (as implemented, consolidated)

```sql
-- Secret scanner findings — confirmed distinguishing open vs. resolved,
-- fixed after an earlier bug treated historical findings as current
CREATE TABLE secret_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_number INTEGER,
    pattern_type TEXT,
    redacted_preview TEXT,
    status TEXT DEFAULT 'open',        -- 'open' | 'resolved' | 'false_positive'
                                       -- a new finding is not created when a false_positive row already
                                       -- covers the same repo_name/file_path/line_number/pattern_type (AC 2.4),
                                       -- nor when the same commit/file/line/pattern already exists
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);

-- Tracks already-processed commit SHAs so secret scanning is never repeated
-- on unchanged commits — this is the core fix behind the 176s -> 128s
-- sync-time improvement and the path to real delta-based scaling.
-- A row means "this exact commit SHA was actually secret-scanned successfully" (as built).
-- It is written only by the sync from the SHAs the scanner vouches for, never from
-- github_activity_log and never by webhooks (push payloads carry file names, not diffs).
CREATE TABLE github_scanned_commits (
    repo_name TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_name, commit_sha)
);

-- GitHub repository-level activity, persisted rather than rebuilt each sync
-- (as built; the earlier draft name was `github_activity`)
CREATE TABLE github_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,       -- 'commit' | 'pr'
    identifier TEXT NOT NULL,          -- commit SHA or PR number
    author TEXT NOT NULL,
    summary TEXT NOT NULL,             -- credential-pattern matches are redacted before storage
    html_url TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    is_unreviewed INTEGER DEFAULT 0,
    UNIQUE(repo_name, activity_type, identifier)
);

-- Per-repository sync checkpoint (as built). last_pushed_at is advanced only after the
-- repository's delta secret scan completed; an unchanged pushed_at skips all per-repo API work.
-- github_repo_cache(full_name PK, name, owner, is_private, is_org, html_url, last_synced, last_pushed_at)

-- GitHub webhook events (as built). delivery_id UNIQUE is the idempotency check:
-- a delivery ID already present returns "already_processed" and is not processed again.
CREATE TABLE github_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT UNIQUE,
    repo_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    summary TEXT,
    event_data TEXT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_realtime INTEGER DEFAULT 0,
    included_in_briefing INTEGER DEFAULT 0
);

-- GitHub webhook deliveries — record of HMAC-verified deliveries
CREATE TABLE github_webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,      -- GitHub's delivery ID
    event_type TEXT NOT NULL,
    repo_name TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    signature_valid INTEGER NOT NULL   -- 1 only when the HMAC-SHA256 signature verified; rejected deliveries are not stored
);
-- Webhooks fail closed: without github_webhook_secret in the keyring every delivery is
-- rejected (503); a missing or invalid X-Hub-Signature-256 is rejected (401).

-- Breach monitoring (HIBP)
CREATE TABLE breach_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_checked TEXT NOT NULL,
    breach_name TEXT,
    breach_date TEXT,
    data_classes TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    acknowledged INTEGER DEFAULT 0
);

-- Public exposure findings (Drive/Calendar), with trusted-domain config
CREATE TABLE exposure_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- 'drive' | 'calendar'
    item_id TEXT NOT NULL,
    item_name TEXT,
    exposure_type TEXT,                -- 'anyone_with_link' | 'public' | 'external_domain'
    domain TEXT,                       -- populated when exposure_type = 'external_domain'
    is_trusted_domain INTEGER DEFAULT 0,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'open'
);

CREATE TABLE trusted_domains (
    domain TEXT PRIMARY KEY
);

-- 2FA audit
CREATE TABLE twofa_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,            -- 'google' | 'github'
    twofa_enabled INTEGER,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Dependency vulnerability alerts (Dependabot)
CREATE TABLE dependency_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name TEXT NOT NULL,
    package_name TEXT,
    severity TEXT,
    advisory_url TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'open'
);

-- Gmail message metadata (metadata-only, never full body storage)
CREATE TABLE gmail_messages (
    message_id TEXT PRIMARY KEY,
    subject TEXT,
    sender TEXT,
    sender_domain TEXT,
    category TEXT,                     -- 'important' | 'transactional' | 'promotional' | 'phishing_alert'
    phishing_confidence REAL,          -- from Laya's noul primitive, when applicable
    phishing_source TEXT,              -- 'laya' | 'llm_escalation' | null
    risk_score REAL,
    unsubscribe_available INTEGER DEFAULT 0,
    received_at TIMESTAMP
);

-- Incremental Gmail sync tracking
CREATE TABLE gmail_sync_checkpoints (
    account_email TEXT PRIMARY KEY,
    last_history_id TEXT,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Gmail cleanup proposals (Detect -> Propose -> Confirm, never silent) — as built
CREATE TABLE gmail_cleanup_proposals (
    proposal_token TEXT PRIMARY KEY,
    sender_domain TEXT NOT NULL,
    action_type TEXT NOT NULL,         -- 'unsubscribe_recommendation' | 'delete_newsletter'
    status TEXT NOT NULL,              -- 'pending' | 'confirmed' | 'rejected' | 'expired'
    created_at TEXT NOT NULL,          -- UTC ISO 8601; the proposal expires 15 minutes after this
    executed_at TEXT                   -- set when confirmed/rejected
);
-- Lifecycle (same pattern as calendar proposals):
-- - A proposal older than 15 minutes cannot be confirmed or rejected; it is marked 'expired'.
-- - Confirmation is one atomic UPDATE ... WHERE proposal_token = ? AND status = 'pending'
--   AND created_at > now - 15 minutes. Its row count decides success, so exactly one of several
--   concurrent attempts wins; the others get PROPOSAL_ALREADY_PROCESSED.
-- - Confirming changes only the proposal status; no Gmail action is executed.

-- Calendar proposals — confirmed Propose -> Confirm -> Execute pattern,
-- 15-minute expiry, atomic claiming as already implemented
CREATE TABLE calendar_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,         -- 'create' | 'update' | 'delete'
    event_summary TEXT,
    proposal_data TEXT,                -- JSON of the proposed event
    status TEXT DEFAULT 'pending',     -- 'pending' | 'confirmed' | 'expired' | 'claimed'
    proposed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,              -- proposed_at + 15 minutes
    confirmed_at TIMESTAMP
);

-- Journal / memory entries
CREATE TABLE journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date DATE NOT NULL,
    content TEXT NOT NULL,
    source_data TEXT,
    embedding_vector BLOB,             -- dense feature-hash vector, NumPy-serialized
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- DRS query log — table exists, feature paused (Section 9 of PRD)
CREATE TABLE drs_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer_summary TEXT,
    sources TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 3. Canonical Security Score (fixes the known 0/100-vs-211-findings inconsistency)

```sql
-- The ONLY table any component may read for the current score.
-- No dashboard/chat/briefing code may compute its own score independently.
CREATE TABLE security_score_current (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- single row, recomputed in place
    score INTEGER NOT NULL,                 -- 0-100, explicit integer, never coalesced with `|| 100`
    grade TEXT NOT NULL,                    -- 'A' | 'B' | 'C' | 'D' | 'F'
    secrets_deduction INTEGER DEFAULT 0,
    breaches_deduction INTEGER DEFAULT 0,
    exposure_deduction INTEGER DEFAULT 0,
    twofa_deduction INTEGER DEFAULT 0,
    dependencies_deduction INTEGER DEFAULT 0,   -- critical/high Dependabot alerts (part of the existing SCORE-01 formula)
    active_findings_count INTEGER DEFAULT 0,    -- total open findings counted by the same recompute
    counts TEXT,                                -- JSON per-category open-finding counts from the same recompute
    trend TEXT,                                 -- 'improving' | 'declining' | 'stable' vs. previous history row
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE security_score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score INTEGER NOT NULL,
    breakdown TEXT,                    -- JSON snapshot of the deduction breakdown
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
**Rule:** `security_score_current` is written by exactly one function (`recompute_security_score()`), called after any finding is created/resolved. Every consumer reads this table; none recomputes independently. Frontend must check the score with an explicit `score !== null && score !== undefined` guard — never `score || 100`.

**Canonical implementation columns (as built):** in addition to the core score/grade/deduction columns, `security_score_current` persists four fields, all written only by `recompute_security_score()` in the same transaction as the score:
- `dependencies_deduction` — the Dependabot deduction (critical/high open alerts, -10 each, max -30). It is part of the existing scoring formula; without it the stored breakdown would not sum to the score.
- `active_findings_count` — total open findings (secrets + unacknowledged breaches + open exposures + providers missing 2FA + critical/high Dependabot alerts). Deductions are capped, so this cannot be derived from them.
- `counts` — JSON object with the per-category counts behind the deductions: `open_secrets_count`, `unacknowledged_breaches_count`, `open_exposures_count`, `missing_twofa_count`, `high_dep_alerts_count`. Consumers (dashboard findings summary, chat breakdown) read these instead of re-counting findings, so every surface uses the same counting rules as the score.
- `trend` — `'improving'`, `'declining'`, or `'stable'`, comparing the new score to the most recent `security_score_history` row at recompute time.

Grade mapping (label only; does not affect the numeric score): `A` >= 85, `B` >= 75, `C` >= 60, otherwise `D`. `F` is permitted by the column but has no defined cutoff and is not currently emitted.

## 4. Voice Control & Tool-Calling Support (new)

```sql
-- Defines what the tool-calling router is allowed to execute.
-- The LLM can only ever select from rows in this table — it cannot invent
-- new actions. This table IS the allow-boundary described in the TRD.
CREATE TABLE tool_registry (
    tool_name TEXT PRIMARY KEY,        -- e.g. 'revoke_secret_finding', 'open_app'
    description TEXT,                  -- shown to the LLM as the tool's schema description
    parameters_schema TEXT,            -- JSON schema for expected parameters
    is_sensitive INTEGER DEFAULT 0,    -- 1 = requires speaker verification before execution
    requires_confirmation INTEGER DEFAULT 0 -- 1 = requires Propose/Confirm flow
);

-- Log of every voice command processed, for audit and debugging
-- (as built: services/voice_pipeline.py, one row per POST /voice/command)
CREATE TABLE voice_command_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_transcript TEXT NOT NULL,      -- transcript with credential-shaped tokens replaced by [REDACTED]
    resolved_tool TEXT,                -- FK-like reference to tool_registry.tool_name (NULL if no tool was selected)
    resolved_parameters TEXT,          -- JSON of the *validated* parameters (redacted); NULL if validation never passed
    speaker_verified INTEGER,          -- NULL if not required, 0/1 if checked
    executed INTEGER DEFAULT 0,        -- 1 only when a handler ran; a created proposal is 0
    rejected_reason TEXT,              -- reason code when rejected/blocked/failed (e.g. SPEAKER_MISMATCH, TOOL_NOT_REGISTERED)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Never stored here (or anywhere in SQLite): audio, speaker embeddings, similarity scores.

-- Pending Propose -> Confirm -> Execute requests for tool calls selected through the
-- tool-calling router (as built: services/tool_router.py). Used for every tool with
-- requires_confirmation = 1 whose handler would act directly - currently resolve_finding.
-- Calendar and Gmail cleanup keep their own proposal tables (calendar_actions,
-- gmail_cleanup_proposals); their tools only create proposals there.
CREATE TABLE tool_action_proposals (
    proposal_token TEXT PRIMARY KEY,   -- random UUID returned to the client; required to confirm
    tool_name TEXT NOT NULL,           -- tool_registry.tool_name that will run on confirmation
    arguments TEXT NOT NULL,           -- JSON of the already-validated tool parameters (e.g. finding_type, id, status)
    channel TEXT NOT NULL,             -- 'chat' | 'voice' - the entry point that produced the proposal
    status TEXT NOT NULL DEFAULT 'proposed',
                                       -- 'proposed' | 'claimed' | 'executed' | 'execution_failed'
                                       -- | 'cancelled' | 'expired' | 'rejected'
    created_at TIMESTAMP NOT NULL,     -- UTC ISO 8601
    expires_at TIMESTAMP NOT NULL,     -- created_at + 15 minutes
    executed_at TIMESTAMP,             -- set on every status transition after 'proposed'
    speaker_verified INTEGER NOT NULL DEFAULT 0
                                       -- 1 only for voice proposals created after speaker verification passed
);
```

**`tool_action_proposals` semantics (as implemented):**
- **Propose:** when the router selects a tool with `requires_confirmation = 1` (from the code definition or the `tool_registry` row, whichever is stricter), it validates the arguments, writes one `proposed` row, and returns the token and a confirmation prompt. The handler does not run. Nothing is changed at this point.
- **Sensitivity is not stored in the row.** It is read from the code definition and `tool_registry` at both propose and confirm time. A sensitive tool requested over voice reaches this table only after speaker verification passed, and the row records that as `speaker_verified = 1` (see the voice rule below).
- **Confirm (`POST /tools/confirm`):**
  - Unknown token → `not_found`; any status other than `proposed` → `already_processed`; nothing runs.
  - Past `expires_at` → row becomes `expired`; nothing runs.
  - `confirmed = false` → row becomes `cancelled`; nothing runs.
  - `confirmed = true` → atomic claim (`UPDATE ... SET status = 'claimed' WHERE proposal_token = ? AND status = 'proposed'`). Only the request that moves the row out of `proposed` can continue, so a proposal executes at most once, including under replays or concurrent confirmations.
  - After the claim, the tool must still be executable (code definition and `tool_registry` row) and the stored arguments must re-validate; otherwise the row becomes `rejected`. A row created on the `voice` channel for a sensitive tool is also rejected unless `speaker_verified = 1`.
  - The handler then runs once; the row ends as `executed` or `execution_failed`.
- Arguments never contain credentials; for `resolve_finding` they are `finding_type`, `id`, and `status`.

**Voice security rule (as built):**
- `/voice/command` may run read-only tools without speaker verification.
- Every mutating or external action is registered with `is_sensitive = 1`. This currently covers `resolve_finding`, `propose_calendar_event` (Google Calendar writes), and `propose_gmail_cleanup`. On the `voice` channel such a tool runs only after local speaker verification (Section 5) of the sample attached to that command passes. Verification runs after the tool is selected and its arguments validate, and **before** any proposal or confirmation is created.
- Any verification failure fails closed: the tool is rejected (`status = "blocked"`, reason `NO_VOICE_SAMPLE`, `INVALID_AUDIO`, `NOT_ENROLLED`, `REFERENCE_INVALID`, `SPEAKER_MISMATCH`, `VERIFICATION_UNAVAILABLE`, `VERIFICATION_MISCONFIGURED`, `VERIFICATION_ERROR`, or `SPEAKER_VERIFICATION_UNAVAILABLE` when a caller supplies no verifier), no proposal is created, and nothing executes merely because the planner selected it.
- After verification passes, the normal confirmation rules still apply, and confirmation is **click-only**: `resolve_finding` creates a `tool_action_proposals` row with `speaker_verified = 1` for `POST /tools/confirm`, and calendar/Gmail cleanup create their existing proposals for their existing confirm endpoints. There is no voice confirmation.
- The same requests remain available through chat, where they still require Propose → Confirm.
- Any new mutating or external tool must be registered with `is_sensitive = 1` so the voice gate applies to it.
- Note: `get_daily_briefing` is registered as non-sensitive; its only side effect is internal bookkeeping (marking overnight GitHub events as included in a briefing), not a change to user data or an external system.

**Computer control storage (IMPLEMENTED in CC-1a Step 17: `computer_control_steps` + the `computer_control` tool; the session endpoints in Section 6 remain PLANNED. Design in `07_Computer_Control.md`).**
- `computer_control` will be one `tool_registry` row with `is_sensitive = 0` and `requires_confirmation = 0`. Risk is decided per action inside the session, and MEDIUM and HIGH actions are blocked in CC-1a. Its voice commands keep one `voice_command_log` row each, with `resolved_tool = 'computer_control'`.
- One row per attempted action, including BLOCKED, STALE and CANCELLED:

```sql
CREATE TABLE computer_control_steps (          -- AS BUILT (Step 17): services/computer_control_log.py
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    op TEXT,                                   -- closed vocabulary name; NULL for DONE/BLOCKED/ASK decisions
    decision_source TEXT NOT NULL,             -- 'fast_path' | 'chooser' | 'plan' (code-owned plans used by validation harnesses)
    target_summary TEXT,                       -- redacted JSON: role, subrole, label, app bundle id; never secure-field or typed content
    gate_decision TEXT NOT NULL,               -- 'LOW' | 'MEDIUM' | 'HIGH' | 'FORBIDDEN' plus outcome
    status TEXT NOT NULL,                      -- SUCCESS | FAILED | BLOCKED | STALE | NOT_VERIFIABLE | CANCELLED | TIMEOUT
    execution TEXT,                            -- NOT_EXECUTED | EXECUTED | ERROR | TIMEOUT
    verification TEXT,                         -- VERIFIED | VERIFIED_NO_CHANGE | CHANGED_UNSPECIFIED | INCONCLUSIVE | NOT_APPLICABLE
    mechanism TEXT,
    ax_error INTEGER,
    latency_ms REAL,
    state_layers TEXT,                         -- JSON list: 'ui' | 'document' | 'filesystem'
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,   -- NULL / 0 in CC-1a (no chooser)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Never stored: raw AX element references, screenshots, secure-field content, text typed into
-- password fields, or full document contents.
```

## 5. Speaker Verification (as built: `services/speaker_verification.py`)
- Reference embedding stored as a local file (e.g. `speaker_reference.npy`), generated once during an enrollment flow, **never stored in SQLite, never transmitted**.
- Verification is a local comparison (cosine similarity above a threshold) between the incoming command's voice embedding and the stored reference — no external API call, no cost.
- Any `tool_registry` row with `is_sensitive = 1` must pass this check before execution proceeds to Propose/Confirm (see the voice rule in Section 4).

**Model.** SpeechBrain ECAPA-TDNN speaker encoder `speechbrain/spkrec-ecapa-voxceleb`, pinned to Hugging Face revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` (Apache-2.0). It produces 192-dimensional embeddings, expects 16 kHz mono input, and its model card reports 0.80% EER on VoxCeleb1-O.
- Optional dependency: `speechbrain>=1.1.1,<1.2`, via the `voice` extra or `requirements-voice.txt`. SpeechBrain pulls in torch and torchaudio.
  - **Install with `pip install -r requirements-voice.txt`.** `pip install .[voice]` / `uv` builds currently fail with setuptools' "Multiple top-level packages discovered in a flat-layout: ['ui', 'services', 'integrations']". That packaging issue is **pre-existing**: it reproduces with the pyproject.toml from before the voice extra was added. It is unrelated to speaker verification and not fixed here.
- Download is a separate, explicit, one-time step: `python -m services.speaker_verification download-model` fetches the pinned revision into `~/.evie/models/spkrec-ecapa-voxceleb` (override: `EVIE_SPEAKER_MODEL_DIR`).
- At runtime the model loads only from that local directory, with `FetchConfig(allow_network=False)`, so a request never downloads anything.
- If the package or model files are missing, verification reports `VERIFICATION_UNAVAILABLE` and sensitive voice actions stay blocked.

**Threshold.**
- The decision is `cosine(reference, candidate) > threshold`, the same comparison as SpeechBrain's `SpeakerRecognition.verify_batch`.
- The default is **0.25**, SpeechBrain's published default for `verify_batch` on this cosine score. It is the library's reference operating point, **not a value calibrated for the enrolled user**.
- It is configurable with `EVIE_SPEAKER_THRESHOLD`, which must be a number strictly between 0 and 1. Raise it to reduce false accepts at the cost of more false rejects.
- An invalid value fails closed (`VERIFICATION_MISCONFIGURED`).

**Audio input.**
- 16 kHz, mono, 16-bit PCM WAV, 1-15 seconds, at most 1,000,000 bytes, sent base64-encoded to the local backend only.
- Captured in the browser by `ui/voice_capture.js` (getUserMedia + Web Audio). `VoiceAdapter`/speech-to-text is unchanged.
- Decoded in memory and discarded; it is never written to disk, SQLite, or logs.
- Near-silent samples are rejected (`INVALID_AUDIO`).

**Storage.**
- `~/.evie/speaker_reference.npy` (override: `EVIE_SPEAKER_REFERENCE_PATH`), outside the repository.
- Directory mode 0700, file mode 0600. Written with a temp file plus atomic `os.replace` under a lock.
- Loaded with `allow_pickle=False` and validated (shape 192, finite, non-zero); a missing file means `NOT_ENROLLED` and an invalid one means `REFERENCE_INVALID`, both fail closed.

**Enrollment.**
- One sample (`POST /voice/enroll`).
- If a reference already exists, the request must carry `replace_existing = true`, which the UI sends only after an explicit click confirmation. Otherwise it returns 409 `REENROLLMENT_CONFIRMATION_REQUIRED` and nothing changes.
- Re-enrollment overwrites the single file cleanly; no older embeddings are kept.
- Responses contain status flags only, never the embedding.

**Residual risks (accepted, not addressed by this design).**
- *Replay:* a recording of the enrolled user played to the microphone can pass verification.
- *No liveness detection:* speaker embeddings measure voice similarity only; nothing checks that a live person is speaking, and no challenge phrase is used.
- *Audio/transcript mismatch:* the transcript comes from the browser's speech-to-text while the verification sample is captured separately, so the backend cannot prove both are the same utterance. Any local process that can call the API can pair a recording with a different transcript.
- *Scope:* speaker verification protects against a different person speaking to Evie. It does not protect against local software that can call the localhost API, which can already use `/chat` and the click-confirm endpoints.
- *Mitigation:* every sensitive voice action still requires a click confirmation after verification.
- In Chrome, the Web Speech API sends the *command* audio to the browser vendor for transcription, as it did before this change. The verification and enrollment samples themselves are sent only to the local backend.

**Validation record (Protected Area #3 closure, 2026-09-28).**
- **Real model:** the pinned model ran from `~/.evie/models/spkrec-ecapa-voxceleb` with outbound network connections blocked.
- **Real user, physical microphone, Chrome dashboard.** All passed:
  - enrollment with the user's voice
  - enrollment persisting across a browser refresh
  - the browser microphone → local WAV capture path
  - a read-only voice command (no verification invoked)
  - the user's voice on a sensitive Gmail cleanup command (verified)
  - sensitive proposal creation
  - click confirmation of that proposal
- **Privacy:** no audio or voice-print leakage observed.
- **Different-speaker behaviour, on real published recordings:** SpeechBrain's published test recordings (2 speakers × 6 utterances, 16 kHz mono) were run through Evie's own `enroll()` / `verify()` at the 0.25 threshold.
  - Same speaker: 10/10 accepted (cosine 0.49–0.73 across all pairs).
  - Different speaker: 12/12 rejected (cosine −0.11–0.10).
  - These scores were computed only in a diagnostic script and were never stored or logged by Evie.
- **Limitation:** a second *human* speaking into the physical microphone was **not personally verified** (no second person was available). Acceptance Criteria §7.3 is supported by the published-recording test above, not by a live second-person trial.
- **Automated suite:** 236/236 passed.

## 6. Internal API Endpoints (updated)
| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/dashboard/state` | Single call returning overall Good/Needs Attention/Critical + all section summaries — this is what the dashboard loads on open, no question required |
| POST | `/chat` | Free-form message through the tool-calling router |
| POST | `/voice/command` | Transcript (+ optional local `audio_wav_base64` sample) in, same tool-calling router as `/chat`; speaker verification runs only for sensitive tools and fails closed (Sections 4-5); every command logged in `voice_command_log` |
| POST | `/tools/confirm` | Confirm or cancel a pending `tool_action_proposals` row; executes at most once |
| GET | `/computer/sessions/{id}` | **PLANNED** (`07_Computer_Control.md`): session state and redacted step records |
| POST | `/computer/sessions/{id}/stop` | **PLANNED**: cancel the session; no further action executes |
| POST | `/voice/enroll` | Records and stores the reference speaker embedding from one sample; re-enrollment requires `replace_existing = true` after a click confirmation |
| GET | `/voice/enrollment` | `{enrolled, verification_available}` - no embedding data |
| GET | `/findings` | All open findings across categories |
| POST | `/findings/{id}/resolve` | Mark resolved/false-positive; triggers `recompute_security_score()`. Direct endpoint for explicit, trusted UI interactions (finding-card buttons); LLM-selected resolution uses the `resolve_finding` tool instead |
| GET | `/score` | Reads `security_score_current` — nothing else |
| POST | `/calendar/propose`, `/calendar/confirm` | Unchanged, confirmed working |
| POST | `/gmail/cleanup/propose`, `/gmail/cleanup/confirm` | Mirrors the calendar pattern |
| POST | `/webhooks/github` | Fails closed: rejected unless `github_webhook_secret` is configured and the HMAC-SHA256 signature verifies; idempotent via `github_events.delivery_id`; verified deliveries recorded in `github_webhook_deliveries` |
| GET | `/journal/search?q=` | Natural-language memory search |
