"""
Read-Only Gmail Phishing Pattern Analyzer for Evie Assistant.

Implements SCORE-04 & Section 8 of 02_TRD.md:
Inspects email headers (From, Subject, Return-Path, Authentication-Results) for phishing indicators.
Strictly READ-ONLY (gmail.readonly scope). Performs zero email send, modify, or delete operations.
"""

import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
import redaction

FINANCIAL_URGENCY_PATTERNS = (
    "wire transfer",
    "urgent payment",
    "account suspended",
    "verify password",
    "verify identity",
    "security breach action required",
    "immediate action required",
    "confirm billing",
    "unauthorized login attempt",
)

# Dynamic import of Laya noul primitive if available
NOUL_AVAILABLE = False
_noul_module = None

try:
    import noul
    NOUL_AVAILABLE = True
    _noul_module = noul
except ImportError:
    try:
        from laya import noul
        NOUL_AVAILABLE = True
        _noul_module = noul
    except ImportError:
        _noul_module = None
        NOUL_AVAILABLE = False


def extract_domain(email_str: str) -> str:
    """
    Extract domain from an email address or header string (e.g., 'PayPal <security@paypal.com>').
    """
    match = re.search(r"[\w\.-]+@([\w\.-]+)", email_str)
    if match:
        return match.group(1).lower()
    return ""


def analyze_email_header(headers: dict) -> dict:
    """
    Analyze a single email's header metadata for phishing indicators (SCORE-04).
    """
    # Normalize headers dictionary
    normalized_headers = {}
    if isinstance(headers, dict):
        for k, v in headers.items():
            normalized_headers[k.lower()] = str(v)
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and "name" in h and "value" in h:
                normalized_headers[h["name"].lower()] = str(h["value"])

    sender = normalized_headers.get("from", "")
    return_path = normalized_headers.get("return-path", "")
    subject = normalized_headers.get("subject", "")
    auth_results = normalized_headers.get("authentication-results", "").lower()

    flags = []

    # 1. Financial Urgency Keyword Check
    subject_lower = subject.lower()
    for pattern in FINANCIAL_URGENCY_PATTERNS:
        if pattern in subject_lower:
            flags.append(f"urgency_keyword: '{pattern}'")
            break

    # 2. Sender Domain vs Return-Path Mismatch Check
    sender_domain = extract_domain(sender)
    return_domain = extract_domain(return_path)

    if sender_domain and return_domain and sender_domain != return_domain:
        flags.append(f"domain_mismatch: from '{sender_domain}' vs return-path '{return_domain}'")

    # 3. DKIM / SPF Authentication Failure Check
    if "dkim=fail" in auth_results:
        flags.append("dkim_failure")
    if "spf=fail" in auth_results:
        flags.append("spf_failure")

    # 4. Risk Score Calculation
    flag_count = len(flags)
    if flag_count == 0:
        risk_score = "low"
    elif flag_count == 1:
        risk_score = "moderate"
    elif flag_count == 2:
        risk_score = "high"
    else:
        risk_score = "critical"

    return {
        "subject": subject,
        "sender": sender,
        "return_path": return_path,
        "risk_score": risk_score,
        "flags": flags,
    }


