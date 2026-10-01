# Phase 1: Security Watchers & Keyring Foundations — Technical Research

## Stack & Library Analysis

### 1. Keyring Management (`keyring`)
- **Library:** `keyring` (Python package)
- **Backend:** macOS Keychain on Darwin / SecretService on Linux
- **Pattern:**
  ```python
  import keyring

  SERVICE_NAME = "evie_assistant"

  def set_credential(key: str, value: str) -> None:
      keyring.set_password(SERVICE_NAME, key, value)

  def get_credential(key: str) -> str | None:
      return keyring.get_password(SERVICE_NAME, key)
  ```

### 2. Secret Scanner (`github_watch.py`)
- **Targeted Regex Patterns:** Standard API key formats: GitHub tokens (`ghp_...`, `github_pat_...`), OpenAI API keys (`sk-...`), Anthropic API keys (`sk-ant-...`), AWS access keys (`AKIA...`), and Slack webhooks.
- **Targeted Shannon Entropy Calculation:**
  $$H(X) = -\sum_{i=1}^{n} P(x_i) \log_2 P(x_i)$$
  Calculated on discrete candidate secret tokens/words (length > 20, Shannon entropy > 4.5) rather than arbitrary source-code substrings to minimize false positives.
- **Redaction Boundary:**
  ```python
  def redact_secret(val: str | None) -> str:
      if not val or len(val) <= 4:
          return "****"
      return f"{val[:2]}...{val[-2:]}"
  ```
  Raw secret values exist transiently only during scanner evaluation; all findings, logs, and storage contain strictly redacted strings.

### 3. HIBP Breach Watcher (`breach_watch.py`)
- **Credential Retrieval:** HIBP API key retrieved from OS Keyring via `keyring_utils.get_credential("evie_assistant", "hibp_api_key")`.
- **Endpoint:** HTTPS `https://haveibeenpwned.com/api/v3/breachedaccount/{urllib.parse.quote(email)}?truncateResponse=false`
- **Headers:** `hibp-api-key: <key>`, `User-Agent: Evie-Security-Assistant`
- **Phase 1 Decision:** Direct email lookup over HTTPS is the chosen Phase 1 design; k-anonymity is explicitly deferred.
- **HTTP Handling:** HTTP 404 is explicitly defined as "no breach found" (`[]`). HTTP 401, 403, 429, and 5xx errors are surfaced/raised — never masked as "0 breaches".

### 4. Exposure Auditor (`exposure_watch.py`)
- **Google Drive API (Data Minimization):** Query file metadata and permissions strictly using `drive.files.list(q="trashed = false", fields="nextPageToken, files(id, name, mimeType, permissions(id, type, role, domain, emailAddress))")`. Does NOT download or inspect file contents.
- **Pagination & Error Handling:** Process all result pages via `nextPageToken`. Explicitly raise API exceptions on failure rather than returning truncated results.
- **Exposure Classification & Trusted Domains:** Compare permission domains against configured trusted-domain set (`config.py`). Classify findings explicitly as `"public"` (`type == "anyone"`) or `"external_domain"` (`domain` outside trusted set), preserving permission roles (`reader`, `writer`, etc.).
- **Google Calendar API (ACL Scope):** Audit Calendar ACL resources using `calendarList().list()` and `acl().list()`. Does NOT inspect or retrieve event titles, descriptions, attendees, or event bodies.

### 5. 2FA Auditor (`twofa_audit.py`)
- **GitHub API:** `GET /user` returns `two_factor_authentication` boolean (for authenticated user; requires `read:user` or `user` OAuth scope).
- **Google 2FA Status:** Personal Google 2FA auditing is explicitly deferred/blocked in Phase 1 because no documented, supported non-admin Google API endpoint exists for inspecting personal account 2FA enrollment. No unsupported endpoints or generic OAuth scope approximations are proposed.

## Validation Architecture

To satisfy verification, each module must be validated against unit tests:
- `tests/test_keyring.py`: Tests storing and retrieving credentials via mocked keyring backend interface.
- `tests/test_secret_scanner.py`: Test synthetic strings (e.g., GitHub, OpenAI, Anthropic, AWS keys) -> true positive; test normal code strings -> true negative. Verify zero raw secret leakage in findings.
- `tests/test_breach_watch.py`: Test response parsing, URL-encoding, 404 handling, and API error propagation with mocked HTTP requests and keyring.
- `tests/test_exposure_watch.py`: Test permission auditing logic, trusted-domain matching, and pagination handling/failure with mocked Google API discovery clients.
- `tests/test_twofa_audit.py`: Test GitHub 2FA response parsing with mocked API calls and verify Google 2FA deferred behavior.
