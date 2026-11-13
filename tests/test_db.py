import dataclasses
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from repo_index import db
from repo_index.config import ConfigError
from tests.conftest import CONFIG_ROOT as PROJECT_ROOT


def lib(name, version="1.0.0"):
    return {"ecosystem": "npm", "name": name, "version_declared": version, "version_resolved": version,
            "scope": "runtime", "manifest_path": "package.json", "commit_sha": "old"}


@pytest.fixture
def conn(settings, repos_config):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    db.rebuild_repo(conn, "owner/payments-api", {
        "libraries": [lib("express")],
        "facts": [{"key": "owner", "value": '"team-payments"', "source_path": "repos.yaml", "commit_sha": "c"}],
        "features": [{"kind": "module", "text": "billing", "path": "billing", "commit_sha": "old",
                      "embedding": [0.1] * 8}],
    }, indexed_sha="old")
    return conn


def snapshot(conn, repo="owner/payments-api"):
    return {
        "sha": conn.execute("SELECT indexed_sha FROM repos WHERE repo_id = ?", (repo,)).fetchone()[0],
        "libs": [r[0] for r in conn.execute("SELECT name FROM libraries WHERE repo_id = ?", (repo,))],
        "vectors": conn.execute("SELECT count(*) FROM vec_features").fetchone()[0],
    }


def test_init_writes_meta_matching_env(settings, conn):
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["embed_dim"] == str(settings.embed_dim) == "8"
    assert meta["embed_model"] == "fake-embed"
    assert [r[0] for r in conn.execute("SELECT repo_id FROM repos ORDER BY repo_id")] == \
        ["owner/address-ui", "owner/payments-api"]


def test_open_refuses_when_embedding_model_or_dim_changed(settings, conn):
    with pytest.raises(ConfigError, match="embed_model='fake-embed'.*'other-model'"):
        db.open_index(dataclasses.replace(settings, embed_model="other-model"))
    with pytest.raises(ConfigError, match="embed_dim='8'.*'16'"):
        db.open_index(dataclasses.replace(settings, embed_dim=16))


def test_open_without_init_fails_clearly(settings):
    with pytest.raises(ConfigError, match="run `python -m repo_index init`"):
        db.open_index(settings)


def test_rebuild_replaces_rows_and_embeddings(conn):
    db.rebuild_repo(conn, "owner/payments-api", {"libraries": [lib("koa")]}, indexed_sha="new")
    assert snapshot(conn) == {"sha": "new", "libs": ["koa"], "vectors": 0}


def test_failed_rebuild_keeps_old_rows_and_sha(conn):
    bad = {**lib("broken"), "manifest_path": None}  # NOT NULL violation after the deletes ran
    with pytest.raises(sqlite3.IntegrityError):
        db.rebuild_repo(conn, "owner/payments-api", {"libraries": [lib("koa"), bad]}, indexed_sha="new")
    assert snapshot(conn) == {"sha": "old", "libs": ["express"], "vectors": 1}


def test_rebuild_rejects_unknown_tables(conn):
    with pytest.raises(ValueError, match="vulns"):
        db.rebuild_repo(conn, "owner/payments-api", {"vulns": []}, indexed_sha="new")


def test_killed_process_mid_rebuild_keeps_old_rows_and_sha(settings, conn):
    """A real hard kill (os._exit) after the deletes and some inserts, inside the transaction."""
    script = textwrap.dedent(f"""
        import os
        from repo_index import db
        conn = db.connect({str(settings.db_path)!r})
        inserts = []
        def die_on_second_insert(sql):
            if sql.startswith("INSERT INTO libraries"):
                inserts.append(sql)
                if len(inserts) == 2:
                    os._exit(9)
        conn.set_trace_callback(die_on_second_insert)
        rows = [dict(ecosystem="npm", name=n, version_declared="1", version_resolved="1", scope="runtime",
                     manifest_path="package.json", commit_sha="new") for n in ("a", "b", "c")]
        db.rebuild_repo(conn, "owner/payments-api", {{"libraries": rows}}, indexed_sha="new")
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=PROJECT_ROOT,
                            check=False)
    assert result.returncode == 9, result.stderr
    fresh = db.connect(settings.db_path)
    assert snapshot(fresh) == {"sha": "old", "libs": ["express"], "vectors": 1}