def analyze_phishing_laya(headers: dict, confidence_threshold: float = 0.7) -> Optional[dict]:
    """
    First-pass phishing classification using Laya's noul primitive.
    Returns dict with phishing_confidence, is_phishing, risk_score if noul is available
    and produces a confident verdict. Returns None if noul is unavailable or low-confidence.
    """
    if not NOUL_AVAILABLE or _noul_module is None:
        return None

    try:
        normalized = {}
        if isinstance(headers, dict):
            normalized = {k.lower(): str(v) for k, v in headers.items()}
        elif isinstance(headers, list):
            for h in headers:
                if isinstance(h, dict) and "name" in h and "value" in h:
                    normalized[h["name"].lower()] = str(h["value"])

        subject = normalized.get("subject", "")
        sender = normalized.get("from", "")
        text_to_analyze = f"From: {sender}\nSubject: {subject}"

        result = None
        for method_name in ("predict", "classify_phishing", "classify"):
            if hasattr(_noul_module, method_name):
                fn = getattr(_noul_module, method_name)
                try:
                    res = fn(text_to_analyze) if method_name != "classify_phishing" else fn(text_to_analyze, headers=headers)
                    if res is not None:
                        # Check if res is valid return structure
                        if isinstance(res, (dict, float, int, tuple)):
                            result = res
                            break
                        elif hasattr(res, "get") or hasattr(res, "confidence") or hasattr(res, "return_value"):
                            if isinstance(getattr(res, "return_value", None), (dict, float, int, tuple)):
                                result = res.return_value
                                break
                except Exception:
                    continue

        if result is None and hasattr(_noul_module, "PhishingClassifier"):
            try:
                classifier = _noul_module.PhishingClassifier()
                for method_name in ("predict", "classify"):
                    if hasattr(classifier, method_name):
                        fn = getattr(classifier, method_name)
                        res = fn(text_to_analyze)
                        if res is not None and isinstance(res, (dict, float, int, tuple)):
                            result = res
                            break
            except Exception:
                pass

        if result is None:
            return None

        confidence = 0.0
        is_phishing = False

        if isinstance(result, dict):
            confidence = float(result.get("confidence", result.get("score", result.get("phishing_confidence", 0.0))))
            is_phishing = bool(result.get("is_phishing", result.get("phishing", confidence >= 0.5)))
        elif isinstance(result, (float, int)):
            confidence = float(result)
            is_phishing = confidence >= 0.5
        elif isinstance(result, tuple) and len(result) == 2:
            is_phishing = bool(result[0])
            confidence = float(result[1])

        if confidence < confidence_threshold:
            # Low confidence / ambiguous -> escalate to LLM/heuristic path
            return None

        risk_score = "high" if is_phishing else "low"
        if is_phishing and confidence >= 0.9:
            risk_score = "critical"

        return {
            "phishing_confidence": round(confidence, 3),
            "phishing_source": "laya",
            "is_phishing": is_phishing,
            "risk_score": risk_score,
            "flags": ["laya_noul_flagged"] if is_phishing else [],
        }
    except Exception:
        return None


def analyze_email_phishing(headers: dict, confidence_threshold: float = 0.7) -> dict:
    """
    Comprehensive phishing analysis for an email message.
    1. Attempts Laya noul first-pass filter if available and confident (source: 'laya').
    2. Escalates ambiguous/low-confidence or unavailable noul to rule-based/LLM path (source: 'llm_escalation' or 'heuristic').
    """
    laya_res = analyze_phishing_laya(headers, confidence_threshold=confidence_threshold)
    if laya_res is not None:
        header_info = analyze_email_header(headers)
        return {
            "subject": header_info["subject"],
            "sender": header_info["sender"],
            "return_path": header_info["return_path"],
            "risk_score": laya_res["risk_score"],
            "phishing_confidence": laya_res["phishing_confidence"],
            "phishing_source": "laya",
            "flags": laya_res["flags"] or header_info["flags"],
        }

    header_info = analyze_email_header(headers)
    flag_count = len(header_info["flags"])
    heuristic_confidence = 0.9 if flag_count >= 2 else (0.6 if flag_count == 1 else 0.1)

    phishing_source = "llm_escalation" if NOUL_AVAILABLE else "heuristic"

    return {
        "subject": header_info["subject"],
        "sender": header_info["sender"],
        "return_path": header_info["return_path"],
        "risk_score": header_info["risk_score"],
        "phishing_confidence": round(heuristic_confidence, 3),
        "phishing_source": phishing_source,
        "flags": header_info["flags"],
    }


