-- Applied by db.apply_schema. {EMBED_DIM} is replaced with EMBED_DIM from .env.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version, embed_model, embed_dim. Startup refuses to run if embed_model/dim differ from .env.

CREATE TABLE IF NOT EXISTS repos (
  repo_id TEXT PRIMARY KEY,          -- owner/name
  default_branch TEXT,
  indexed_sha TEXT,                  -- commit the rows below describe; set LAST in the rebuild txn
  remote_sha TEXT,                   -- latest seen via ls-remote
  remote_checked_at TEXT,            -- ISO-8601 UTC
  indexed_at TEXT,
  last_commit_at TEXT,               -- for stale-critical
  search_aid_purpose TEXT            -- LLM text; never returned as a fact
);

CREATE TABLE IF NOT EXISTS facts (    -- one value per (repo, key)
  repo_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,   -- value is JSON
  source_path TEXT NOT NULL, commit_sha TEXT NOT NULL,
  PRIMARY KEY (repo_id, key));

CREATE TABLE IF NOT EXISTS libraries (
  repo_id TEXT NOT NULL, ecosystem TEXT NOT NULL, name TEXT NOT NULL,
  version_declared TEXT, version_resolved TEXT,   -- resolved NULL = unresolved
  scope TEXT, manifest_path TEXT NOT NULL, commit_sha TEXT NOT NULL,
  PRIMARY KEY (repo_id, ecosystem, name, manifest_path));

CREATE TABLE IF NOT EXISTS vulns (
  repo_id TEXT NOT NULL, ecosystem TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
  vuln_id TEXT NOT NULL, severity TEXT NOT NULL,   -- CRITICAL|HIGH|MEDIUM|LOW|UNKNOWN
  summary TEXT, url TEXT, manifest_path TEXT NOT NULL, checked_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, ecosystem, name, version, vuln_id, manifest_path));

CREATE TABLE IF NOT EXISTS eol (
  repo_id TEXT NOT NULL, product TEXT NOT NULL, version TEXT NOT NULL,
  eol_date TEXT, is_eol INTEGER, source_path TEXT NOT NULL, checked_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, product, source_path));

CREATE TABLE IF NOT EXISTS symbols (
  repo_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,   -- class|function|method|module
  name TEXT NOT NULL, signature TEXT, line INTEGER NOT NULL, commit_sha TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_symbols ON symbols(repo_id, path);

CREATE TABLE IF NOT EXISTS routes (
  repo_id TEXT NOT NULL, method TEXT, route TEXT NOT NULL,
  handler_path TEXT, handler_name TEXT, line INTEGER,
  source TEXT NOT NULL,              -- openapi|code
  source_path TEXT NOT NULL, commit_sha TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS features (  -- what semantic_search ranks
  id INTEGER PRIMARY KEY, repo_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- purpose|openapi_op|route|readme_heading|module|ui_text
  text TEXT NOT NULL, path TEXT, line INTEGER, commit_sha TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_features USING vec0(embedding float[{EMBED_DIM}]);  -- rowid = features.id

CREATE TABLE IF NOT EXISTS cache (kind TEXT, input_hash TEXT, model TEXT, output TEXT,
  PRIMARY KEY (kind, input_hash, model));   -- kind: purpose|embedding

CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, detail TEXT);                -- kind: update|vulns|selfcheck
CREATE TABLE IF NOT EXISTS errors (run_id INTEGER, repo_id TEXT, stage TEXT, message TEXT, at TEXT);
