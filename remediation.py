"""
Remediation Guidance Generator for Evie Assistant.

Implements SCORE-02 & Section 8 of 06_Acceptance_Criteria.md:
Generates actionable, step-by-step remediation instructions for flagged security findings:
secrets, breach checks, public exposures, 2FA gaps, and dependency vulnerabilities.

Instructional ONLY — performs ZERO automatic destructive modifications.
"""

import sqlite3
import redaction


def get_remediation_guide(finding_type: str, finding_id: int, db_path: str = "evie.db") -> dict:
    """
    Retrieve step-by-step remediation guidance for a specific finding (SCORE-02).
    """
    finding_type = str(finding_type).lower().strip()
    if finding_type not in ("secret", "breach", "exposure", "twofa", "dependency"):
        raise ValueError(f"Invalid finding_type '{finding_type}'. Must be 'secret', 'breach', 'exposure', 'twofa', or 'dependency'.")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        if finding_type == "secret":
            cur = conn.execute("SELECT * FROM secret_findings WHERE id = ?", (finding_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Secret finding with ID {finding_id} not found.")

            repo = row["repo_name"]
            file_path = row["file_path"]
            line_num = row["line_number"] or "unknown"
            preview = row["redacted_preview"] or "****"
            pattern = row["pattern_type"] or "sensitive credential"

            title = f"Remediate Leaked Credential in {repo}"
            severity = "critical"
            steps = [
                f"1. Immediately log in to the provider dashboard for '{pattern}' and revoke/delete the compromised credential ({preview}).",
                "2. Generate a new credential/token and store it strictly in the OS Keyring via `keyring_utils.set_credential`.",
                f"3. Remove the raw secret value from file '{file_path}' (line {line_num}) in repository '{repo}'.",
                "4. Rewrite or prune git commit history if the secret was pushed to a public repository.",
                f"5. Mark finding #{finding_id} as resolved via `POST /findings/{finding_id}/resolve`.",
            ]
            details = {"repo": repo, "file_path": file_path, "line_number": line_num, "preview": preview}

        elif finding_type == "breach":
            cur = conn.execute("SELECT * FROM breach_checks WHERE id = ?", (finding_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Breach finding with ID {finding_id} not found.")

            email = row["email_checked"]
            breach_name = row["breach_name"] or "Data Breach"

            title = f"Remediate Compromised Account ({breach_name})"
            severity = "high"
            steps = [
                f"1. Immediately change password for account associated with '{email}' on {breach_name} and any services sharing that password.",
                "2. Enable 2-Factor Authentication (2FA) using an authenticator app or hardware security key.",
                "3. Check for unauthorized logins, password resets, or API token creations in account settings.",
                "4. Update credentials stored in OS Keyring if necessary.",
                f"5. Mark breach check #{finding_id} as acknowledged in Evie.",
            ]
            details = {"email": email, "breach_name": breach_name}

        elif finding_type == "exposure":
            cur = conn.execute("SELECT * FROM exposure_findings WHERE id = ?", (finding_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Exposure finding with ID {finding_id} not found.")

            source = row["source"]
            item_name = row["item_name"] or "Resource"
            exposure_type = row["exposure_type"] or "public"

            title = f"Remediate Public Exposure on {source.capitalize()}: {item_name}"
            severity = "high"
            steps = [
                f"1. Open {source.capitalize()} settings for item '{item_name}' (ID: {row['item_id']}).",
                f"2. Change sharing permissions from '{exposure_type}' to 'Restricted' or specific user access.",
                "3. Remove public web link access unless explicitly intended for open publication.",
                f"4. Re-run {source.capitalize()} exposure audit to verify finding resolution.",
            ]
            details = {"source": source, "item_name": item_name, "exposure_type": exposure_type}

        elif finding_type == "twofa":
            cur = conn.execute("SELECT * FROM twofa_audit WHERE id = ?", (finding_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"2FA audit finding with ID {finding_id} not found.")

            provider = row["provider"]

            title = f"Enable 2FA on {provider.capitalize()} Account"
            severity = "moderate"
            steps = [
                f"1. Log in to your {provider.capitalize()} account security settings page.",
                "2. Select 'Two-Factor Authentication' or '2-Step Verification'.",
                "3. Pair an Authenticator App (e.g. Google Authenticator, YubiKey, TOTP).",
                "4. Download and securely store backup recovery codes.",
                "5. Re-run 2FA audit to verify updated status.",
            ]
            details = {"provider": provider}

        elif finding_type == "dependency":
            cur = conn.execute("SELECT * FROM dependency_alerts WHERE id = ?", (finding_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Dependency alert finding with ID {finding_id} not found.")

            repo = row["repo_name"]
            pkg = row["package_name"] or "dependency"
            sev = row["severity"] or "high"
            url = row["advisory_url"] or ""

            title = f"Remediate {sev.capitalize()} Vulnerability in {pkg} ({repo})"
            severity = sev.lower()
            steps = [
                f"1. Review security advisory details at: {url or 'GitHub Dependabot'}.",
                f"2. Update package '{pkg}' in repository '{repo}' to the patched version.",
                f"3. Run dependency package manager update (e.g. `pip install --upgrade {pkg}` or `npm update {pkg}`).",
                "4. Run test suite to verify no breaking changes were introduced.",
                "5. Push updated dependency manifest (`requirements.txt` or `package.json`).",
            ]
            details = {"repo": repo, "package": pkg, "severity": sev, "advisory_url": url}

    return {
        "finding_type": finding_type,
        "finding_id": finding_id,
        "title": title,
        "severity": severity,
        "steps": steps,
        "details": details,
    }
