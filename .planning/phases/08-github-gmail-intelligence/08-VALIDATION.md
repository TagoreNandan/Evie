# Phase 8 Validation: GitHub + Gmail Personal & Work Intelligence Layer

## Success Criteria Checklist

1. [ ] **GitHub Account Discovery & Org Support (`SEC-07`)**: Discovers personal, private, and organization repositories accessible to the PAT.
2. [ ] **PR & Unreviewed PR Detection (`SEC-07`)**: Tracks open PRs and accurately identifies unreviewed PRs needing attention.
3. [ ] **Commit Analytics (`SEC-07`)**: Calculates commit counts per day, month, and year across monitored repositories.
4. [ ] **Actionable Diff Links & Redaction (`SEC-07`)**: Persisted secret scanner findings include actionable commit diff URLs (`https://github.com/{repo}/commit/{sha}`) with redacted secret previews.
5. [ ] **Gmail Metadata Classification & Intelligence (`MAIL-01`)**: Categorizes emails into `important`, `phishing_alert`, and `newsletter_promotional` using read-only headers without reading message bodies.
6. [ ] **Sender Frequency Analytics & Cleanup Recommendations (`MAIL-01`)**: Identifies high-frequency marketing senders and generates cleanup/unsubscribe recommendations.
7. [ ] **Proposal-Confirmation Safety Boundary (`MAIL-01`)**: Cleanup/unsubscribe recommendations produce a proposal card (`/gmail/propose_cleanup`) requiring explicit confirmation (`/gmail/confirm_cleanup`) before execution.
8. [ ] **Factual Conversational Query Routing (`POST /chat`)**: `/chat` resolves exact factual queries for repositories, commit analytics, unreviewed PRs, phishing alerts, and email newsletters from SQLite data without LLM hallucination.
9. [ ] **100% Test Pass Rate**: `tests/test_github_gmail_intelligence.py` passes cleanly alongside all existing tests.
