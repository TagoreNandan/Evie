"""
Conversational Intelligence & Grounded Query Router for Evie Assistant.

Parses user prompt intents and entities (repo_name, time_range) and dispatches to
grounded SQLite queries over github_repo_cache, github_activity_log, secret_findings,
and gmail_metadata.
"""

import os
import re
import datetime
import sqlite3
from typing import Dict, Any, Optional, List

from services.github_intelligence import (
    check_github_sync_status,
    get_repository_analytics,
    get_recent_activity,
    get_yesterday_activity,
    get_unreviewed_prs,
    get_secret_findings,
    get_secret_findings_by_repository,
)
from services.gmail_intelligence import (
    check_gmail_sync_status,
    get_inbox_intelligence_summary,
    get_top_promotional_senders,
    get_important_emails,
    get_recent_messages,
    get_suspicious_emails,
    get_promotional_emails,
)


def extract_repository_entity(message: str, db_path: Optional[str] = None) -> Optional[str]:
    """
    Extracts repository name entity from user query, avoiding temporal words ('recently', 'today', etc.).
    Cross-references against cached repositories in github_repo_cache if available.
    """
    stop_words = {
        "me", "us", "you", "them", "him", "her", "it", "all", "now", "today", "yesterday",
        "recently", "lately", "review", "prs", "pr", "commits", "commit", "that", "this",
        "my", "our", "a", "an", "the", "main", "master", "repo", "repository", "inbox",
        "email", "gmail", "something", "anything", "nothing", "everything", "this_week",
        "month", "year", "away", "important", "new", "changed", "happen", "happened", "work", "worked",
        "in", "for", "on", "at", "to", "from", "with", "by", "of", "is", "are", "was", "were", "did", "does", "do", "done", "get", "got"
    }

    # 1. Match owner/repo pattern explicitly first, e.g. "TagoreNandan/TagoreNandan" or "user/repo"
    owner_repo_match = re.search(r'([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)', message)
    if owner_repo_match:
        candidate = owner_repo_match.group(1).strip()
        if candidate.lower() not in stop_words:
            return candidate

    msg_lower = message.strip().lower()

    # 2. Match "[name] repository" or "[name] repo" (e.g. "TagoreNandan repository")
    prefix_match = re.search(r'\b([a-zA-Z0-9_\-\.]+)\s+(?:repo|repository)\b', message, flags=re.IGNORECASE)
    if prefix_match:
        cand = prefix_match.group(1).strip()
        if cand.lower() not in stop_words:
            return cand

    # 3. Match "repo [name]" or "repository [name]" (e.g. "repository TagoreNandan")
    suffix_match = re.search(r'\b(?:repo|repository)\s+([a-zA-Z0-9_\-\.]+)\b', message, flags=re.IGNORECASE)
    if suffix_match:
        cand = suffix_match.group(1).strip()
        if cand.lower() not in stop_words:
            return cand

    # 4. Check if query mentions a known cached repository name or full_name
    if db_path and os.path.exists(db_path):
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.execute("SELECT full_name, name FROM github_repo_cache")
                for fn, n in cur.fetchall():
                    if fn and fn.lower() in msg_lower:
                        return fn
                    if n and n.lower() not in stop_words and len(n) > 2:
                        pattern = r'\b' + re.escape(n.lower()) + r'\b'
                        if re.search(pattern, msg_lower):
                            return fn
        except Exception:
            pass

    return None