def fetch_gmail_incremental_messages(
    access_token: str, start_history_id: Optional[str] = None, max_results: int = 10
) -> dict:
    """
    Fetch incremental email message headers using Gmail REST API v1.
    If start_history_id is provided, uses users.history.list to get new messages since start_history_id.
    Falls back gracefully to users.messages.list if no checkpoint exists or if start_history_id is invalid/expired.
    Strictly READ-ONLY (format=metadata). Does NOT retrieve email body or attachments.
    """
    if not access_token:
        raise ValueError("CREDENTIAL_MISSING: Google OAuth access token is required")

    headers = {"Authorization": f"Bearer {access_token}"}
    retrieved_messages = []
    new_history_id = None
    fallback_triggered = False

    # 1. Attempt History API Incremental Fetch if start_history_id provided
    if start_history_id:
        history_url = "https://gmail.googleapis.com/gmail/v1/users/me/history"
        history_params = {"startHistoryId": str(start_history_id), "maxResults": str(max_results)}

        try:
            with httpx.Client(timeout=10.0) as client:
                h_resp = client.get(history_url, headers=headers, params=history_params)

            if h_resp.status_code == 200:
                h_data = h_resp.json()
                new_history_id = str(h_data.get("historyId", start_history_id))
                history_records = h_data.get("history", [])

                target_msg_ids = []
                for rec in history_records:
                    # Collect message IDs added or referenced in history
                    for added in rec.get("messagesAdded", []):
                        msg_info = added.get("message", {})
                        if isinstance(msg_info, dict) and "id" in msg_info:
                            if msg_info["id"] not in target_msg_ids:
                                target_msg_ids.append(msg_info["id"])
                    for msg_item in rec.get("messages", []):
                        if isinstance(msg_item, dict) and "id" in msg_item:
                            if msg_item["id"] not in target_msg_ids:
                                target_msg_ids.append(msg_item["id"])

                if not target_msg_ids:
                    return {
                        "messages": [],
                        "history_id": new_history_id,
                        "fallback_triggered": False,
                    }

                # Fetch metadata only for new history messages
                with httpx.Client(timeout=10.0) as client:
                    for msg_id in target_msg_ids[:max_results]:
                        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
                        metadata_params = [
                            ("format", "metadata"),
                            ("metadataHeaders", "From"),
                            ("metadataHeaders", "Return-Path"),
                            ("metadataHeaders", "Subject"),
                            ("metadataHeaders", "Authentication-Results"),
                            ("metadataHeaders", "List-Unsubscribe"),
                        ]
                        try:
                            m_resp = client.get(url, headers=headers, params=metadata_params)
                        except Exception as e:
                            safe_err = redaction.redact_secret(str(e))
                            raise RuntimeError(f"Gmail API metadata request failed for message {msg_id}: {safe_err}") from e

                        if m_resp.status_code == 200:
                            m_data = m_resp.json()
                            if isinstance(m_data, dict):
                                payload = m_data.get("payload", {})
                                header_list = payload.get("headers", [])
                                header_map = {
                                    h.get("name", "").lower(): h.get("value", "")
                                    for h in header_list if isinstance(h, dict)
                                }
                                retrieved_messages.append({
                                    "id": m_data.get("id"),
                                    "thread_id": m_data.get("threadId"),
                                    "internal_date": m_data.get("internalDate"),
                                    "headers": header_list,
                                    "sender": header_map.get("from", ""),
                                    "subject": header_map.get("subject", ""),
                                    "return_path": header_map.get("return-path", ""),
                                    "authentication_results": header_map.get("authentication-results", ""),
                                    "list_unsubscribe": header_map.get("list-unsubscribe", ""),
                                })

                return {
                    "messages": retrieved_messages,
                    "history_id": new_history_id,
                    "fallback_triggered": False,
                }

            elif h_resp.status_code in (404, 400):
                # History ID expired or invalid -> trigger clean fallback to messages.list
                fallback_triggered = True
            elif h_resp.status_code == 401:
                raise RuntimeError("HTTP 401: Unauthorized - OAuth token invalid or expired")
            elif h_resp.status_code == 403:
                raise RuntimeError("HTTP 403: Forbidden - Gmail API access denied")
            elif h_resp.status_code == 429:
                raise RuntimeError("HTTP 429: Rate limit exceeded")
        except RuntimeError:
            raise
        except Exception:
            fallback_triggered = True

    # 2. Standard Messages List (Fallback path or no start_history_id)
    list_params = {"maxResults": str(max_results)}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get("https://gmail.googleapis.com/gmail/v1/users/me/messages", headers=headers, params=list_params)
    except Exception as e:
        safe_err = redaction.redact_secret(str(e))
        raise RuntimeError(f"Gmail API list request failed: {safe_err}") from e

    if resp.status_code == 401:
        raise RuntimeError("HTTP 401: Unauthorized - OAuth token invalid or expired")
    elif resp.status_code == 403:
        raise RuntimeError("HTTP 403: Forbidden - Gmail API access denied")
    elif resp.status_code == 429:
        raise RuntimeError("HTTP 429: Rate limit exceeded")
    elif resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: Gmail API list request failed")

    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError("Malformed JSON response from Gmail API list endpoint") from e

    if not isinstance(data, dict):
        raise RuntimeError("Malformed response structure from Gmail API list endpoint")

    new_history_id = data.get("historyId")
    message_items = data.get("messages", [])
    if not isinstance(message_items, list) or not message_items:
        return {"messages": [], "history_id": new_history_id, "fallback_triggered": fallback_triggered}

    with httpx.Client(timeout=10.0) as client:
        for item in message_items[:max_results]:
            if not isinstance(item, dict) or "id" not in item:
                continue
            msg_id = item["id"]
            url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
            metadata_params = [
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Return-Path"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Authentication-Results"),
                ("metadataHeaders", "List-Unsubscribe"),
            ]
            try:
                m_resp = client.get(url, headers=headers, params=metadata_params)
            except Exception as e:
                safe_err = redaction.redact_secret(str(e))
                raise RuntimeError(f"Gmail API metadata request failed for message {msg_id}: {safe_err}") from e

            if m_resp.status_code == 401:
                raise RuntimeError("HTTP 401: Unauthorized - OAuth token invalid or expired")
            elif m_resp.status_code == 403:
                raise RuntimeError("HTTP 403: Forbidden - Gmail API access denied")
            elif m_resp.status_code == 429:
                raise RuntimeError("HTTP 429: Rate limit exceeded")
            elif m_resp.status_code != 200:
                raise RuntimeError(f"HTTP {m_resp.status_code}: Gmail API metadata fetch failed for message {msg_id}")

            try:
                m_data = m_resp.json()
            except Exception as e:
                raise RuntimeError(f"Malformed JSON response for Gmail message {msg_id}") from e

            if isinstance(m_data, dict):
                if not new_history_id and m_data.get("historyId"):
                    new_history_id = str(m_data.get("historyId"))

                payload = m_data.get("payload", {})
                header_list = payload.get("headers", [])
                normalized = {
                    "id": m_data.get("id"),
                    "thread_id": m_data.get("threadId"),
                    "internal_date": m_data.get("internalDate"),
                    "headers": header_list,
                }

                header_map = {}
                for header in header_list:
                    if isinstance(header, dict):
                        name = header.get("name")
                        value = header.get("value", "")
                        if name:
                            header_map[name.lower()] = value

                normalized["sender"] = header_map.get("from", "")
                normalized["subject"] = header_map.get("subject", "")
                normalized["return_path"] = header_map.get("return-path", "")
                normalized["authentication_results"] = header_map.get(
                    "authentication-results", ""
                )
                normalized["list_unsubscribe"] = header_map.get(
                    "list-unsubscribe", ""
                )
                retrieved_messages.append(normalized)

    return {"messages": retrieved_messages, "history_id": new_history_id, "fallback_triggered": fallback_triggered}


