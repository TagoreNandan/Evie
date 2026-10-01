"""
GitHub sync delta-processing & webhook correctness (Protected Area #5;
AGENTS.md priority 6, TRD Section 5, Acceptance Criteria Sections 2-3).

Fast and mocked: PyGithub is replaced by an in-memory fake, and credentials are patched per test.
"""

import hashlib
import hmac
import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from integrations import github_watch
from integrations.github_watch import MAX_COMMITS_PER_SYNC, scan_github_repository_commits
from main import app, init_db
from services.github_intelligence import init_github_tables, process_github_webhook_event, sync_github_intelligence

client = TestClient(app)

RAW_SECRET = "AKIAABCDEFGHIJKLMNOP"  # AWS-access-key-shaped test value
WEBHOOK_SECRET = "delta-test-webhook-secret"


class FakeFile:
    def __init__(self, filename, patch):
        self.filename, self.patch = filename, patch


class FakeCommit:
    def __init__(self, sha, patch="+ print('hello')", fail=False, filename="app.py"):
        self.sha, self._patch, self._fail, self._filename = sha, patch, fail, filename

    @property
    def files(self):
        if self._fail:
            raise RuntimeError("GitHub API error while fetching commit files")
        return [FakeFile(self._filename, self._patch)]


class FakeRepo:
    def __init__(self, commits, listing_fails_after=None):
        self.commits, self.listing_fails_after = commits, listing_fails_after
        self.listed = 0

    def get_commits(self):
        for i, c in enumerate(self.commits):
            if self.listing_fails_after is not None and i >= self.listing_fails_after:
                raise RuntimeError("GitHub API pagination error")
            self.listed += 1
            yield c


class FakeGithub:
    repos = {}

    def __init__(self, token):
        pass

    def get_repo(self, name):
        return FakeGithub.repos[name]


def sha(n):
    return hashlib.sha1(str(n).encode()).hexdigest()  # distinct 7-char prefixes, like real SHAs


@pytest.fixture
def gh(tmp_path, monkeypatch):
    db = str(tmp_path / "delta_evie.db")
    init_db(db)
    init_github_tables(db)
    FakeGithub.repos = {}
    monkeypatch.setattr("github.Github", FakeGithub)
    monkeypatch.setattr("integrations.github_watch.get_credential", lambda service, key: "fake_token")
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity",
                        lambda name, token_key="github_token": {"commits": [], "pull_requests": []})
    state = {"repos": []}
    monkeypatch.setattr("services.github_intelligence.discover_user_repositories_detailed",
                        lambda token_key="github_token": state["repos"])

    def set_repos(**pushed):
        state["repos"] = [
            {"full_name": name.replace("__", "/"), "name": name.split("__")[-1], "owner": "org",
             "is_private": False, "is_org": True, "html_url": "url", "pushed_at": p}
            for name, p in pushed.items()
        ]
    monkeypatch.setattr(main, "DB_PATH", db)
    return type("GH", (), {"db": db, "set_repos": staticmethod(set_repos)})


def q(db, sql, *args):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, args).fetchall()


def scanned(db, repo):
    return {r[0] for r in q(db, "SELECT commit_sha FROM github_scanned_commits WHERE repo_name = ?", repo)}


def checkpoint(db, repo):
    rows = q(db, "SELECT last_pushed_at FROM github_repo_cache WHERE full_name = ?", repo)
    return rows[0][0] if rows else None


# --- Delta processing ---

def test_successfully_scanned_shas_are_recorded_and_checkpoint_advances(gh):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(3)), FakeCommit(sha(2)), FakeCommit(sha(1))])
    gh.set_repos(org__api="2026-09-28T10:00:00Z")

    res = sync_github_intelligence(gh.db)
    assert scanned(gh.db, "org/api") == {sha(3), sha(2), sha(1)}
    assert checkpoint(gh.db, "org/api") == "2026-09-28T10:00:00Z"
    assert (res["repositories_changed"], res["repositories_skipped"], res["repositories_incomplete"]) == (1, 0, 0)


def test_unchanged_pushed_at_skips_all_per_repo_work(gh, monkeypatch):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(1))])
    gh.set_repos(org__api="2026-09-28T10:00:00Z")
    sync_github_intelligence(gh.db)

    def forbidden(*a, **k):
        raise AssertionError("per-repo scanning ran for an unchanged repository")
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_activity", forbidden)
    monkeypatch.setattr("services.github_intelligence.scan_github_repository_commits", forbidden)

    res = sync_github_intelligence(gh.db)
    assert (res["repositories_changed"], res["repositories_skipped"]) == (0, 1)


