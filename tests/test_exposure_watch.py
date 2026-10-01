"""
Unit tests for DriveExposureAuditor and CalendarExposureAuditor in exposure_watch module.
"""

from unittest.mock import MagicMock
import pytest

from integrations.exposure_watch import (
    DriveExposureAuditor,
    CalendarExposureAuditor,
    DRIVE_METADATA_READONLY_SCOPE,
    CALENDAR_READONLY_SCOPE,
)


def test_drive_public_anyone_permission():
    mock_service = MagicMock()
    mock_files_request = MagicMock()
    mock_files_request.execute.return_value = {
        "files": [
            {
                "id": "file_123",
                "name": "Public Design Doc",
                "mimeType": "application/vnd.google-apps.document",
                "permissions": [
                    {"id": "perm_anyone", "type": "anyone", "role": "reader"}
                ],
            }
        ]
    }
    mock_service.files().list.return_value = mock_files_request

    auditor = DriveExposureAuditor()
    findings = auditor.audit_drive_permissions(mock_service)

    assert len(findings) == 1
    assert findings[0]["resource_id"] == "file_123"
    assert findings[0]["exposure_type"] == "public"
    assert findings[0]["role"] == "reader"


def test_drive_external_domain_permission():
    mock_service = MagicMock()
    mock_files_request = MagicMock()
    mock_files_request.execute.return_value = {
        "files": [
            {
                "id": "file_456",
                "name": "Shared Strategy",
                "mimeType": "application/pdf",
                "permissions": [
                    {"id": "perm_ext", "type": "domain", "domain": "externalpartner.com", "role": "writer"}
                ],
            }
        ]
    }
    mock_service.files().list.return_value = mock_files_request

    auditor = DriveExposureAuditor()
    trusted = {"mycompany.com"}
    findings = auditor.audit_drive_permissions(mock_service, trusted_domains=trusted)

    assert len(findings) == 1
    assert findings[0]["exposure_type"] == "external_domain"
    assert findings[0]["domain"] == "externalpartner.com"
    assert findings[0]["role"] == "writer"


def test_drive_trusted_domain_ignored():
    mock_service = MagicMock()
    mock_files_request = MagicMock()
    mock_files_request.execute.return_value = {
        "files": [
            {
                "id": "file_789",
                "name": "Internal Memo",
                "mimeType": "application/pdf",
                "permissions": [
                    {"id": "perm_internal", "type": "domain", "domain": "mycompany.com", "role": "reader"}
                ],
            }
        ]
    }
    mock_service.files().list.return_value = mock_files_request

    auditor = DriveExposureAuditor()
    trusted = {"mycompany.com"}
    findings = auditor.audit_drive_permissions(mock_service, trusted_domains=trusted)

    assert len(findings) == 0


def test_drive_pagination_multi_page():
    mock_service = MagicMock()

    page1_response = {
        "nextPageToken": "token_page_2",
        "files": [
            {
                "id": "file_1",
                "name": "Doc 1",
                "permissions": [{"id": "p1", "type": "anyone", "role": "reader"}],
            }
        ],
    }

    page2_response = {
        "files": [
            {
                "id": "file_2",
                "name": "Doc 2",
                "permissions": [{"id": "p2", "type": "anyone", "role": "writer"}],
            }
        ]
    }

    req1 = MagicMock()
    req1.execute.return_value = page1_response
    req2 = MagicMock()
    req2.execute.return_value = page2_response

    mock_service.files().list.side_effect = [req1, req2]

    auditor = DriveExposureAuditor()
    findings = auditor.audit_drive_permissions(mock_service)

    assert len(findings) == 2
    assert findings[0]["resource_id"] == "file_1"
    assert findings[1]["resource_id"] == "file_2"


def test_drive_api_failure_raises_exception():
    mock_service = MagicMock()
    mock_service.files().list.side_effect = Exception("Google Drive API rate limit")

    auditor = DriveExposureAuditor()
    with pytest.raises(RuntimeError, match="Google Drive API list request failed"):
        auditor.audit_drive_permissions(mock_service)


def test_calendar_acl_public_and_external_permissions():
    mock_service = MagicMock()

    mock_cal_list_req = MagicMock()
    mock_cal_list_req.execute.return_value = {
        "items": [
            {"id": "primary_cal_id", "summary": "Personal Calendar"}
        ]
    }
    mock_service.calendarList().list.return_value = mock_cal_list_req

    mock_acl_req = MagicMock()
    mock_acl_req.execute.return_value = {
        "items": [
            {"id": "rule_1", "scope": {"type": "default"}, "role": "freeBusyReader"},
            {"id": "rule_2", "scope": {"type": "domain", "value": "external.org"}, "role": "reader"},
        ]
    }
    mock_service.acl().list.return_value = mock_acl_req

    auditor = CalendarExposureAuditor()
    trusted = {"mycompany.com"}
    findings = auditor.audit_calendar_acls(mock_service, trusted_domains=trusted)

    assert len(findings) == 2
    assert findings[0]["resource_type"] == "calendar_acl"
    assert findings[0]["exposure_type"] == "public"
    assert findings[0]["role"] == "freeBusyReader"

    assert findings[1]["exposure_type"] == "external_domain"
    assert findings[1]["domain"] == "external.org"

    # Verify event methods were NEVER called (no event content fetching)
    mock_service.events.assert_not_called()
