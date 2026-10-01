# Evie — Acceptance Criteria & Test Plan
*Revision 2 — includes fixes for bugs actually found in testing, plus new voice/speaker-verification criteria*

## 1. Canonical Security Score
- [x] Dashboard, chat, and briefing all display the identical score and grade for the same underlying state (regression test for the observed 0/100-vs-211-findings bug). *(Evidence: `tests/test_canonical_security_score.py` `test_all_surfaces_report_identical_canonical_score` passed.)*
- [x] A genuine score of `0` displays as `0`, never silently replaced with `100` (regression test for the `score || 100` bug). *(Evidence: `tests/test_canonical_security_score.py` `test_zero_score_is_preserved_on_every_surface` and `test_frontend_uses_canonical_grade_and_explicit_null_checks` passed.)*
- [x] Score recalculates immediately after any finding is created or resolved. *(Evidence: `tests/test_canonical_security_score.py` `test_webhook_secret_finding_creation_recomputes_score` and `test_resolve_endpoint_recomputes_score` passed.)*

## 2. GitHub Secret Scanner
- [ ] A known fake AWS-format key in a new commit is flagged within one sync/webhook cycle.
- [ ] A commit with no secrets produces zero findings.
- [ ] A previously-scanned commit SHA (in `github_scanned_commits`) is never rescanned.
- [ ] Marking a finding false-positive prevents re-flagging on the same file/line.
- [ ] Historical/resolved findings are never displayed as current/open findings (regression test for the earlier bug where this was conflated).

## 3. GitHub Sync Performance
- [x] Re-running sync with zero repository changes completes in near-zero time (delta check only, no rescanning). *(Evidence: `tests/test_incremental_monitoring_and_router.py` `test_github_incremental_skip_and_webhook` passed.)*
- [x] A sync where 3 of N repos changed processes only those 3, not all N. *(Evidence: `services/github_intelligence.py` `last_pushed_at` timestamp delta skip & `tests/test_incremental_monitoring_and_router.py` passed.)*
- [ ] Webhook delivery is rejected if HMAC signature is invalid.
- [ ] A duplicate webhook delivery ID is not processed twice.

## 4. Gmail Classification
- [x] Phishing and promotional/spam classification are produced by two distinct checks — a promotional email is never returned as a phishing alert or vice versa. *(Evidence: `tests/test_gmail_phase_a.py` `test_promotional_email_remains_promotional_and_not_automatically_phishing` passed.)*
- [x] Laya's `noul` primitive is used as first-pass phishing classification; results below the confidence threshold are escalated to the LLM classifier, not trusted directly. *(Evidence: `tests/test_gmail_phase_a.py` `test_laya_available_path` and `test_low_confidence_escalation` passed.)*
- [x] A `phishing_alert` result always displays with clear "heuristic, not proof" framing in the UI. *(Evidence: `ui/index.html` line 109 notice tag and `tests/test_dashboard_state_phase_c.py` passed.)*
- [x] A message already seen in a prior sync (per `gmail_sync_checkpoints`) is not re-downloaded/re-classified from scratch. *(Evidence: `tests/test_gmail_phase_a.py` `test_checkpoint_creation_and_reuse` and `test_only_new_history_changes_processed` passed.)*

## 5. Calendar & Gmail Cleanup (Propose/Confirm pattern)
- [ ] A calendar or cleanup proposal is never executed without explicit confirmation.
- [ ] An unconfirmed proposal expires after its defined window (15 minutes for calendar) and cannot be confirmed afterward.
- [ ] Concurrent confirmation attempts on the same proposal are handled atomically (only one succeeds).

## 6. Tool-Calling Router (new)
- [x] A free-form message not matching any previously hardcoded phrase is still correctly routed to the right tool (e.g., "get rid of that leaked key from yesterday" resolves to `resolve_finding` with the correct finding ID). *(Evidence: `tests/test_tool_calling.py` `test_free_form_message_reaches_correct_tool_with_finding_id` passed.)*
- [x] The model never executes an action outside the `tool_registry` — attempting to coax it into an undefined action results in no execution and a clear rejection. *(Evidence: `tests/test_tool_calling.py` `test_unregistered_llm_tool_is_rejected` and `test_tool_without_registry_row_is_rejected` passed.)*
- [x] The same router produces consistent tool selection whether invoked from chat or from voice, for equivalent phrasing. *(Evidence: `tests/test_tool_calling.py` `test_chat_and_voice_use_the_same_router` and `test_llm_tool_selection_is_identical_for_chat_and_voice` passed.)*

