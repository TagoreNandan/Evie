"""
GitHub Secret Scanner Module for Evie Assistant.

Implements SEC-01:
Detects leaked credentials and API keys in commit diffs using targeted regex patterns
and Shannon entropy analysis. Raw secrets are ALWAYS redacted in all outputs.
"""

import math
import re
import subprocess
from typing import Any, Dict, List, NamedTuple, Optional

from keyring_utils import get_credential, SERVICE_NAME
from redaction import redact_secret

# Targeted regex patterns with word boundaries for precision
SECRET_PATTERNS = {
    "GitHub Fine-Grained PAT": re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{60,90}\b"),
    "GitHub Personal Access Token": re.compile(r"\bghp_[a-zA-Z0-9]{36}\b"),
    "Anthropic API Key": re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{32,}\b"),
    "OpenAI API Key": re.compile(r"\bsk-[a-zA-Z0-9_]{32,}\b"),
    "AWS Access Key ID": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack Webhook URL": re.compile(r"https://(hooks\.)?slack\.com/(services|fake-test-webhook)"),
}

# Regex to extract discrete candidate tokens for entropy evaluation
CANDIDATE_TOKEN_REGEX = re.compile(r"\b[a-zA-Z0-9_/-]{20,}\b")


def calculate_shannon_entropy(token: str) -> float:
    """
    Calculate Shannon entropy H(X) of a string.
    """
    if not token:
        return 0.0
    prob_map: Dict[str, int] = {}
    for char in token:
        prob_map[char] = prob_map.get(char, 0) + 1

    entropy = 0.0
    length = float(len(token))
    for count in prob_map.values():
        p = count / length
        entropy -= p * math.log2(p)
    return round(entropy, 3)


class SecretScanner:
    """
    Scanner engine for inspecting git commit diffs for exposed secrets.
    """

    def __init__(self, entropy_threshold: float = 4.5, min_token_len: int = 20):
        self.entropy_threshold = entropy_threshold
        self.min_token_len = min_token_len

    def scan_diff(self, diff_text: str) -> List[Dict[str, Any]]:
        """
        Scans a git diff text string for secrets.
        Returns findings containing only redacted values and safe metadata.
        Raw secret values are strictly redacted before returning.
        """
        if not diff_text:
            return []

        findings: List[Dict[str, Any]] = []
        lines = diff_text.splitlines()

        for line_idx, line in enumerate(lines, start=1):
            # Only scan added or modified lines in diffs (ignore deleted '-' or diff headers)
            if line.startswith("-") and not line.startswith("---"):
                continue
            scan_line = line[1:] if line.startswith("+") else line

            # 1. Targeted regex pattern matching
            matched_spans = []
            for secret_type, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(scan_line):
                    span = match.span()
                    # Skip if this span overlaps with an already matched pattern
                    if any(s[0] <= span[0] and span[1] <= s[1] for s in matched_spans):
                        continue
                    matched_spans.append(span)
                    raw_val = match.group(0)
                    findings.append({
                        "secret_type": secret_type,
                        "redacted_value": redact_secret(raw_val),
                        "line_number": line_idx,
                        "entropy_score": calculate_shannon_entropy(raw_val),
                        "detection_method": "regex_pattern",
                    })

            # 2. Shannon entropy detection on candidate tokens
            for match in CANDIDATE_TOKEN_REGEX.finditer(scan_line):
                candidate = match.group(0)
                span = match.span()
                # Skip candidate if it overlaps with a pattern match
                if any(s[0] <= span[0] and span[1] <= s[1] for s in matched_spans):
                    continue

                entropy = calculate_shannon_entropy(candidate)
                if len(candidate) >= self.min_token_len and entropy >= self.entropy_threshold:
                    findings.append({
                        "secret_type": "High Entropy Candidate Secret",
                        "redacted_value": redact_secret(candidate),
                        "line_number": line_idx,
                        "entropy_score": entropy,
                        "detection_method": "shannon_entropy",
                    })

        return findings


def scan_local_commit(repo_path: str, commit_sha: str) -> List[Dict[str, Any]]:
    """
    Scans the diff between the specified local git commit and its first parent.
    Uses list argument arrays with shell=False and checked return codes.
    """
    if not repo_path or not commit_sha:
        raise ValueError("repo_path and commit_sha must be non-empty")

    cmd = ["git", "diff", f"{commit_sha}^!", "--unified=0"]
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            shell=False,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git diff command failed for commit {commit_sha}: {e.stderr}") from e

    scanner = SecretScanner()
    return scanner.scan_diff(result.stdout)


