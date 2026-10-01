"""
Google Drive & Calendar Exposure Auditor Module for Evie Assistant.

Implements SEC-03:
Audits Google Drive permissions and Google Calendar ACLs for public or external domain exposure.
Enforces strict data minimization, pagination completeness, and least-privilege read-only scopes.
"""

from typing import Any, Dict, List, Optional, Set

try:
    from integrations.breach_watch import redact_email
except ImportError:
    from breach_watch import redact_email

DRIVE_METADATA_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


class DriveExposureAuditor:
    """
    Auditor for Google Drive file permissions.
    """

    def audit_drive_permissions(
        self, service: Any, trusted_domains: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Audit Google Drive files for public ('anyone') or external-domain permissions.
        Requires drive.metadata.readonly scope.
        Processes all pages via nextPageToken and raises exceptions on API failure.
        Does NOT download or inspect document contents.
        """
        if trusted_domains is None:
            trusted_domains = set()
        # Ensure all trusted domains are lowercased
        trusted_domains = {d.lower() for d in trusted_domains}

        findings: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        fields_str = "nextPageToken, files(id, name, mimeType, permissions(id, type, role, domain, emailAddress))"

        while True:
            try:
                request = service.files().list(
                    q="trashed = false",
                    pageSize=100,
                    pageToken=page_token,
                    fields=fields_str,
                )
                response = request.execute()
            except Exception as e:
                raise RuntimeError(f"Google Drive API list request failed: {e}") from e

            files = response.get("files", [])
            for file_item in files:
                file_id = file_item.get("id")
                file_name = file_item.get("name")
                permissions = file_item.get("permissions", [])

                for perm in permissions:
                    perm_type = perm.get("type")
                    perm_role = perm.get("role")
                    perm_domain = perm.get("domain", "").lower() if perm.get("domain") else ""
                    email_addr = perm.get("emailAddress")

                    # Check 1: Public "anyone" permission
                    if perm_type == "anyone":
                        findings.append({
                            "resource_type": "drive_file",
                            "resource_id": file_id,
                            "resource_name": file_name,
                            "exposure_type": "public",
                            "role": perm_role,
                            "permission_id": perm.get("id"),
                        })
                    # Check 2: External domain or external user permission
                    elif perm_type == "domain" and perm_domain and perm_domain not in trusted_domains:
                        findings.append({
                            "resource_type": "drive_file",
                            "resource_id": file_id,
                            "resource_name": file_name,
                            "exposure_type": "external_domain",
                            "domain": perm_domain,
                            "role": perm_role,
                            "permission_id": perm.get("id"),
                        })
                    elif perm_type == "user" and email_addr and "@" in email_addr:
                        user_domain = email_addr.split("@", 1)[1].lower()
                        if user_domain not in trusted_domains and trusted_domains:
                            findings.append({
                                "resource_type": "drive_file",
                                "resource_id": file_id,
                                "resource_name": file_name,
                                "exposure_type": "external_domain",
                                "domain": user_domain,
                                "redacted_email": redact_email(email_addr),
                                "role": perm_role,
                                "permission_id": perm.get("id"),
                            })

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return findings


class CalendarExposureAuditor:
    """
    Auditor for Google Calendar ACL resource permissions.
    Audits calendar ACLs without inspecting individual event contents.
    """

    def audit_calendar_acls(
        self, service: Any, trusted_domains: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Audit Google Calendar ACLs for public or external sharing.
        Requires calendar.readonly or calendar.acls.readonly scope.
        Does NOT retrieve event titles, descriptions, or event bodies.
        """
        if trusted_domains is None:
            trusted_domains = set()
        trusted_domains = {d.lower() for d in trusted_domains}

        findings: List[Dict[str, Any]] = []

        try:
            calendars_result = service.calendarList().list().execute()
        except Exception as e:
            raise RuntimeError(f"Google Calendar API list request failed: {e}") from e

        calendars = calendars_result.get("items", [])
        for cal in calendars:
            cal_id = cal.get("id")
            cal_summary = cal.get("summary")

            try:
                acl_result = service.acl().list(calendarId=cal_id).execute()
            except Exception as e:
                # If reading ACL fails for a specific calendar, log error or raise
                raise RuntimeError(f"Failed to fetch ACL for calendar '{cal_id}': {e}") from e

            acl_rules = acl_result.get("items", [])
            for rule in acl_rules:
                scope = rule.get("scope", {})
                scope_type = scope.get("type")
                scope_value = scope.get("value", "")
                role = rule.get("role")

                if scope_type in ("default", "anyone"):
                    findings.append({
                        "resource_type": "calendar_acl",
                        "resource_id": cal_id,
                        "resource_name": cal_summary,
                        "exposure_type": "public",
                        "role": role,
                        "rule_id": rule.get("id"),
                    })
                elif scope_type == "domain" and scope_value and scope_value.lower() not in trusted_domains:
                    findings.append({
                        "resource_type": "calendar_acl",
                        "resource_id": cal_id,
                        "resource_name": cal_summary,
                        "exposure_type": "external_domain",
                        "domain": scope_value.lower(),
                        "role": role,
                        "rule_id": rule.get("id"),
                    })
                elif scope_type == "user" and "@" in scope_value:
                    user_domain = scope_value.split("@", 1)[1].lower()
                    if user_domain not in trusted_domains and trusted_domains:
                        findings.append({
                            "resource_type": "calendar_acl",
                            "resource_id": cal_id,
                            "resource_name": cal_summary,
                            "exposure_type": "external_domain",
                            "domain": user_domain,
                            "redacted_email": redact_email(scope_value),
                            "role": role,
                            "rule_id": rule.get("id"),
                        })

        return findings
