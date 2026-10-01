# Computer Control: Validation Evidence (Phase 0 / 0b)

*Permanent summary written 2026-09-28. This is the evidence behind the "validated" claims in `docs/07_Computer_Control.md`.*

## Provenance (read first)
- **The original raw artifacts were temporary** and are not preserved. That includes the probe scripts, JSON result files, fixtures and virtual environments.
  - The first Phase 0b set (`ax_walk.py`, `observe_round2/3.py`, `action_validate{,2,3}.py` and their JSON output) was under `/private/tmp/claude-501/…/74b5e294…/scratchpad/phase0b/`. It was **already deleted** by the time of the capability audit. Those results survive only as the reports in that session's transcript.
  - The later set (`activation_probe.py`, `axpress_probe.py`, `keyboard_probe.py`, and the `method1_*`, `axpress_run` and `keyboard_run` JSON) was under `/private/tmp/claude-501/…/a7217673…/scratchpad/activation/`. It is expected to disappear the same way.
- **The figures below are copied from those results.** They cannot be regenerated from preserved raw data.
- No secrets, user content, screenshots or environments are stored here. All fixtures were Evie-created scratch files.

## Environment
- macOS 26.2 (25C56), Apple M1, 8 GB RAM.
- Python 3.13.13 (conda) in a scratch virtual environment, PyObjC 12.2.2.
- **Runner:** Antigravity IDE → its helper process → zsh → claude → zsh → python. macOS attributes the runner to **Antigravity IDE** (`com.google.antigravity-ide`, pid 2494), which holds the Accessibility grant. `AXIsProcessTrusted()` = True.
- **Scope:** every result applies to that runner only. Evie's production runtime has not been tested.

## 1. Application activation
| Trial | Starting state (3 readings held for 3 s) | Mechanism | API result | After (independent) | Result |
|---|---|---|---|---|---|
| Phase 0b run A (ACT‑A) | Antigravity frontmost | `AXFrontmost = true` on TextEdit | success | NSWorkspace still showed Antigravity | EXECUTED BUT NOT VERIFIED |
| Phase 0b run C (ACT‑C) | Antigravity frontmost; TextEdit inactive | `AXFrontmost = true` | success, 7 ms | Antigravity still frontmost; TextEdit `AXFrontmost` and active both false | **FAILED** |
| `method1_trial2` (22:55:02) | Antigravity frontmost on NSWorkspace (event loop pumped), NSWorkspace in a fresh process, and `lsappinfo`; TextEdit running and inactive | `NSRunningApplication.activate(options: 3)` (all windows plus the deprecated ignoring-other-apps flag) | `true`, 54.3 ms | All 3 readings showed TextEdit; `isActive` and `AXFrontmost` both true; NSWorkspace changed 55.5 ms after the call | **PASS** |
| `method1_chrome_front` (22:57:00) | Chrome frontmost on the same 3 readings | same | `true`, 50.6 ms | same; changed after 54.8 ms | **PASS** |

- **Not tested:** LaunchServices open with activation, `open -a`, and the production `osascript … activate` (it has never launched an app live); other target apps; repeated trials; full-screen apps or Spaces.
- In the Chrome trial the starting condition was already met, so the user didn't need to intervene.

## 2. AXPress (`axpress_run`, 23:04)
- **Fixture:** a scratch RTF containing only `EVIE_AXPRESS_FIXTURE` in one explicitly left-aligned paragraph. TextEdit stayed in the background (Chrome frontmost). The fixture window was TextEdit's main and focused window.
- **Target:** the format-bar `AXCheckBox`/`AXSegment` "align center". Its only action is `AXPress`; `AXIdentifier` is null; 16 supported attributes.
- **Staleness check:** re-resolved at 23:04:15.822 from the live tree (app → window by `AXDocument` → segment by role, subrole and description). Checked `CFEqual`, role, subrole, value, enabled state, frame, that `AXPress` is still offered, window frame, first-character position, and disk. Everything matched.
- **Press:** `kAXErrorSuccess`, 103.3 ms.
  - Segments went from left 1 / centre 0 to left 0 / centre 1.
  - The first character's position (`AXBoundsForRange`) moved from x 204.0 to 429.6, with y unchanged.
  - Window frame, character count (21) and disk were unchanged.