def parse_user_intent(message: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Parses a user message into semantic intent and extracted entities (repo_name, time_range).
    Uses semantic domain concepts to handle broad natural language query variations.
    """
    msg_lower = message.strip().lower()

    # 1. Greetings & Capabilities
    wake_greetings = ("good morning", "good day", "wake", "hey evie", "hello evie", "hi evie")
    if any(msg_lower == wg or msg_lower.startswith(wg + " ") or msg_lower.endswith(" " + wg) for wg in wake_greetings):
        return {"intent": "wake_greeting", "entities": {}}

    general_greetings = ("yo", "hello", "hi", "hey", "greetings", "good evening", "what can you do", "who are you", "help", "how are you", "what's up")
    if any(msg_lower == gg or msg_lower.startswith(gg + " ") or msg_lower.endswith(" " + gg) for gg in general_greetings):
        return {"intent": "greeting", "entities": {}}

    # 1b. Contextual Follow-up Intent
    followup_phrases = (
        "tell them", "show me those", "which ones", "give me more details",
        "tell me more about those", "tell me more", "show details", "details on those",
        "show me more", "show more", "next page", "more details"
    )
    if any(fp in msg_lower for fp in followup_phrases):
        return {"intent": "contextual_followup", "entities": {"raw_message": message}}

    # 2. Extract repository entity if present (avoiding temporal words like "recently")
    repo_entity = extract_repository_entity(message, db_path=db_path)

    # 3. Extract time range entity if present
    time_range = "recent"
    if "today" in msg_lower:
        time_range = "today"
    elif "yesterday" in msg_lower:
        time_range = "yesterday"
    elif "this week" in msg_lower or "week" in msg_lower:
        time_range = "this_week"
    elif "last month" in msg_lower:
        time_range = "last_month"
    elif "this year" in msg_lower or "year" in msg_lower:
        time_range = "this_year"

    # Calendar Proposal Intent
    if "propose" in msg_lower and "event" in msg_lower:
        return {"intent": "calendar_propose", "entities": {}}

    # Security Health Score / Posture Intent
    if any(phrase in msg_lower for phrase in (
        "security score", "security health score", "health score", "my score", "posture score",
        "security health", "security situation", "how secure am i", "what should i worry about", "security status"
    )):
        return {"intent": "security_score", "entities": {}}

    # Explicit DRS Research Intent
    if msg_lower.startswith("drs:") or any(term in msg_lower for term in ("should i learn", "recommendation for", "compare", "evaluate", "versus", "vs")):
        return {"intent": "drs_ask", "entities": {"raw_message": message}}

    # Combined GitHub + Gmail Recent Activity / Personal Update Briefing
    if any(phrase in msg_lower for phrase in (
        "across my github and gmail", "across github and gmail", "both github and gmail", "github and gmail", "gmail and github",
        "recent changes across", "latest briefing", "give me my briefing", "full report", "summary of everything",
        "changed recently across", "recent changes", "daily report", "what changed today", "rundown of what changed",
        "quick rundown", "quick update", "what happened while i was away", "anything important since yesterday"
    )):
        return {"intent": "combined_briefing", "entities": {"time_range": time_range}}

    # Secret findings / leaks (Per repo vs general)
    if any(phrase in msg_lower for phrase in ("most secret candidates", "repositories have the most secret", "repos with most secrets", "secret candidates by repository", "secret candidates per repo")):
        return {"intent": "github_secrets_by_repo", "entities": {}}

    if any(phrase in msg_lower for phrase in ("secret", "secrets", "exposed", "api key", "leak", "candidate secrets")):
        return {"intent": "github_secrets", "entities": {"repo_name": repo_entity}}

    # PRs waiting for review
    if (re.search(r'\bprs?\b', msg_lower) or any(phrase in msg_lower for phrase in ("pull request", "unreviewed", "waiting for review", "code review"))) and not ("propose" in msg_lower and "event" in msg_lower):
        return {"intent": "github_prs", "entities": {"repo_name": repo_entity}}

    # Gmail Important Emails
    if any(phrase in msg_lower for phrase in (
        "send me anything important", "anything important in my email", "anything important", "important email",
        "important emails", "important message", "important messages", "anything important from email"
    )) and not ("since yesterday" in msg_lower):
        return {"intent": "gmail_important", "entities": {}}

    # Gmail Top Promotional Sender
    if any(phrase in msg_lower for phrase in (
        "top promotional sender", "most promotional emails", "most newsletters",
        "which sender is sending me the most promotional", "sending me the most promotional", "top sender"
    )):
        return {"intent": "gmail_top_promotional_sender", "entities": {}}

    # Gmail Phishing / Suspicious emails
    if any(phrase in msg_lower for phrase in ("suspicious", "phishing", "fake", "alert emails", "suspicious emails", "dangerous emails", "phish", "anything suspicious")):
        return {"intent": "gmail_phishing", "entities": {}}

    # Gmail Promotional / Newsletters
    if any(phrase in msg_lower for phrase in ("promotional", "newsletter", "newsletters", "promo email", "promotional emails", "promo")):
        return {"intent": "gmail_promotional", "entities": {}}

    # Gmail Inbox / Email domain query
    gmail_keywords = ("gmail", "inbox", "email", "emails", "mail", "messages", "received while", "away", "my email", "what's new in my inbox", "anything i should know from email")
    if any(kw in msg_lower for kw in gmail_keywords):
        return {"intent": "gmail_summary", "entities": {}}

    # GitHub Repositories Listing
    if any(phrase in msg_lower for phrase in ("repositories", "repository list", "repo list", "what repositories", "monitored repos", "list repos")) and not repo_entity:
        return {"intent": "github_repos", "entities": {}}

    # GitHub Commit & Activity Queries (covers "what did I change", "anything new in my main repo", "what did I work on", "what happened in that repo", "have I been working on")
    github_activity_keywords = (
        "github", "commit", "commits", "working on", "work", "worked", "activity", "changed", "change", "changes",
        "repo", "repository", "pushed", "push", "code", "lately"
    )
    if any(kw in msg_lower for kw in github_activity_keywords) or repo_entity is not None:
        return {"intent": "github_commits", "entities": {"repo_name": repo_entity, "time_range": time_range}}

    # General conversational question / fallback
    return {"intent": "general_conversational", "entities": {"repo_name": repo_entity, "time_range": time_range}}


def execute_grounded_query(
    intent_data: Dict[str, Any],
    db_path: str,
    active_tone: str = "neutral",
    last_chat_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Executes grounded SQLite retrieval based on parsed intent and entities.
    Returns response payload dictionary matching FastAPI expected schema.
    Updates last_chat_context if provided.
    """
    intent = intent_data.get("intent", "unknown")
    entities = intent_data.get("entities", {})
    repo_name = entities.get("repo_name")
    time_range = entities.get("time_range", "recent")

    # Helper to set last_chat_context safely
    def update_context(topic: str, data: Any, extra: Optional[Dict[str, Any]] = None):
        if last_chat_context is not None:
            last_chat_context["topic"] = topic
            last_chat_context["data"] = data
            if extra:
                last_chat_context.update(extra)

    # 1. Greeting & Morning Briefing Fast-Path
    if intent in ("greeting", "wake_greeting"):
        if intent == "wake_greeting":
            from services.briefing import generate_briefing
            b_report = generate_briefing(db_path, trigger="wake")
            sec = b_report.get("sections", {}).get("security_findings", {})
            cal = b_report.get("sections", {}).get("calendar_today", {})
            gh = b_report.get("sections", {}).get("github_activity", {})
            reply = f"Good morning! Here is your Evie Daily Briefing ({b_report['timestamp'][:10]}):\n"
            reply += f"• Security Score: {sec['score']}/100 (Grade: {sec['grade']})\n"
            reply += f"• Security Findings: {sec.get('open_secrets', 0)} secrets, {sec.get('public_exposures', 0)} exposures, {sec.get('unacknowledged_breaches', 0)} breaches.\n"
            reply += f"• Calendar Today: {cal.get('event_count', 0)} event(s).\n"
            reply += f"• Overnight GitHub Activity: {gh.get('count', 0)} event(s)."
            return {"reply": reply, "briefing": b_report, "tone": active_tone}

        reply = (
            "Hello! I am Evie, your personal assistant and 24/7 security monitoring engine. Here is what I can do for you:\n"
            "• **GitHub Intelligence**: Track repositories, commits, unreviewed PRs, and secret leaks\n"
            "• **Gmail Intelligence**: Audit inbox messages, flag phishing alerts, analyze promotional senders, and recommend cleanups\n"
            "• **Security Health Score**: Compute real-time posture and vulnerability breakdown\n"
            "• **Calendar Actions**: Propose and confirm schedule updates safely\n\n"
            "How can I help you today?"
        )
        return {"reply": reply, "tone": active_tone}

    # 1b. Calendar Action Proposal
    if intent == "calendar_propose":
        from integrations.calendar_actions import propose_calendar_action
        from datetime import datetime, timedelta
        now = datetime.now()
        start = (now + timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1, hours=1)).isoformat()
        prop = propose_calendar_action(
            db_path=db_path,
            action_type="create",
            summary="Proposed Event",
            start_time=start,
            end_time=end,
            description="Created via Evie chat",
        )
        return {"reply": prop["prompt"], "proposal": prop, "tone": active_tone}

    # 1c. Explicit DRS Research Query
    if intent == "drs_ask":
        import drs
        q = entities.get("raw_message", "")
        res = drs.ask_drs_question(q, db_path=db_path)
        reply = res.get("summary", "")
        if res.get("disclaimer"):
            reply += f"\n\n*{res['disclaimer']}*"
        return {"reply": reply, "drs": res, "sources": res.get("sources", []), "tone": active_tone}

    # 1c. Explicit Security Score / Posture Query
    if intent == "security_score":
        import security_score
        score_data = security_score.get_current_security_score(db_path)
        score_val = score_data["score"]
        grade = score_data["grade"]
        counts = score_data.get("counts", {})
        active_findings = score_data.get("active_findings_count", 0)
        reply = (
            f"Your Security Health Score is {score_val}/100 (Grade: {grade}).\n"
            f"Active Findings: {active_findings} total.\n"
            f"• Secrets: {counts.get('open_secrets_count', 0)}\n"
            f"• Breaches: {counts.get('unacknowledged_breaches_count', 0)}\n"
            f"• Exposures: {counts.get('open_exposures_count', 0)}\n"
            f"• Missing 2FA: {counts.get('missing_twofa_count', 0)}"
        )
        return {"reply": reply, "score": score_data, "tone": active_tone}

    # 1d. Contextual Follow-up Execution
    if intent == "contextual_followup":
        if last_chat_context and last_chat_context.get("topic"):
            topic = last_chat_context["topic"]
            data = last_chat_context.get("data")
            msg_raw = entities.get("raw_message", "").lower()

            if topic == "suspicious_emails":
                if isinstance(data, list) and data:
                    items = [f"• [{str(m.get('risk_score')).upper()}] From: {m.get('sender')} | Subject: '{m.get('subject')}' | Received: {str(m.get('received_at'))[:10]}" for m in data]
                    reply = f"Details on suspicious email alerts ({len(data)} total):\n" + "\n".join(items)
                else:
                    reply = "No suspicious emails recorded to detail."
                return {"reply": reply, "suspicious_emails": data, "tone": active_tone}

            if topic in ("promotional_emails", "gmail_summary"):
                if "tell" in msg_raw or "unsubscribe" in msg_raw:
                    from services.gmail_intelligence import propose_gmail_cleanup_action
                    proposals = []
                    domains = set()
                    if isinstance(data, list):
                        for item in data:
                            d = item.get("sender_domain")
                            if d: domains.add(d)
                    elif isinstance(data, dict):
                        for s in data.get("top_promotional_senders", []):
                            if s.get("domain"): domains.add(s["domain"])

                    if domains:
                        for dom in sorted(domains):
                            prop = propose_gmail_cleanup_action(db_path, dom, action_type="unsubscribe_recommendation")
                            proposals.append(prop)
                        prop_list = [f"• Proposal '{p['proposal_token'][:8]}...': Unsubscribe from {p['sender_domain']}" for p in proposals]
                        reply = f"Created {len(proposals)} unsubscribe proposal(s):\n" + "\n".join(prop_list) + "\n\nPlease confirm execution via POST /gmail/confirm_cleanup."
                    else:
                        reply = "No promotional sender domains available to generate unsubscribe proposals."
                    return {"reply": reply, "proposals": proposals, "tone": active_tone}
                else:
                    if isinstance(data, list) and data:
                        items = [f"• From: {m.get('sender')} | Subject: '{m.get('subject')}' | Unsubscribe: {'Available' if m.get('has_unsubscribe') else 'N/A'}" for m in data[:10]]
                        reply = f"Promotional email details ({len(data)} total):\n" + "\n".join(items)
                    elif isinstance(data, dict) and data.get("top_promotional_senders"):
                        items = [f"• {s['domain']} ({s['count']} email(s))" for s in data["top_promotional_senders"]]
                        reply = f"Top promotional senders:\n" + "\n".join(items)
                    else:
                        reply = "No promotional emails recorded to detail."
                    return {"reply": reply, "promotional_emails": data, "tone": active_tone}

            if topic == "secret_findings":
                if isinstance(data, list) and data:
                    page = last_chat_context.get("page", 1) + 1
                    page_size = last_chat_context.get("page_size", 5)
                    start_idx = (page - 1) * page_size
                    end_idx = start_idx + page_size
                    page_items = data[start_idx:end_idx]

                    if page_items:
                        items = [f"• Repo: {f.get('repo_name')} | Commit: {str(f.get('commit_sha'))[:7]} | Pattern: {f.get('secret_type') or f.get('pattern_type')} | File: {f.get('filename') or f.get('file_path')} | Preview: {f.get('redacted_value') or f.get('redacted_preview')}" for f in page_items]
                        reply = f"Secret Candidates (showing {start_idx + 1}-{min(end_idx, len(data))} of {len(data)} total):\n" + "\n".join(items)
                        if end_idx < len(data):
                            reply += f"\n\nThere are {len(data) - end_idx} more candidate findings. Say 'show me more' to view the next page."
                        last_chat_context["page"] = page
                    else:
                        reply = f"All {len(data)} secret candidates have been shown."
                else:
                    reply = "No open secret candidate findings to detail."
                return {"reply": reply, "secret_findings": data, "tone": active_tone}

            if topic == "unreviewed_prs":
                if isinstance(data, list) and data:
                    items = [f"• Repo: {pr.get('repo_name')} | PR #{pr.get('number')}: '{pr.get('title')}' by {pr.get('author')} ({pr.get('html_url')})" for pr in data]
                    reply = f"Open unreviewed PRs details:\n" + "\n".join(items)
                else:
                    reply = "No open PRs to detail."
                return {"reply": reply, "unreviewed_prs": data, "tone": active_tone}

            if topic == "github_activity":
                if isinstance(data, list) and data:
                    items = [f"• [{a.get('repo_name')}] {a.get('summary')} by {a.get('author')} ({str(a.get('timestamp', ''))[:10]})" for a in data]
                    reply = f"GitHub activity details:\n" + "\n".join(items)
                else:
                    reply = "No recent GitHub activity to detail."
                return {"reply": reply, "github_activity": data, "tone": active_tone}

    # 2. Combined Briefing / Cross-Domain Personal Update
    if intent == "combined_briefing":
        gh_status = check_github_sync_status(db_path)
        gm_status = check_gmail_sync_status(db_path)

        reply_parts = ["Here is your Evie Status Report & Recent Activity:\n"]

        if gh_status == "unconfigured":
            reply_parts.append("📊 **GitHub Intelligence**: Unconfigured / Not synchronized.")
        else:
            analytics = get_repository_analytics(db_path)
            reply_parts.append(
                f"📊 **GitHub Intelligence**:\n"
                f"• Synchronized Repositories: {analytics.get('total_repositories', 0)}\n"
                f"• Commit Activity: {analytics.get('commits_yesterday', 0)} yesterday, {analytics.get('commits_last_month', 0)} last month\n"
                f"• Unreviewed PRs: {analytics.get('unreviewed_prs_count', 0)}"
            )

        if gm_status == "unconfigured":
            reply_parts.append("\n📧 **Gmail Intelligence**: Unconfigured / Not synchronized.")
        else:
            gm_summary = get_inbox_intelligence_summary(db_path)
            reply_parts.append(
                f"\n📧 **Gmail Intelligence**:\n"
                f"• Phishing Alerts: {gm_summary.get('phishing_alert_count', 0)}\n"
                f"• Promotional/Newsletters: {gm_summary.get('promotional_count', 0)}\n"
                f"• Important Emails: {gm_summary.get('important_count', 0)}"
            )

        import security_score
        canonical_score = security_score.get_current_security_score(db_path)
        sec_findings = get_secret_findings(db_path)
        reply_parts.append(
            f"\n🛡️ **Security & Exposure Findings**:\n"
            f"• Security Score: {canonical_score['score']}/100 (Grade: {canonical_score['grade']})\n"
            f"• Open Secret Candidates: {len(sec_findings)}"
        )

        return {"reply": "\n".join(reply_parts), "tone": active_tone}

    # 3. GitHub Secret Candidate Breakdown by Repository
    if intent == "github_secrets_by_repo":
        if check_github_sync_status(db_path) == "unconfigured":
            return {"reply": "GitHub data has not been synchronized or configured yet.", "repo_secrets": [], "tone": active_tone}

        repo_secrets = get_secret_findings_by_repository(db_path)
        update_context("secret_findings", repo_secrets)
        if repo_secrets:
            items = [f"• {r['repo_name']}: {r['candidate_count']} high-entropy secret candidate(s)" for r in repo_secrets]
            reply = f"Repository Secret Candidates Breakdown ({len(repo_secrets)} repository(ies) affected):\n" + "\n".join(items)
        else:
            reply = "No open secret candidates detected in monitored repositories."
        return {"reply": reply, "repo_secrets": repo_secrets, "tone": active_tone}

    # 4. GitHub Secret Exposure Query
    if intent == "github_secrets":
        if check_github_sync_status(db_path) == "unconfigured":
            return {"reply": "GitHub data has not been synchronized or configured yet.", "secret_findings": [], "tone": active_tone}

        findings = get_secret_findings(db_path)
        if repo_name:
            findings = [f for f in findings if repo_name.lower() in f.get("repo_name", "").lower()]

        page_size = 5
        update_context("secret_findings", findings, {"page": 1, "page_size": page_size})
        if findings:
            first_page = findings[:page_size]
            finding_list = [f"• Repo: {f['repo_name']} (commit {str(f['commit_sha'])[:7]}): {f.get('secret_type') or f.get('pattern_type')} ({f.get('redacted_value') or f.get('redacted_preview')}) in {f.get('filename') or f.get('file_path')}" for f in first_page]
            reply = f"Alert: Detected {len(findings)} high-entropy secret candidate(s) across monitored repositories.\n\nShowing candidates 1-{min(page_size, len(findings))}:\n" + "\n".join(finding_list)
            if len(findings) > page_size:
                reply += f"\n\nThere are {len(findings) - page_size} additional secret candidates. Say 'show me more' to view the next page."
        else:
            reply = "No exposed secret candidates or API keys have been detected in monitored repositories."
        return {"reply": reply, "secret_findings": findings, "tone": active_tone}

    # 5. GitHub PRs Query
    if intent == "github_prs":
        if check_github_sync_status(db_path) == "unconfigured":
            return {"reply": "GitHub data has not been synchronized or configured yet.", "unreviewed_prs": [], "tone": active_tone}

        unreviewed = get_unreviewed_prs(db_path)
        if repo_name:
            unreviewed = [pr for pr in unreviewed if repo_name.lower() in pr.get("repo_name", "").lower()]

        update_context("unreviewed_prs", unreviewed)
        if unreviewed:
            pr_list = [f"• {pr['repo_name']} PR #{pr['number']}: '{pr['title']}' by {pr['author']}" for pr in unreviewed]
            reply = f"There are {len(unreviewed)} open PR(s) waiting for review:\n" + "\n".join(pr_list)
        else:
            reply = "No open pull requests are currently waiting for review."
        return {"reply": reply, "unreviewed_prs": unreviewed, "tone": active_tone}

    # 6. GitHub Repositories Query
    if intent == "github_repos":
        if check_github_sync_status(db_path) == "unconfigured":
            return {"reply": "GitHub data has not been synchronized or configured yet.", "analytics": {}, "tone": active_tone}

        analytics = get_repository_analytics(db_path)
        repos = analytics.get("repositories", [])
        if repos:
            repo_names = [f"• {r['full_name']} ({'private' if r.get('is_private') else 'public'})" for r in repos]
            reply = f"You have {len(repos)} synchronized repository(ies):\n" + "\n".join(repo_names)
        else:
            reply = "0 synchronized repositories found."
        return {"reply": reply, "analytics": analytics, "tone": active_tone}

    # 7. GitHub Commits & Activity Query
    if intent == "github_commits":
        if check_github_sync_status(db_path) == "unconfigured":
            return {"reply": "GitHub data has not been synchronized or configured yet.", "analytics": {}, "tone": active_tone}

        analytics = get_repository_analytics(db_path)

        # Context resolution if user says "that repo" or repo_name is missing but stored in context
        target_repo = repo_name
        if not target_repo and last_chat_context and last_chat_context.get("topic") == "github_activity":
            data = last_chat_context.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("repo_name"):
                target_repo = data[0]["repo_name"]

        # Handle specific repository filter
        if target_repo:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT repo_name, identifier as sha, summary, author, timestamp, html_url FROM github_activity_log WHERE activity_type='commit' AND LOWER(repo_name) LIKE ? ORDER BY timestamp DESC LIMIT 5",
                    (f"%{target_repo.lower()}%",),
                ).fetchall()
            activity_items = [dict(r) for r in rows]
            update_context("github_activity", activity_items)
            if rows:
                commit_lines = [f"• [{r['repo_name']}] {r['summary']} by {r['author']} ({str(r['sha'])[:7]})" for r in rows]
                reply = f"Recent Commits for repository '{target_repo}':\n" + "\n".join(commit_lines)
            else:
                reply = f"No recent commits found matching repository '{target_repo}' in synchronized activity log."
            return {"reply": reply, "analytics": analytics, "tone": active_tone}

        # Handle time range filters
        if time_range == "yesterday":
            y_act = get_yesterday_activity(db_path)
            count = y_act.get("commit_count", 0)
            items = y_act.get("items", [])
            update_context("github_activity", items)
            if items:
                act_lines = [f"• [{a['repo_name']}] {a['summary']} by {a['author']}" for a in items[:5]]
                reply = f"Yesterday's GitHub Activity ({count} commit(s)):\n" + "\n".join(act_lines)
            else:
                reply = "0 commit(s) recorded yesterday across synchronized repositories."
        elif time_range == "last_month":
            reply = f"You made {analytics.get('commits_last_month', 0)} commit(s) last month across synchronized repositories."
        elif time_range == "this_year":
            reply = f"You made {analytics.get('commits_this_year', 0)} commit(s) this year across synchronized repositories."
        else:
            activity = get_recent_activity(db_path, days=7)
            update_context("github_activity", activity)
            if activity:
                act_lines = [f"• [{a['repo_name']}] {a['summary']} by {a['author']}" for a in activity[:5]]
                reply = f"Recent GitHub Activity ({len(activity)} commit(s) recently):\n" + "\n".join(act_lines)
            else:
                reply = f"Commit Activity Summary: {analytics.get('commits_yesterday', 0)} yesterday, {analytics.get('commits_last_month', 0)} last month, {analytics.get('commits_this_year', 0)} this year ({analytics.get('commits_total', 0)} total)."
        return {"reply": reply, "analytics": analytics, "tone": active_tone}

    # 8. Gmail Important Emails Query
    if intent == "gmail_important":
        if check_gmail_sync_status(db_path) == "unconfigured":
            return {"reply": "Gmail integration has not been configured or synchronized yet.", "important_emails": [], "tone": active_tone}

        important = get_important_emails(db_path)
        update_context("gmail_summary", important)
        if important:
            lines = [f"• From: {m['sender']} | Subject: '{m['subject']}' ({m['category']})" for m in important[:5]]
            reply = f"Important Email Messages ({len(important)} recent):\n" + "\n".join(lines)
        else:
            reply = "No important email messages detected in synchronized inbox metadata."
        return {"reply": reply, "important_emails": important, "tone": active_tone}

    # 9. Gmail Top Promotional Sender
    if intent == "gmail_top_promotional_sender":
        if check_gmail_sync_status(db_path) == "unconfigured":
            return {"reply": "Gmail integration has not been configured or synchronized yet.", "top_senders": [], "tone": active_tone}

        top_senders = get_top_promotional_senders(db_path)
        update_context("promotional_emails", top_senders)
        if top_senders:
            top_domain, top_count = top_senders[0]["domain"], top_senders[0]["count"]
            reply = f"The top promotional sender is **{top_domain}** with {top_count} promotional email(s).\n\nTop Promotional Senders:\n"
            reply += "\n".join([f"• {s['domain']}: {s['count']} email(s)" for s in top_senders])
        else:
            reply = "No promotional or newsletter emails have been detected in your inbox."
        return {"reply": reply, "top_senders": top_senders, "tone": active_tone}

    # 10. Gmail Promotional Email Query
    if intent == "gmail_promotional":
        if check_gmail_sync_status(db_path) == "unconfigured":
            return {"reply": "Gmail integration has not been configured or synchronized yet.", "tone": active_tone}

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT subject, sender, sender_domain, received_at, has_unsubscribe FROM gmail_metadata WHERE category='newsletter_promotional' ORDER BY received_at DESC LIMIT 10"
            ).fetchall()]
        update_context("promotional_emails", rows)
        if rows:
            p_lines = [f"• From: {r['sender']} | Subject: '{r['subject']}' | Unsubscribe: {'Available' if r.get('has_unsubscribe') else 'N/A'}" for r in rows]
            reply = f"Promotional & Newsletter Emails ({len(rows)} recent):\n" + "\n".join(p_lines)
        else:
            reply = "No promotional emails found in synchronized metadata."
        return {"reply": reply, "tone": active_tone}

    # 11. Gmail Phishing Query
    if intent == "gmail_phishing":
        if check_gmail_sync_status(db_path) == "unconfigured":
            return {"reply": "Gmail integration has not been configured or synchronized yet.", "tone": active_tone}

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT subject, sender, sender_domain, risk_score, received_at FROM gmail_metadata WHERE category='phishing_alert' ORDER BY received_at DESC LIMIT 5"
            ).fetchall()]
        update_context("suspicious_emails", rows)
        if rows:
            lines = [f"• Alert [{r['risk_score'].upper()}] From: {r['sender']} | Subject: '{r['subject']}'" for r in rows]
            reply = f"Flagged Phishing & Suspicious Email Alerts ({len(rows)} alert(s)):\n" + "\n".join(lines)
        else:
            reply = "No phishing alerts or suspicious emails detected in synchronized inbox metadata."
        return {"reply": reply, "tone": active_tone}

    # 12. Gmail Summary Query
    if intent == "gmail_summary":
        if check_gmail_sync_status(db_path) == "unconfigured":
            return {"reply": "Gmail integration has not been configured or synchronized yet.", "gmail_summary": {}, "tone": active_tone}

        gmail_summary = get_inbox_intelligence_summary(db_path)
        update_context("gmail_summary", gmail_summary)
        reply = f"Gmail Inbox Intelligence Summary:\n• Important Emails: {gmail_summary.get('important_count', 0)}\n• Phishing Alerts: {gmail_summary.get('phishing_alert_count', 0)}\n• Promotional/Newsletters: {gmail_summary.get('promotional_count', 0)}"
        return {"reply": reply, "gmail_summary": gmail_summary, "tone": active_tone}

    # 13. Grounded General Conversational Fallback
    gh_status = check_github_sync_status(db_path)
    gm_status = check_gmail_sync_status(db_path)
    gh_analytics = get_repository_analytics(db_path) if gh_status != "unconfigured" else {}
    gm_summary = get_inbox_intelligence_summary(db_path) if gm_status != "unconfigured" else {}

    reply = (
        "I'm Evie, your personal assistant and security monitoring engine. Here is a summary of what I currently monitor for you:\n\n"
        f"• **GitHub Intelligence**: {gh_analytics.get('total_repositories', 0)} repository(ies) synchronized, {gh_analytics.get('unreviewed_prs_count', 0)} open PR(s), {gh_analytics.get('secret_findings_count', 0)} candidate secret(s).\n"
        f"• **Gmail Intelligence**: {gm_summary.get('phishing_alert_count', 0)} phishing alert(s), {gm_summary.get('promotional_count', 0)} promotional email(s).\n"
        "• **Security Posture**: Monitored across 2FA audit, HIBP breaches, and Drive/Calendar exposure checks.\n\n"
        "Feel free to ask me about your recent commits, inbox activity, PR reviews, exposed secrets, or security status!"
    )
    return {"reply": reply, "tone": active_tone}