def test_three_of_n_changed_processes_only_those_three(gh):
    names = [f"r{i}" for i in range(10)]
    for n in names:
        FakeGithub.repos[f"org/{n}"] = FakeRepo([FakeCommit(sha(1))])
    gh.set_repos(**{f"org__{n}": "t1" for n in names})
    sync_github_intelligence(gh.db)

    for n in names:
        FakeGithub.repos[f"org/{n}"] = FakeRepo([FakeCommit(sha(2)), FakeCommit(sha(1))])
    gh.set_repos(**{f"org__{n}": ("t2" if n in ("r1", "r4", "r7") else "t1") for n in names})
    res = sync_github_intelligence(gh.db)

    assert (res["repositories_changed"], res["repositories_skipped"]) == (3, 7)
    listed = {n: FakeGithub.repos[f"org/{n}"].listed for n in names}
    assert {n for n, c in listed.items() if c} == {"r1", "r4", "r7"}
    for n in ("r1", "r4", "r7"):
        assert listed[n] == 2  # new commit + stop at the already-scanned one


def test_zero_change_sync_does_no_commit_listing(gh):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(1))])
    gh.set_repos(org__api="t1")
    sync_github_intelligence(gh.db)
    FakeGithub.repos["org/api"] = repo = FakeRepo([FakeCommit(sha(1))])
    res = sync_github_intelligence(gh.db)
    assert repo.listed == 0 and res["repositories_skipped"] == 1


def test_scanning_stops_at_first_already_scanned_sha(gh):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(i)) for i in (2, 1)])
    gh.set_repos(org__api="t1")
    sync_github_intelligence(gh.db)

    FakeGithub.repos["org/api"] = repo = FakeRepo([FakeCommit(sha(i)) for i in (5, 4, 3, 2, 1)])
    gh.set_repos(org__api="t2")
    sync_github_intelligence(gh.db)
    assert repo.listed == 4  # sha5, sha4, sha3, then stops at the already-scanned sha2
    assert scanned(gh.db, "org/api") == {sha(i) for i in (1, 2, 3, 4, 5)}


def test_push_larger_than_ten_commits_is_scanned_up_to_hard_cap(gh):
    FakeGithub.repos["org/big"] = FakeRepo([FakeCommit(sha(i)) for i in range(150, 0, -1)])
    gh.set_repos(org__big="t1")
    sync_github_intelligence(gh.db)
    recorded = scanned(gh.db, "org/big")
    assert len(recorded) == MAX_COMMITS_PER_SYNC == 100
    assert recorded == {sha(i) for i in range(150, 50, -1)}  # newest 100

    result = scan_github_repository_commits("org/big", scanned_shas=set(), return_scanned=True)
    assert result.capped is True and result.complete is True


def test_failed_commit_scan_keeps_checkpoint_and_records_only_safe_shas(gh):
    # newest-first: sha4 ok, sha3 fails, sha2 ok, sha1 ok
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(4)), FakeCommit(sha(3), fail=True), FakeCommit(sha(2)), FakeCommit(sha(1))])
    gh.set_repos(org__api="t1")
    res = sync_github_intelligence(gh.db)

    assert checkpoint(gh.db, "org/api") is None  # not advanced: the delta will be retried
    assert res["repositories_incomplete"] == 1
    # sha4 succeeded but is NEWER than the failed sha3, so recording it would hide sha3 next time
    assert scanned(gh.db, "org/api") == {sha(2), sha(1)}

    # Next sync (GitHub fixed): sha4 and sha3 are scanned, walk stops at sha2, checkpoint advances
    FakeGithub.repos["org/api"] = repo = FakeRepo([FakeCommit(sha(4)), FakeCommit(sha(3)), FakeCommit(sha(2)), FakeCommit(sha(1))])
    res = sync_github_intelligence(gh.db)
    assert repo.listed == 3
    assert scanned(gh.db, "org/api") == {sha(i) for i in (1, 2, 3, 4)}
    assert checkpoint(gh.db, "org/api") == "t1" and res["repositories_incomplete"] == 0


def test_failed_listing_records_nothing_and_keeps_checkpoint(gh):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(i)) for i in (3, 2, 1)], listing_fails_after=2)
    gh.set_repos(org__api="t1")
    res = sync_github_intelligence(gh.db)
    assert scanned(gh.db, "org/api") == set()
    assert checkpoint(gh.db, "org/api") is None and res["repositories_incomplete"] == 1