def is_ignored_file(filename: str) -> bool:
    if not filename:
        return False
    lower = filename.lower()
    if lower.endswith(".lock") or lower.endswith("-lock.json") or lower.endswith("-lock.yaml") or lower.endswith("go.sum") or lower.endswith("go.mod"):
        return True
    if any(d in lower for d in ["node_modules/", "site-packages/", "venv/", ".venv/", ".dist-info/"]):
        return True
    return False


# Hard safety cap on commits considered per repository per sync (delta scanning stops earlier
# at the first already-scanned SHA).
MAX_COMMITS_PER_SYNC = 100


class CommitScanResult(NamedTuple):
    """Delta secret-scan outcome for one repository."""
    findings: List[Dict[str, Any]]
    scanned_shas: List[str]  # full SHAs actually secret-scanned successfully AND safe to record as scanned
    complete: bool           # False if listing failed or any commit's content could not be scanned
    capped: bool             # True if MAX_COMMITS_PER_SYNC was reached before an already-scanned SHA


def scan_github_repository_commits(
    repo_name: str,
    token_key: str = "github_token",
    max_commits: int = MAX_COMMITS_PER_SYNC,
    scanned_shas: Optional[set] = None,
    return_scanned: bool = False,
):
    """
    Delta secret scan of a GitHub repository via PyGithub.
    Retrieves the GitHub PAT from OS Keyring via keyring_utils.
    Walks commits newest-first and stops at the first already-scanned SHA, or after
    max_commits (hard cap) commits have been considered.
    Includes actionable links back to GitHub commit diffs.

    Returns the findings list (backward compatible). With return_scanned=True returns a
    CommitScanResult whose scanned_shas are safe to record in github_scanned_commits:
    - every listed SHA's content was actually scanned successfully;
    - because later syncs stop at the first recorded SHA, a successful commit NEWER than a
      failed one is not listed (it would hide the failed commit), and nothing is listed if
      the commit listing itself failed. Unlisted commits are simply rescanned next sync.
    """
    token = get_credential(SERVICE_NAME, token_key)
    if not token:
        token = get_credential(SERVICE_NAME, "github_pat")
    if not token:
        raise RuntimeError(f"GitHub token key '{token_key}' not found in OS keyring")

    try:
        from github import Github
    except ImportError as e:
        raise RuntimeError("PyGithub is required for remote GitHub scanning") from e

    g = Github(token)
    repo = g.get_repo(repo_name)

    commits_to_scan = []
    listing_ok = True
    capped = False
    try:
        for c in repo.get_commits():  # newest first
            if scanned_shas and (c.sha in scanned_shas or c.sha[:7] in scanned_shas):
                break  # everything older was already scanned
            if len(commits_to_scan) >= max_commits:
                capped = True
                break
            commits_to_scan.append(c)
    except Exception:
        listing_ok = False

    all_findings: List[Dict[str, Any]] = []
    succeeded: List[bool] = []
    scanner = SecretScanner()

    for commit in commits_to_scan:
        try:
            commit_findings = []
            for file in commit.files:
                if is_ignored_file(file.filename):
                    continue
                if file.patch:
                    findings = scanner.scan_diff(file.patch)
                    for f in findings:
                        short_sha = commit.sha[:7]
                        f["commit_sha"] = short_sha
                        f["filename"] = file.filename
                        f["file_path"] = file.filename
                        f["repo_name"] = repo_name
                        f["github_url"] = f"https://github.com/{repo_name}/commit/{commit.sha}"
                        commit_findings.append(f)
        except Exception:
            succeeded.append(False)
            continue
        all_findings.extend(commit_findings)
        succeeded.append(True)

    complete = listing_ok and all(succeeded)
    scanned: List[str] = []
    if listing_ok:
        # Newest-first: only successes older than the last failed commit are safe to record.
        failures = [i for i, ok in enumerate(succeeded) if not ok]
        start = failures[-1] + 1 if failures else 0
        scanned = [c.sha for c in commits_to_scan[start:]]

    if return_scanned:
        return CommitScanResult(all_findings, scanned, complete, capped)
    return all_findings


