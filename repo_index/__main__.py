"""CLI: init | update [--repo X] | vulns | health | selfcheck | serve."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import db, facts, manifests, sync
from .config import ConfigError, Integration, RepoConfig, ReposConfig, Settings, load_all, load_settings


@dataclass
class StageContext:
    settings: Settings
    repos_config: ReposConfig
    integrations: dict[str, Integration]
    commit_sha: str               # the clone's HEAD; becomes indexed_sha
    config_sha: str               # the config repo's HEAD; commit_sha of repos.yaml facts
    files: list[str]              # git ls-files of the clone
    rows: db.Rows = field(default_factory=dict)       # merged output of the stages that ran before
    warnings: list[str] = field(default_factory=list)  # non-fatal problems, recorded as errors


Stage = Callable[[Path, RepoConfig, StageContext], db.Rows]


def manifests_stage(clone: Path, repo: RepoConfig, ctx: StageContext) -> db.Rows:
    libraries, errors = manifests.parse_manifests(clone, ctx.files, ctx.commit_sha,
                                                  ctx.repos_config.exclude_folders)
    ctx.warnings += [f"manifest parse failed: {e}" for e in errors]
    return {"libraries": libraries}


def facts_stage(clone: Path, repo: RepoConfig, ctx: StageContext) -> db.Rows:
    return {"facts": facts.build_facts(clone, ctx.files, ctx.rows.get("libraries", []), repo, ctx.repos_config,
                                       ctx.integrations, ctx.commit_sha, ctx.config_sha, ctx.settings.max_file_kb)}


# Order matters: later stages read earlier output from ctx.rows. Append new stages (code map, embeddings).
STAGES: list[Stage] = [manifests_stage, facts_stage]


def run_stages(clone: Path, repo: RepoConfig, ctx: StageContext) -> db.Rows:
    for stage in STAGES:
        for table, rows in stage(clone, repo, ctx).items():
            if table == "repo":
                ctx.rows.setdefault("repo", {}).update(rows)
            else:
                ctx.rows.setdefault(table, []).extend(rows)
    return ctx.rows


def config_sha(config_dir: Path) -> str:
    # ponytail: HEAD of the config repo, even if repos.yaml has uncommitted edits; "uncommitted" outside git.
    try:
        return sync.git(["rev-parse", "HEAD"], cwd=config_dir).strip()
    except (sync.SyncError, OSError):
        return "uncommitted"


def config_fingerprint(settings: Settings) -> str:
    digest = hashlib.sha256()
    for name in ("repos.yaml", "integrations.yaml"):
        digest.update((settings.config_dir / name).read_bytes())
    return digest.hexdigest()


def run_update(settings: Settings, only_repo: str | None = None) -> dict:
    repos_config, integrations, _ = load_all(settings)
    selected = [repos_config.get(only_repo)] if only_repo else list(repos_config.repos)
    conn = db.open_index(settings)
    db.upsert_repos(conn, repos_config)
    fingerprint, cfg_sha = config_fingerprint(settings), config_sha(settings.config_dir)
    run_id = db.start_run(conn, "update")
    summary = {"rebuilt": [], "unchanged": [], "failed": []}
    for repo in selected:
        try:
            clone = sync.sync_repo(conn, settings, repo.repo)
            head = sync.head_sha(clone)
            indexed = conn.execute("SELECT indexed_sha FROM repos WHERE repo_id = ?", (repo.repo,)).fetchone()[0]
            fingerprint_key = f"config_fingerprint:{repo.repo}"
            seen = conn.execute("SELECT value FROM meta WHERE key = ?", (fingerprint_key,)).fetchone()
            if head == indexed and seen and seen[0] == fingerprint:
                summary["unchanged"].append(repo.repo)
                continue
            ctx = StageContext(settings, repos_config, integrations, head, cfg_sha, sync.tracked_files(clone))
            rows = run_stages(clone, repo, ctx)
            db.rebuild_repo(conn, repo.repo, rows, indexed_sha=head)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (fingerprint_key, fingerprint))
            for warning in ctx.warnings:
                db.record_error(conn, run_id, repo.repo, "update", warning)
            summary["rebuilt"].append(repo.repo)
        except Exception as exc:  # noqa: BLE001 - one broken repo must not stop the others; recorded, run fails
            db.record_error(conn, run_id, repo.repo, "update", f"{type(exc).__name__}: {exc}")
            summary["failed"].append({"repo": repo.repo, "error": f"{type(exc).__name__}: {exc}"})
    db.finish_run(conn, run_id, "failed" if summary["failed"] else "ok", json.dumps(summary))
    return summary


def cmd_init(settings: Settings, args) -> int:
    repos_config, _, _ = load_all(settings)
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    meta = dict(conn.execute("SELECT key, value FROM meta WHERE key NOT LIKE 'config_fingerprint:%'").fetchall())
    print(json.dumps({"db": str(settings.db_path), "meta": meta, "repos": len(repos_config.repos)}, indent=2))
    return 0


def cmd_update(settings: Settings, args) -> int:
    summary = run_update(settings, args.repo)
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


def cmd_vulns(settings: Settings, args) -> int:
    from . import vulns
    summary = vulns.run_vulns(db.open_index(settings), settings)
    print(json.dumps(summary, indent=2))
    return 1 if summary["errors"] else 0


def cmd_health(settings: Settings, args) -> int:
    print(json.dumps(sync.health(db.open_index(settings), settings), indent=2))
    return 0


def cmd_not_built(settings: Settings, args) -> int:
    print(f"`{args.command}` is not built yet (BUILD.md phases 7 and 9).", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repo_index")
    parser.add_argument("--env", default=".env", help="path to the .env file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create data/index.db and one repos row per repos.yaml entry")
    update = sub.add_parser("update", help="sync clones and rebuild changed repos")
    update.add_argument("--repo", help="only this owner/name")
    sub.add_parser("vulns", help="refresh OSV vulnerabilities and end-of-life data")
    sub.add_parser("health", help="print per-repo freshness and last runs")
    sub.add_parser("selfcheck", help="not built yet")
    sub.add_parser("serve", help="not built yet")
    args = parser.parse_args(argv)

    commands = {"init": cmd_init, "update": cmd_update, "vulns": cmd_vulns, "health": cmd_health,
                "selfcheck": cmd_not_built, "serve": cmd_not_built}
    try:
        return commands[args.command](load_settings(args.env), args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
