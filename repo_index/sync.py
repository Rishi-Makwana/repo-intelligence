"""Remote change detection, shallow clones, the stale rule and the health report."""

from __future__ import annotations

import base64
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .db import utc_now

GIT_TIMEOUT_SECONDS = 300


class SyncError(Exception):
    pass


def remote_url(repo_id: str) -> str:
    return f"https://github.com/{repo_id}.git"


def clone_path(settings: Settings, repo_id: str) -> Path:
    return settings.clones_dir / repo_id.replace("/", "__")


def _git_env(token: str) -> dict[str, str]:
    """Pass the token as an HTTP header through git's env config, so it never appears in argv,
    the remote URL stored in .git/config, or any log line."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        })
    return env


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "***")
        text = text.replace(base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***")
    return text


def git(args: list[str], *, cwd: Path | None = None, token: str = "") -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=_git_env(token), capture_output=True,
                                text=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s") from None
    if result.returncode != 0:
        raise SyncError(_redact(f"git {args[0]} failed: {result.stderr.strip()}", token))
    return result.stdout


def ls_remote(url: str, token: str = "") -> tuple[str | None, str]:
    """Return (default_branch, sha of the remote HEAD)."""
    out = git(["ls-remote", "--symref", url, "HEAD"], token=token)
    branch, sha = None, None
    for line in out.splitlines():
        left, _, right = line.partition("\t")
        if left.startswith("ref: ") and right == "HEAD":
            branch = left.removeprefix("ref: refs/heads/")
        elif right == "HEAD":
            sha = left
    if not sha:
        raise SyncError(f"git ls-remote returned no HEAD for {url}")
    return branch, sha


def head_sha(clone: Path) -> str | None:
    if not (clone / ".git").exists():
        return None
    try:
        return git(["rev-parse", "HEAD"], cwd=clone).strip()
    except SyncError:
        return None


def ensure_clone(url: str, clone: Path, remote_sha: str, token: str = "") -> bool:
    """Make `clone` a depth-1 checkout of the remote HEAD. Returns True if anything changed."""
    if head_sha(clone) == remote_sha:
        return False
    if (clone / ".git").exists():
        git(["fetch", "--depth", "1", "origin", "HEAD"], cwd=clone, token=token)
        git(["reset", "--hard", "--quiet", "FETCH_HEAD"], cwd=clone)
    else:
        clone.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--quiet", "--depth", "1", url, str(clone)], token=token)
    return True


def last_commit_at(clone: Path) -> str:
    stamp = git(["log", "-1", "--format=%cI"], cwd=clone).strip()
    return datetime.fromisoformat(stamp).astimezone(UTC).isoformat()


def tracked_files(clone: Path) -> list[str]:
    return [p for p in git(["ls-files", "-z"], cwd=clone).split("\0") if p]


def sync_repo(conn: sqlite3.Connection, settings: Settings, repo_id: str) -> Path:
    """ls-remote, record remote_sha, bring the clone to it, record last_commit_at. Returns the clone path."""
    url = remote_url(repo_id)
    branch, sha = ls_remote(url, settings.github_token)
    conn.execute("UPDATE repos SET default_branch = ?, remote_sha = ?, remote_checked_at = ? WHERE repo_id = ?",
                 (branch, sha, utc_now(), repo_id))
    clone = clone_path(settings, repo_id)
    ensure_clone(url, clone, sha, settings.github_token)
    conn.execute("UPDATE repos SET last_commit_at = ? WHERE repo_id = ?", (last_commit_at(clone), repo_id))
    return clone


def is_stale(indexed_sha: str | None, remote_sha: str | None, remote_checked_at: str | None,
             poll_minutes: int, now: datetime | None = None) -> bool:
    """Stale if the index is behind the remote, or the remote has not been checked for 2 x POLL_MINUTES."""
    if not indexed_sha or indexed_sha != remote_sha or not remote_checked_at:
        return True
    now = now or datetime.now(UTC)
    return now - datetime.fromisoformat(remote_checked_at) > timedelta(minutes=2 * poll_minutes)


def health(conn: sqlite3.Connection, settings: Settings) -> dict:
    # ponytail: plain dict for the CLI; the tools.py `health` tool wraps this in the envelope.
    repos = []
    for row in conn.execute("SELECT * FROM repos ORDER BY repo_id"):
        errors = conn.execute(
            "SELECT stage, message, at FROM errors WHERE repo_id = ? ORDER BY rowid DESC LIMIT 3",
            (row["repo_id"],)).fetchall()
        repos.append({
            "repo": row["repo_id"],
            "indexed_sha": row["indexed_sha"],
            "remote_sha": row["remote_sha"],
            "stale": is_stale(row["indexed_sha"], row["remote_sha"], row["remote_checked_at"],
                              settings.poll_minutes),
            "remote_checked_at": row["remote_checked_at"],
            "last_commit_at": row["last_commit_at"],
            "last_errors": [dict(e) for e in errors],
        })
    runs = {}
    for kind in ("update", "vulns", "selfcheck"):
        run = conn.execute("SELECT started_at, finished_at, status, detail FROM runs WHERE kind = ? "
                           "ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        runs[kind] = dict(run) if run else None
    return {"repos": repos, "last_runs": runs}