## 7. Voice Command Pipeline (new)
- [x] A spoken command is correctly transcribed via the Web Speech API and passed to the tool-calling router. *(Evidence: `ui/voice.js` Web Speech API integration, `main.py` `/voice/command` endpoint, `tests/test_speaker_verification.py` passed.)*
- [x] A command resolving to a tool marked `is_sensitive = 1` does not execute until speaker verification passes. *(Evidence: `tests/test_speaker_verification.py` `test_verified_voice_creates_verified_proposal_and_click_confirm_executes` and `test_unverified_voice_blocked` passed.)*
- [x] A command spoken by a different voice than the enrolled reference is rejected for sensitive actions (tested with a second person's voice or a recording). *(Evidence: `tests/test_speaker_verification.py` `test_sensitive_action_fails_closed_on_unverified_voice` passed.)*
- [x] A non-sensitive command (e.g., "open Safari") executes without triggering speaker verification or a confirmation step. *(Evidence: `tests/test_open_app.py` `test_voice_uses_the_same_router_without_verification_or_confirmation` passed.)*
- [x] Every processed voice command is logged in `voice_command_log` with its resolution outcome, whether executed or rejected. *(Evidence: `tests/test_speaker_verification.py` `test_voice_command_log_records_every_outcome` passed.)*
- [x] Total cost per voice command stays in the sub-cent range (no continuous audio-streaming API is used at any point in the pipeline). *(Evidence: browser Web Speech API $0 STT + SpeechBrain local verification + single Anthropic tool planner call.)*

## 8. Speaker Verification Enrollment
- [x] Enrollment produces and stores a local reference embedding file; nothing is written to SQLite or transmitted externally. *(Evidence: `tests/test_speaker_verification.py` `test_enrollment_flow` passed.)*
- [x] Re-running enrollment overwrites the previous reference cleanly (no orphaned old embeddings). *(Evidence: `tests/test_speaker_verification.py` `test_reenrollment_overwrites_existing_reference` passed.)*

## 9. Memory / Journal
- [ ] A journal entry is retrievable via natural-language date-based query ("6 months ago today").
- [ ] A topic-based query returns semantically relevant entries via the SQLite+NumPy cosine similarity search, not random entries.

## 10. Dashboard-First Experience
- [x] Opening the dashboard with no user input displays the overall Good/Needs Attention/Critical state and all section summaries without requiring a chat query. *(Evidence: `tests/test_ui_phase_d.py` `test_index_html_dashboard_first_structure` and `test_app_js_dashboard_and_score_zero_handling` passed.)*
- [x] A "what changed today" style query returns actual events for that resolved time window, not an aggregate count. *(Evidence: `ui/app.js` `renderToolUI` `timeline` rendering & `tests/test_incremental_monitoring_and_router.py` passed.)*

## 11. General / Cross-Cutting
- [ ] No secret, password, token, or speaker-verification embedding appears in any log file, database row, or terminal output — verified by a full-text search after a test run that intentionally triggers findings and a voice enrollment.
- [ ] Fast test suite (unit/mocked) runs independently of and faster than the full live-API suite.
- [ ] The system's monthly API cost, extrapolated from a week of realistic use including voice commands, stays near-zero beyond the existing LLM/HIBP budget.

## 12. Computer Control — CC-1a (PLANNED; see `07_Computer_Control.md`)
- [x] Only validated operations execute. An operation whose mechanism is not validated is refused even if its schema exists. *(Evidence: registry + `LIVE_OPS`; `focus` refused at every entry point — 07 §36 Step 16A, §37.)*
- [x] No API return value alone produces SUCCESS. Every SUCCESS has operation-specific verification evidence (activation, exact text, control state, scroll range). *(Evidence: derived ActionResult status; activation/key-post returns are never proof; live runs 07 §36.)*
- [x] A stale target (changed between choosing and acting) produces STALE and no action. An ambiguous target produces ASK. A missing target produces BLOCKED. *(Evidence: resolver/executor/hardening fast tests.)*
- [x] MEDIUM and HIGH actions, secure fields, sensitive apps, and Evie's own UI are blocked in CC-1a. *(Evidence: gate fast tests.)*
- [x] Text typed by `set_text` is always a span of the user's verbatim utterance. Instructions shown on screen cannot trigger actions or widen the allowed-app scope. *(Evidence: provenance/hardening tests; injection cases in the chooser benchmark.)*
- [x] Stop halts the session before its next action. The loop ends at 40 actions, after 3 consecutive no-ops, or after 180 s. Only one session is active at a time. *(Evidence: session + Step 11 integration tests.)*
- [x] A session does not start if the worker lacks functional Accessibility trust. Activation and AX validation have been re-run under the real worker runtime. *(Evidence: signed Evie runtime, 07 §33–§36.)*
- [x] `open_app` behaviour and its existing tests are unchanged. *(Evidence: `tests/test_open_app.py` 56 passed at closure.)*
- [x] Every attempted action is logged (redacted) in `computer_control_steps`. *(Evidence (Step 17): one redacted row per attempted decision via the orchestrator audit port; no dispatch without a ready log; 27 rows in the §18 run with zero sensitive-text hits — 07 §37.)*
- [x] The CC-1a / CC-1b.1 benchmark (`07_Computer_Control.md` Section 18) has a recorded success rate. *(Evidence: Step 17 baseline **7/13 = 53.8 %**; CC-1b.1 final harness run **12/13 = 92.3 %** overall, **12/12 = 100 %** executable pass rate with Task 5 intentionally deferred — 07 §38.)*
