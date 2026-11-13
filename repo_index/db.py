"""SQLite connection, schema, meta checks and the per-repo atomic rebuild."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec

from .config import ConfigError, ReposConfig, Settings

SCHEMA_VERSION = "1"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# Tables rebuild_repo replaces wholesale. vulns and eol are owned by the `vulns` command instead.
REBUILT_TABLES = ("facts", "libraries", "symbols", "routes", "features")
REPO_COLUMNS = ("search_aid_purpose",)

Rows = dict[str, list[dict]]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Autocommit connection; callers open explicit transactions with BEGIN.

    check_same_thread=False: the MCP server runs each (synchronous, read-only) tool call in its own
    worker thread over this one connection. Writers (init/update/vulns/selfcheck) are separate, single-
    threaded CLI invocations, so this never risks a write racing another write.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def apply_schema(conn: sqlite3.Connection, embed_dim: int) -> None:
    conn.executescript(SCHEMA_PATH.read_text().replace("{EMBED_DIM}", str(int(embed_dim))))


def check_meta(conn: sqlite3.Connection, settings: Settings) -> None:
    """Refuse to run when the index was built with a different embedding model or dimension."""
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    expected = {"embed_model": settings.embed_model, "embed_dim": str(settings.embed_dim)}
    for key, want in expected.items():
        have = meta.get(key)
        if have is not None and have != want:
            raise ConfigError(
                f"index was built with {key}={have!r} but .env says {want!r}. "
                f"Rebuild: delete {settings.db_path} and run `python -m repo_index init` then `update`.")
    if meta.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ConfigError(f"index schema_version {meta['schema_version']!r} != {SCHEMA_VERSION!r}; rebuild it")


def open_index(settings: Settings, *, create: bool = False) -> sqlite3.Connection:
    """Open data/index.db, verifying meta. `create=True` (init only) makes the file and writes meta."""
    if not create and not settings.db_path.exists():
        raise ConfigError(f"{settings.db_path} does not exist; run `python -m repo_index init` first")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    if create:
        apply_schema(conn, settings.embed_dim)
    check_meta(conn, settings)
    if create:
        conn.executemany(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            [("schema_version", SCHEMA_VERSION), ("embed_model", settings.embed_model),
             ("embed_dim", str(settings.embed_dim))])
    return conn


def upsert_repos(conn: sqlite3.Connection, repos_config: ReposConfig) -> None:
    # ponytail: repos removed from repos.yaml keep their rows; add pruning if repos are retired often.
    conn.executemany("INSERT OR IGNORE INTO repos (repo_id) VALUES (?)",
                     [(r.repo,) for r in repos_config.repos])


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values())).lastrowid


def rebuild_repo(conn: sqlite3.Connection, repo_id: str, rows: Rows, *, indexed_sha: str) -> None:
    """Replace every per-repo row in one transaction and set indexed_sha last.

    `rows` maps a table in REBUILT_TABLES to row dicts (repo_id is filled in). A `features` row may
    carry an `embedding` list, stored in vec_features under the feature's id. The optional `repo` key
    holds repos columns to set (only REPO_COLUMNS). Any failure rolls back to the previous index.
    """
    unknown = set(rows) - set(REBUILT_TABLES) - {"repo"}
    if unknown:
        raise ValueError(f"rebuild_repo: unknown row tables {sorted(unknown)}")
    repo_columns = rows.get("repo") or {}
    if set(repo_columns) - set(REPO_COLUMNS):
        raise ValueError(f"rebuild_repo: only {REPO_COLUMNS} may be set on repos")

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM vec_features WHERE rowid IN (SELECT id FROM features WHERE repo_id = ?)",
                     (repo_id,))
        for table in REBUILT_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
        for table in REBUILT_TABLES:
            for row in rows.get(table, []):
                row = {**row, "repo_id": repo_id}
                embedding = row.pop("embedding", None) if table == "features" else None
                rowid = _insert(conn, table, row)
                if embedding is not None:
                    conn.execute("INSERT INTO vec_features (rowid, embedding) VALUES (?, ?)",
                                 (rowid, sqlite_vec.serialize_float32(embedding)))
        conn.execute("UPDATE repos SET search_aid_purpose = ? WHERE repo_id = ?",
                     (repo_columns.get("search_aid_purpose"), repo_id))
        updated = conn.execute("UPDATE repos SET indexed_sha = ?, indexed_at = ? WHERE repo_id = ?",
                               (indexed_sha, utc_now(), repo_id)).rowcount
        if updated != 1:
            raise ValueError(f"rebuild_repo: repo {repo_id!r} has no repos row; run init")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def start_run(conn: sqlite3.Connection, kind: str) -> int:
    return conn.execute("INSERT INTO runs (kind, started_at, status) VALUES (?, ?, 'running')",
                        (kind, utc_now())).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, detail: str = "") -> None:
    conn.execute("UPDATE runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
                 (utc_now(), status, detail, run_id))


def record_error(conn: sqlite3.Connection, run_id: int, repo_id: str | None, stage: str, message: str) -> None:
    conn.execute("INSERT INTO errors (run_id, repo_id, stage, message, at) VALUES (?, ?, ?, ?, ?)",
                 (run_id, repo_id, stage, message[:2000], utc_now()))