def test_scanner_exception_keeps_checkpoint(gh, monkeypatch):
    monkeypatch.setattr("integrations.github_watch.get_credential", lambda service, key: None)  # no token -> raises
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(1))])
    gh.set_repos(org__api="t1")
    res = sync_github_intelligence(gh.db)
    assert checkpoint(gh.db, "org/api") is None and scanned(gh.db, "org/api") == set()
    assert res["repositories_incomplete"] == 1


def test_secret_in_new_commit_is_flagged_and_redacted(gh):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(2), patch=f"+ AWS_KEY={RAW_SECRET}", filename="config.py"), FakeCommit(sha(1))])
    gh.set_repos(org__api="t1")
    res = sync_github_intelligence(gh.db)
    rows = q(gh.db, "SELECT file_path, pattern_type, redacted_preview, status FROM secret_findings")
    assert res["secrets_found"] == 1
    assert rows[0][0] == "config.py" and rows[0][3] == "open"
    assert RAW_SECRET not in json.dumps(rows)


# --- Webhooks ---

def _signed(payload, delivery, secret=WEBHOOK_SECRET, event="push"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig, "Content-Type": "application/json"}


PUSH = {
    "repository": {"full_name": "org/api", "name": "api"},
    "sender": {"login": "dev"},
    "ref": "refs/heads/main",
    "commits": [{"id": sha(9), "message": "feat: add config", "author": {"name": "dev"}, "added": ["config.py"], "modified": []}],
}


@pytest.fixture
def with_secret(monkeypatch):
    monkeypatch.setattr("keyring_utils.get_credential", lambda service, key: WEBHOOK_SECRET if key == "github_webhook_secret" else None)


def test_webhook_rejected_when_secret_not_configured(gh):
    body, headers = _signed(PUSH, "d-noconf")
    res = client.post("/webhooks/github", content=body, headers=headers)
    assert res.status_code == 503 and "WEBHOOK_SECRET_NOT_CONFIGURED" in res.json()["detail"]
    assert q(gh.db, "SELECT COUNT(*) FROM github_events")[0][0] == 0
    assert q(gh.db, "SELECT COUNT(*) FROM github_activity_log")[0][0] == 0


def test_webhook_rejected_for_invalid_or_missing_signature(gh, with_secret):
    body, headers = _signed(PUSH, "d-bad", secret="wrong-secret")
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401
    headers.pop("X-Hub-Signature-256")
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401
    assert q(gh.db, "SELECT COUNT(*) FROM github_events")[0][0] == 0


def test_valid_webhook_accepted_with_truthful_signature_valid(gh, with_secret):
    body, headers = _signed(PUSH, "d-ok")
    res = client.post("/webhooks/github", content=body, headers=headers)
    assert res.status_code == 200 and res.json()["status"] == "processed"
    assert q(gh.db, "SELECT delivery_id, signature_valid FROM github_webhook_deliveries") == [("d-ok", 1)]


def test_duplicate_delivery_is_idempotent(gh, with_secret):
    body, headers = _signed(PUSH, "d-dup")
    assert client.post("/webhooks/github", content=body, headers=headers).json()["status"] == "processed"
    assert client.post("/webhooks/github", content=body, headers=headers).json()["status"] == "already_processed"
    assert q(gh.db, "SELECT COUNT(*) FROM github_events WHERE delivery_id = 'd-dup'")[0][0] == 1
    assert q(gh.db, "SELECT COUNT(*) FROM github_activity_log WHERE identifier = ?", sha(9))[0][0] == 1


def test_webhook_never_marks_pushed_sha_as_scanned(gh, with_secret):
    body, headers = _signed(PUSH, "d-scan")
    client.post("/webhooks/github", content=body, headers=headers)
    assert scanned(gh.db, "org/api") == set()

    # The next sync scans the pushed commit's real diff (not hidden by the webhook)
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(9), patch=f"+ KEY={RAW_SECRET}", filename="config.py")])
    gh.set_repos(org__api="t-after-push")
    res = sync_github_intelligence(gh.db)
    assert sha(9) in scanned(gh.db, "org/api")
    assert res["secrets_found"] == 1


def test_webhook_payload_finding_does_not_hide_commit_from_sync(gh):
    # A secret in the commit message is found by the webhook, but that is not a content scan
    process_github_webhook_event(
        {"event_type": "push", "repository": {"full_name": "org/api"},
         "commits": [{"id": sha(7), "message": f"oops {RAW_SECRET}", "author": {"name": "dev"}}]},
        gh.db,
    )
    assert q(gh.db, "SELECT COUNT(*) FROM secret_findings")[0][0] == 1
    FakeGithub.repos["org/api"] = repo = FakeRepo([FakeCommit(sha(7))])
    gh.set_repos(org__api="t1")
    sync_github_intelligence(gh.db)
    assert repo.listed == 1 and sha(7) in scanned(gh.db, "org/api")