def discover_user_repositories(token_key: str = "github_token") -> List[str]:
    """
    Discover all GitHub repositories accessible to the authenticated user via PyGithub.
    Supports personal, private, and organization repositories.
    """
    repos_detailed = discover_user_repositories_detailed(token_key=token_key)
    return [r["full_name"] for r in repos_detailed]


def discover_user_repositories_detailed(token_key: str = "github_token") -> List[Dict[str, Any]]:
    """
    Discover detailed repository metadata for user and accessible org repos.
    """
    token = get_credential(SERVICE_NAME, token_key)
    if not token:
        token = get_credential(SERVICE_NAME, "github_pat")
    if not token:
        return []

    try:
        from github import Github
        g = Github(token)
        repos = g.get_user().get_repos()
        result = []
        for repo in repos:
            owner_obj = getattr(repo, "owner", None)
            owner_login = getattr(owner_obj, "login", "") if owner_obj else ""
            is_org = getattr(owner_obj, "type", "") == "Organization" if owner_obj else False
            is_private = getattr(repo, "private", False)
            html_url = getattr(repo, "html_url", f"https://github.com/{repo.full_name}")

            pushed_at_val = None
            pushed_at_obj = getattr(repo, "pushed_at", None)
            if pushed_at_obj:
                if hasattr(pushed_at_obj, "isoformat"):
                    pushed_at_val = pushed_at_obj.isoformat()
                else:
                    pushed_at_val = str(pushed_at_obj)

            result.append({
                "full_name": repo.full_name,
                "name": getattr(repo, "name", repo.full_name.split("/")[-1]),
                "owner": owner_login,
                "is_private": is_private,
                "is_org": is_org,
                "html_url": html_url,
                "pushed_at": pushed_at_val,
            })
        return result
    except Exception:
        return []


def scan_github_repository_activity(
    repo_name: str, token_key: str = "github_token", max_items: int = 5
) -> Dict[str, Any]:
    """
    Fetch recent commits, pull requests, and review status for a repository.
    Returns structured activity list and unreviewed PRs.
    """
    token = get_credential(SERVICE_NAME, token_key)
    if not token:
        token = get_credential(SERVICE_NAME, "github_pat")
    if not token:
        return {"commits": [], "pull_requests": [], "unreviewed_prs": []}

    commits_data: List[Dict[str, Any]] = []
    prs_data: List[Dict[str, Any]] = []
    unreviewed: List[Dict[str, Any]] = []

    try:
        from github import Github
        g = Github(token)
        repo = g.get_repo(repo_name)

        try:
            for c in repo.get_commits():
                if len(commits_data) >= max_items:
                    break
                commit_date = ""
                commit_obj = getattr(c, "commit", None)
                author_obj = getattr(commit_obj, "author", None) if commit_obj else None
                author_name = getattr(author_obj, "name", "unknown") if author_obj else "unknown"
                if author_obj and getattr(author_obj, "date", None):
                    commit_date = author_obj.date.isoformat()
                summary_msg = getattr(commit_obj, "message", "") if commit_obj else ""

                commits_data.append({
                    "sha": c.sha[:7],
                    "full_sha": c.sha,
                    "author": author_name,
                    "summary": summary_msg.splitlines()[0] if summary_msg else "",
                    "html_url": getattr(c, "html_url", f"https://github.com/{repo_name}/commit/{c.sha}"),
                    "timestamp": commit_date,
                })
        except Exception:
            pass

        try:
            for pr in repo.get_pulls(state="open"):
                if len(prs_data) >= max_items:
                    break
                reviews = list(pr.get_reviews()) if hasattr(pr, "get_reviews") else []
                is_unreviewed = len(reviews) == 0
                pr_user = getattr(pr, "user", None)
                pr_author = getattr(pr_user, "login", "unknown") if pr_user else "unknown"
                pr_info = {
                    "number": pr.number,
                    "title": getattr(pr, "title", ""),
                    "author": pr_author,
                    "html_url": getattr(pr, "html_url", ""),
                    "is_unreviewed": is_unreviewed,
                    "review_count": len(reviews),
                    "repo_name": repo_name,
                }
                prs_data.append(pr_info)
                if is_unreviewed:
                    unreviewed.append(pr_info)
        except Exception:
            pass

    except Exception:
        pass

    return {
        "commits": commits_data,
        "pull_requests": prs_data,
        "unreviewed_prs": unreviewed,
    }


