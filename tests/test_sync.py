from datetime import UTC, datetime, timedelta

import pytest

from repo_index import db, sync
from tests.conftest import make_remote, push_commit, run_git


@pytest.fixture
def remotes(tmp_path, monkeypatch):
    urls, works = {}, {}
    for repo_id in ("owner/payments-api", "owner/address-ui"):
        urls[repo_id], works[repo_id] = make_remote(tmp_path, repo_id.split("/")[1])
    monkeypatch.setattr(sync, "remote_url", urls.__getitem__)
    return works


@pytest.fixture
def conn(settings, repos_config):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    return conn


def test_sync_clones_every_repo_then_second_run_changes_nothing(settings, conn, remotes):
    for repo_id, work in remotes.items():
        clone = sync.sync_repo(conn, settings, repo_id)
        assert clone == settings.clones_dir / repo_id.replace("/", "__")
        assert sync.head_sha(clone) == run_git("rev-parse", "HEAD", cwd=work)
        assert (clone / "README.md").exists()

    clones = {r: settings.clones_dir / r.replace("/", "__") for r in remotes}
    before = {r: (c / ".git" / "HEAD").stat().st_mtime_ns for r, c in clones.items()}
    for repo_id in remotes:
        url = sync.remote_url(repo_id)
        _, sha = sync.ls_remote(url)
        assert sync.ensure_clone(url, clones[repo_id], sha) is False
        sync.sync_repo(conn, settings, repo_id)
    assert {r: (c / ".git" / "HEAD").stat().st_mtime_ns for r, c in clones.items()} == before


def test_sync_records_remote_state_and_last_commit(settings, conn, remotes):
    sync.sync_repo(conn, settings, "owner/payments-api")
    row = conn.execute("SELECT * FROM repos WHERE repo_id = 'owner/payments-api'").fetchone()
    assert row["default_branch"] == "main"
    assert row["remote_sha"] == run_git("rev-parse", "HEAD", cwd=remotes["owner/payments-api"])
    assert datetime.fromisoformat(row["remote_checked_at"]).tzinfo is not None
    assert datetime.fromisoformat(row["last_commit_at"]).utcoffset() == timedelta(0)


def test_new_remote_commit_is_fetched_into_existing_clone(settings, conn, remotes):
    clone = sync.sync_repo(conn, settings, "owner/address-ui")
    new_sha = push_commit(remotes["owner/address-ui"])
    sync.sync_repo(conn, settings, "owner/address-ui")
    assert sync.head_sha(clone) == new_sha
    assert run_git("rev-parse", "--is-shallow-repository", cwd=clone) == "true"


def test_health_is_stale_until_indexed_sha_is_set(settings, conn, remotes):
    sync.sync_repo(conn, settings, "owner/payments-api")
    report = {r["repo"]: r for r in sync.health(conn, settings)["repos"]}
    assert report["owner/payments-api"]["stale"] is True
    assert report["owner/payments-api"]["indexed_sha"] is None

    remote_sha = report["owner/payments-api"]["remote_sha"]
    db.rebuild_repo(conn, "owner/payments-api", {}, indexed_sha=remote_sha)
    report = {r["repo"]: r for r in sync.health(conn, settings)["repos"]}
    assert report["owner/payments-api"]["stale"] is False
    assert report["owner/address-ui"]["stale"] is True


def test_stale_rule_both_conditions():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    fresh = (now - timedelta(minutes=19)).isoformat()
    old = (now - timedelta(minutes=21)).isoformat()
    assert sync.is_stale("a", "a", fresh, 10, now) is False
    assert sync.is_stale("a", "b", fresh, 10, now) is True       # behind the remote
    assert sync.is_stale("a", "a", old, 10, now) is True         # polling itself is broken
    assert sync.is_stale(None, "a", fresh, 10, now) is True
    assert sync.is_stale("a", "a", None, 10, now) is True


def test_token_never_in_argv_url_or_errors(tmp_path, monkeypatch):
    token = "ghp_testtoken123"
    calls = []
    real_run = sync.subprocess.run

    def spy(args, **kwargs):
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", spy)
    with pytest.raises(sync.SyncError):
        sync.git(["ls-remote", f"file://{tmp_path}/missing.git"], token=token)
    assert calls and all(token not in " ".join(map(str, args)) for args in calls)

    with pytest.raises(sync.SyncError) as excinfo:  # even if git echoes it back, the message is redacted
        sync.git(["ls-remote", f"file://{tmp_path}/missing-{token}.git"], token=token)
    assert token not in str(excinfo.value)
    env = sync._git_env(token)
    assert token not in env["GIT_CONFIG_VALUE_0"]  # base64 inside an Authorization header, not a URL
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_clone_does_not_store_token_in_git_config(settings, conn, remotes, monkeypatch):
    clone = sync.clone_path(settings, "owner/payments-api")
    url = sync.remote_url("owner/payments-api")
    _, sha = sync.ls_remote(url, "ghp_testtoken123")
    sync.ensure_clone(url, clone, sha, "ghp_testtoken123")
    assert "ghp_testtoken123" not in (clone / ".git" / "config").read_text()