# --- False positives (AC 2.4) ---

def test_false_positive_prevents_reflagging_same_file_line_pattern(gh):
    leak = f"+ AWS_KEY={RAW_SECRET}"
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(1), patch=leak, filename="config.py")])
    gh.set_repos(org__api="t1")
    sync_github_intelligence(gh.db)
    (finding_id,) = q(gh.db, "SELECT id FROM secret_findings")[0]

    from services.finding_actions import resolve_finding
    resolve_finding(gh.db, "secret", finding_id, "false_positive")

    # A newer commit touching the same file/line with the same pattern is not re-flagged
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(2), patch=leak, filename="config.py"), FakeCommit(sha(1))])
    gh.set_repos(org__api="t2")
    res = sync_github_intelligence(gh.db)
    assert res["secrets_found"] == 0
    assert q(gh.db, "SELECT status FROM secret_findings") == [("false_positive",)]



def test_false_positive_is_honoured_on_the_webhook_path(gh):
    # Payload scans have no real file path; they are recorded under 'webhook_commit'
    leak = f"+ AWS_KEY={RAW_SECRET}"

    def push(commit_sha):
        return process_github_webhook_event(
            {"event_type": "push", "repository": {"full_name": "org/api"},
             "commits": [{"id": commit_sha, "author": {"name": "dev"}, "patch": leak}]},
            gh.db,
        )

    assert push(sha(3))["secrets_found"] == 1
    (finding_id, path) = q(gh.db, "SELECT id, file_path FROM secret_findings")[0]
    assert path == "webhook_commit"
    from services.finding_actions import resolve_finding
    resolve_finding(gh.db, "secret", finding_id, "false_positive")

    assert push(sha(4))["secrets_found"] == 0
    assert q(gh.db, "SELECT COUNT(*) FROM secret_findings WHERE status = 'open'")[0][0] == 0


def test_resolved_finding_is_not_suppressed_like_a_false_positive(gh):
    leak = f"+ AWS_KEY={RAW_SECRET}"
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(1), patch=leak, filename="config.py")])
    gh.set_repos(org__api="t1")
    sync_github_intelligence(gh.db)
    (finding_id,) = q(gh.db, "SELECT id FROM secret_findings")[0]
    from services.finding_actions import resolve_finding
    resolve_finding(gh.db, "secret", finding_id, "resolved")

    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(2), patch=leak, filename="config.py"), FakeCommit(sha(1))])
    gh.set_repos(org__api="t2")
    assert sync_github_intelligence(gh.db)["secrets_found"] == 1  # the secret was re-committed


def test_same_commit_finding_is_not_duplicated(gh):
    leak = f"+ AWS_KEY={RAW_SECRET}"
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(2)), FakeCommit(sha(1), patch=leak, filename="config.py", fail=False)])
    gh.set_repos(org__api="t1")
    sync_github_intelligence(gh.db)
    # Force a rescan of the same commit (e.g. after a failed scan) - no duplicate row
    with sqlite3.connect(gh.db) as conn:
        conn.execute("DELETE FROM github_scanned_commits")
        conn.commit()
    gh.set_repos(org__api="t2")
    sync_github_intelligence(gh.db)
    assert q(gh.db, "SELECT COUNT(*) FROM secret_findings")[0][0] == 1


# --- Leakage ---

def test_no_raw_secret_in_db_responses_or_logs(gh, with_secret, caplog):
    FakeGithub.repos["org/api"] = FakeRepo([FakeCommit(sha(1), patch=f"+ KEY={RAW_SECRET}", filename="config.py")])
    gh.set_repos(org__api="t1")
    with caplog.at_level(logging.DEBUG):
        sync_github_intelligence(gh.db)
        payload = {**PUSH, "commits": [{"id": sha(2), "message": f"oops {RAW_SECRET}", "author": {"name": "dev"}}]}
        body, headers = _signed(payload, "d-leak")
        res = client.post("/webhooks/github", content=body, headers=headers)
    assert res.status_code == 200
    with sqlite3.connect(gh.db) as conn:
        dump = "\n".join(conn.iterdump())
    response_without_echo = json.dumps(res.json()["webhook_processing"]) + json.dumps(res.json()["briefing"])
    assert RAW_SECRET not in dump
    assert RAW_SECRET not in response_without_echo
    assert RAW_SECRET not in caplog.text
    assert WEBHOOK_SECRET not in dump and WEBHOOK_SECRET not in res.text