- **Reversal:** "align left" pressed once, 102.5 ms. Segments and x = 204.0 were restored exactly. **PASS** for both.
- **Afterwards:** TextEdit **autosaved** the fixture at 23:04:46 (86 → 317 bytes; the text is the same, but the explicit `\ql` alignment code is gone). The UI reversal did not restore the file bytes.

## 3. AX text insertion (`keyboard_run`, 23:12)
- **Fixture:** a scratch `keyboard_fixture.txt` containing exactly `EVIE_FIXTURE_MARKER` (19 characters), cursor at [0,0]. TextEdit stayed in the background.
- **Target:** `AXTextArea`, `AXIdentifier` "First Text View", `AXFocused` true. `AXEnabled` is unsupported. AX reports `AXSelectedText`, `AXValue`, `AXSelectedTextRange` and `AXFocused` all settable.
- **Focus:** already correct. The fixture was TextEdit's main and focused window, and the focused element was the target. No focus operation was performed.
- **Input:** one set of `AXSelectedText` = `EVIE_KEYBOARD_TEST`. `kAXErrorSuccess`, 6.2 ms, settled in 251 ms.
  - After: `EVIE_KEYBOARD_TESTEVIE_FIXTURE_MARKER`, an exact match. Delta +18 as expected; cursor at [18,0]. **PASS**.
- **Cleanup:** set `AXSelectedTextRange` to [0,18]; re-read the selection, which was exactly the test string; then set `AXSelectedText` to "". The text was back to exactly the marker, 19 characters, cursor [0,0]. **RESTORED AND VERIFIED**. There was no autosave in the next 45 s.
- **This is not keyboard delivery.** No key events were sent.

## 4. Earlier Phase 0b results (summary)
- **Text:** `AXValue` set on an empty TextEdit text area [V]; clear [V].
- **Focus inside an app:** `AXRaise` + `AXMain` + `AXFocused` [V] (TextEdit in the background).
- **Scroll:** scroll bar 0 → 0.25; visible text moved from character 0 to 2,668; the reverse restored it [V].
- **Close:** after the close button press, the element read invalid and no sheet appeared. The CG on-screen window list was the wrong cross-check. **Executed but not verified**.
- **Observation:**
  - Windows are reachable only through `AXFocusedWindow`/`AXMainWindow`; `AXWindows` returned 0.
  - Menu bars are 330–660 nodes.
  - Settling has a floor of about 1 s.
  - Safari's menu item count oscillates.
  - Chrome's `AXWebArea` had 154 nodes in rounds 2–3 but was absent in round 1 (cause unknown).
  - The `AXEnhancedUserInterface` setter returned `NotImplemented` while `IsSettable` said true: **invalid experiment**.
  - Finder's window walk took 3.8 s cold and 2.0 s warm (before settling).
  - App discovery was a 4-folder scan only (91 bundle IDs; misses CoreServices).

## 5. Harness lessons
1. **An API success is never proof.** `AXFrontmost` returned success and did nothing.
2. NSWorkspace can be stale without a pumped main-thread event loop. Cross-check with a fresh process and `lsappinfo`.
3. **Starting states must be proven, not assumed.** Chatting with the assistant through Chrome kept stealing the foreground. Condition-based waiting (3 readings, held for 3 s) fixed it; fixed delays did not.
4. The CG *on-screen* window list is not a valid "window closed" oracle. Background windows (probably on another Space) are listed as off screen.
5. AX can expose only one of an app's open windows. "Nothing else changed" can't be proven through AX for windows it doesn't expose.
6. Commands that go through the app, such as the format bar, act on the app's main document. A main-window guard is required.
7. Scratch apps and windows disappear on their own (TextEdit quit itself; windows closed). Re-verify fixtures immediately before every action.
8. UI, application/document and filesystem state are separate layers. Autosave can write after a reversed UI change.
9. macOS 26 exposes no `AXZoomButton`; the green title-bar button is `AXFullScreenButton`.
10. Labels are English, and `AXIdentifier` is often null.