def fetch_gmail_messages(
    access_token: str, max_results: int = 10, start_history_id: Optional[str] = None
) -> list[dict]:
    """
    Fetch recent email message headers from Gmail REST API v1.
    Delegates to fetch_gmail_incremental_messages and returns the messages list.
    Strictly READ-ONLY (format=metadata). Does NOT retrieve email body or attachments.
    """
    result = fetch_gmail_incremental_messages(access_token=access_token, start_history_id=start_history_id, max_results=max_results)
    return result.get("messages", [])


def categorize_email_message(headers: dict) -> str:
    """
    Categorizes email header metadata independently of phishing risk analysis into:
    - 'newsletter_promotional' (if List-Unsubscribe present or marketing keywords in subject)
    - 'important' (if security/urgent/account/invoice keywords present)
    - 'transactional' (routine transactional emails)

    Promotional/newsletter classification is evaluated strictly on metadata and subject signals
    and is independent from phishing risk analysis.
    """
    normalized = {}
    if isinstance(headers, dict):
        for k, v in headers.items():
            normalized[k.lower()] = str(v)
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and "name" in h and "value" in h:
                normalized[h["name"].lower()] = str(h["value"])

    list_unsub = normalized.get("list-unsubscribe", "")
    subject = normalized.get("subject", "").lower()

    if list_unsub or any(kw in subject for kw in ["newsletter", "digest", "sale", "discount", "offer", "unsubscribe", "weekly", "marketing"]):
        return "newsletter_promotional"

    if any(kw in subject for kw in ["invoice", "receipt", "important", "security", "action required", "alert", "notice", "confidential"]):
        return "important"

    return "transactional"


def scan_phishing_patterns(messages: list[dict]) -> list[dict]:
    """
    Scan a batch of email message headers for phishing patterns.
    Returns only messages flagged with risk_score >= 'moderate'.
    Uses analyze_email_phishing for complete phishing analysis.
    """
    flagged = []
    for msg in messages:
        msg_id = msg.get("id", "unknown")
        headers = msg.get("headers")
        if headers is None and isinstance(msg.get("payload"), dict):
            headers = msg.get("payload", {}).get("headers", {})
        if headers is None:
            headers = {}
        analysis = analyze_email_phishing(headers)

        if analysis["risk_score"] in ("moderate", "high", "critical"):
            flagged.append(
                {
                    "message_id": msg_id,
                    "subject": analysis["subject"],
                    "sender": analysis["sender"],
                    "risk_score": analysis["risk_score"],
                    "phishing_confidence": analysis.get("phishing_confidence"),
                    "phishing_source": analysis.get("phishing_source"),
                    "flags": analysis["flags"],
                }
            )

    return flagged