## 8. Bounded press_key (Steps 15A–15C, 2026-09-29)
- **Permission (15A, read-only, signed Evie):** `CGPreflightPostEventAccess` = true and Accessibility trusted. No new TCC permission was needed.
- **Mechanism probe (15B, validation-only):** `CGEventPostToPid` to the frontmost scratch TextEdit, key-down plus key-up. RIGHT (124) moved the caret 0 → 1; LEFT (123) moved it 1 → 0; text unchanged.
- **Production (15C):** the `press_key` op supports RIGHT_ARROW and LEFT_ARROW only, through the production gate and executor, verified by `AXSelectedTextRange` (+1 then −1, text unchanged). **PASS [V].** No other keys, modifiers, shortcuts or text events.

## 9. focus: intentionally UNVALIDATED/DISABLED (Steps 16/16A, 2026-09-30)
- The Phase 0b scratch result (§4: AXRaise + AXMain + AXFocused under Antigravity) was **never reproduced deterministically under signed Evie**. Two TextEdit documents' text areas are indistinguishable to the resolver (`AMBIGUOUS_TARGET`), and a single window has no second validated focus target.
- **Consequence:** the registry marks focus UNVALIDATED; it is absent from `LIVE_OPS`, `CHOOSER_OPS` and `ORCHESTRATABLE`; and direct session proposals are rejected. The implementation is kept, unreachable. No live focus mutation was performed in Step 16/16A.

## 10. Phase 1a closure review (2026-09-30)
- **Live-validated under signed Evie [V]:** `activate_app` (TextEdit ↔ Finder, 3-source frontmost), `set_text` (exact text and restore), `scroll` (±0.25, visible start restored), alignment `press` (state and glyph), `press_key` RIGHT/LEFT (caret ±1).
- **Not live:** `focus` (UNVALIDATED/DISABLED) and `select_text` (registry-only residual; the executor refuses it).
- **Open acceptance items:** `computer_control_steps` logging (PLANNED); §18 end-to-end benchmark not run as defined.

## 11. Step 17: logging and the Section 18 benchmark (2026-09-30)
- **Logging:** `computer_control_steps` is as built. There is one redacted row per attempted decision; nothing is dispatched without a ready log.
- **Section 18 benchmark:** one run under signed Evie through the router, **7/13 (53.8 %)**. Passed: 7, 8, 9, 10, 11, 12, 13.
- **Failed:**
  - 1–2: Chrome alias vs allowed-app scope; "switch back to" grammar;
  - 3: fixture `open` harness failure;
  - 4: `NO_TEXT_TARGET` on a multi-paragraph type;
  - 5: no selection mechanism;
  - 6: phrasing not in the grammar.
- **Audit:** 27 rows, no sensitive text.

## 12. CC-1b.1: routing correctness and evidence (2026-09-30)
- **Changes:** a shared closed alias table (Chrome), "switch back to X", bounded alignment phrases, and harness race fixes (`ensure_chrome_window()` preflight, `open_in_foreground` fixture setup). Zero production computer-control code changes (`services/computer_control/*` untouched).
- **Two-paragraph `set_text` via the production plan: PASS [V].** Exact text 19 → 94 → 19 chars; fixture file unchanged.
- **Section 18 benchmark, CC-1b.1 harness scope completed: 12 / 13 (92.3 %)** overall pass rate (**12/12 = 100 %** executable pass rate), up from 10/13 baseline. Unexpected operations: 0, retries: 0, fixture-open retries: 0, 43 audit log rows with no sensitive text.
  - **Task 2 (Chrome switch back): PASS [V].** Diagnostic established Chrome had 0 open windows in benchmark env, causing WindowServer focus yield back to TextEdit. Added harness preflight check `ensure_chrome_window()`. Production activation logic unchanged.
  - **Task 8 (Two-document TextEdit fixture): PASS [V].** Diagnostic established back-to-back background `open -g -F` reset window state during initialization. Resolved via staged foreground open (`open_in_foreground`). Production AX observer unchanged.
  - **Passed:** 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13.
  - **Failed / Non-executable:**
    - **5:** Task 5 remains intentionally deferred/non-executable because selection mechanism (`select_text`) is not live.
