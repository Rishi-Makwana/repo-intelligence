# Repo Index: Build Guide

A small, low-maintenance index of a team's GitHub repositories, queried by Claude through MCP tools. The goal is that it is easy to maintain and gives consistently correct answers. It is sized for one team with up to about ten repositories.

This file is the specification the code is built from.

## The core rule

Facts come only from code and files. The LLM only helps find things; it is never the answer.

Every answer is read from a file (a manifest, `repos.yaml`, an OpenAPI spec, source code) or from an external source of truth (OSV, endoflife.date). LLM-written text is used to rank search results and only ever appears in a field named `search_aid`.

## How a question is answered

| Layer | Answers | Built by |
|---|---|---|
| 1. Index | Which repos; libraries and versions; framework; integrations; criticality; owner; Jira/Confluence links; vulnerabilities and end of life | Parsers over manifests and `repos.yaml`; OSV; endoflife.date |
| 2. Code map and feature index | What is inside each repo and where: folders, classes, functions, routes and their handler files | tree-sitter, OpenAPI files, README headings. No LLM. |
| 3. Live agent | What the code actually does | A sub-agent reading the shallow clones, limited to paths from layers 1 and 2 |

Layers 1 and 2 shortlist. Layer 3 confirms.

## Correctness rules

1. Every answer shows its source path and commit.
2. Stale is loud. If a repo's indexed commit is behind its remote, every tool says so.
3. Anything that drives a design or reuse decision is confirmed by the live agent.
4. "Not found" does not mean "doesn't exist". Empty or weak results carry a warning to escalate to live search.
5. Unknown is unknown. Missing criticality is `unclassified`, missing owner is `unknown`, an unpinned version is `unresolved`. Nothing is guessed.

## 0. Rules for the builder

1. Build in phase order. Do not start a phase until the previous phase's check passes.
2. Build only what is listed. No extra tools, layers, dashboards or config.
3. The core rule is a test: no tool may return LLM-written text outside a `search_aid` field.
4. Every row that holds a fact stores `source_path` and `commit_sha`.
5. Mark a deliberate shortcut with a `ponytail:` comment naming the limit and the upgrade path.
6. Where this guide says **verify**, check current documentation before coding. Do not guess APIs.
7. After each phase, report what now works, the check output, and anything skipped.

## 1. Stack

| Need | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | `tomllib` is in the standard library |
| Package manager | `uv` | Use a uv-managed Python: some system Pythons cannot load SQLite extensions (**verify** `sqlite3.Connection.enable_load_extension` exists) |
| Storage | SQLite + `sqlite-vec` | One file, `data/index.db` |
| MCP server | `mcp` Python SDK | stdio for local use, streamable HTTP for a shared machine (**verify** current API) |
| Code parsing | `tree-sitter` + a language-pack package | **verify** package name and supported languages |
| LLM | Anthropic SDK | Purpose line only |
| Embeddings | Ollama `/api/embed` | **verify** endpoint and request shape |
| Vulnerabilities | OSV API (`/v1/querybatch`, then `/v1/vulns/{id}`) | **verify** |
| End of life | endoflife.date API | **verify** current API version |
| Host tools | `git`, `rg` | The live agent uses these on the clones |

Python dependencies: `anthropic`, `httpx`, `pyyaml`, `mcp`, `sqlite-vec`, `tree-sitter`, the tree-sitter language pack. Dev: `pytest`, `ruff`.

The LLM and embedding providers sit behind two small functions so tests run with fakes and no network.

## 2. Key design decisions

| Decision | Why |
|---|---|
| Everything is parsed from local shallow clones. No GitHub GraphQL. | The live agent needs clones anyway. One source of truth for every layer. |
| Change detection with `git ls-remote`. | One cheap call per repo; no API rate limits. |
| A changed repo is fully rebuilt in one transaction: delete all its rows, insert new ones, set `indexed_sha` last. | No mixed-commit data. `ponytail:` full per-repo rebuild; add per-stage gating only if one repo takes over a minute. |
| LLM and embedding calls are cached by input hash. | An unchanged README makes no new call. Cost and drift stay near zero. |
| The self-check is deterministic. Acceptance questions call tools and compare structured output. | No LLM judging the LLM. |
| Unpinned versions are not guessed. Resolve from lockfiles, otherwise mark `unresolved`. | Correctness rule 5. |

## 3. File formats

Config files are read from `CONFIG_DIR` (default: the working directory), so one copy of the tool can serve several teams.

### 3.1 `repos.yaml` (human-owned, changed by pull request)

```yaml
version: 1
criticality_levels: [critical, important, low]   # ordered, highest first
stale_critical:
  months: 6
  levels: [critical]
jira_base_url: https://example.atlassian.net
confluence_base_url: https://example.atlassian.net/wiki
monorepo_folders: [packages, apps, services, modules]
exclude_folders: [node_modules, vendor, build, dist, target, generated]
repos:
  - repo: owner/payments-api           # required, unique
    criticality: critical              # optional; omitted = unclassified
    owner: team-payments               # optional; omitted = unknown
    jira_projects: [PAY]               # optional
    confluence_space: PAYDOCS          # optional
    confluence_url: https://example.atlassian.net/wiki/spaces/PAYDOCS/overview   # optional
    exclude_from_llm: false            # true = no README or code text sent to the LLM
```

Validation fails the whole update and never skips silently: unknown keys, a duplicate `repo`, a `criticality` not in `criticality_levels`, a `stale_critical.levels` entry not in `criticality_levels`.

### 3.2 `integrations.yaml`

```yaml
jira:
  sdks: [{ecosystem: npm, name: jira.js}, {ecosystem: PyPI, name: jira}]
  hosts: ['[a-z0-9-]+\.atlassian\.net']
  env_vars: ['\bJIRA_[A-Z_]+\b']
slack:
  sdks: [{ecosystem: npm, name: "@slack/web-api"}, {ecosystem: PyPI, name: slack_sdk}]
  hosts: ['hooks\.slack\.com']
  env_vars: ['\bSLACK_[A-Z_]+\b']
```

Matches count only in code and config files. Matches under test or docs folders, and in `*.md`, are ignored. Each match records `source_path` and line.

### 3.3 `acceptance.yaml`

```yaml
- id: critical-spring2
  ask: "Which critical repos use Spring Boot 2?"
  tool: search_repos
  args: {criticality: critical, library: {name: spring-boot, version: "2."}}
  expect: {repos_exactly: [owner/payments-api]}
- id: autocomplete-exists
  ask: "Do we have something like address autocomplete?"
  tool: semantic_search
  args: {query: address autocomplete}
  expect: {repo_in_top: {repo: owner/address-ui, k: 3}}
```

Supported `expect` keys: `repos_exactly`, `repos_include`, `repo_in_top {repo, k}`, `field_equals {path, value}`, `count {min, max}`.

### 3.4 `.env`

```
GITHUB_TOKEN=              # read-only; used by git over HTTPS
LLM_PROVIDER=anthropic
LLM_MODEL=
ANTHROPIC_API_KEY=
EMBED_PROVIDER=ollama
EMBED_MODEL=mxbai-embed-large
EMBED_DIM=1024
EMBED_QUERY_PREFIX=        # empty if the model needs none
OLLAMA_URL=http://localhost:11434
CONFIG_DIR=.
DATA_DIR=./data            # index.db + clones/
POLL_MINUTES=10
OSV_MIN_SEVERITY=HIGH
MAX_FILE_KB=500
MCP_TRANSPORT=stdio        # stdio | http
MCP_HOST=127.0.0.1
MCP_PORT=8080
MCP_BEARER_TOKEN=          # required when MCP_TRANSPORT=http
ALERT_WEBHOOK_URL=         # optional; else non-zero exit + cron mail
```

## 4. Database schema (`schema.sql`)

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version, embed_model, embed_dim. Startup refuses to run if embed_model/dim differ from .env.

CREATE TABLE repos (
  repo_id TEXT PRIMARY KEY,          -- owner/name
  default_branch TEXT,
  indexed_sha TEXT,                  -- commit the rows below describe; set LAST in the rebuild txn
  remote_sha TEXT,                   -- latest seen via ls-remote
  remote_checked_at TEXT,            -- ISO-8601 UTC
  indexed_at TEXT,
  last_commit_at TEXT,               -- for stale-critical
  search_aid_purpose TEXT            -- LLM text; never returned as a fact
);

CREATE TABLE facts (                 -- one value per (repo, key)
  repo_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,   -- value is JSON
  source_path TEXT NOT NULL, commit_sha TEXT NOT NULL,
  PRIMARY KEY (repo_id, key));
-- keys: criticality, owner, jira_projects, confluence_space, confluence_url, languages,
--       frameworks, integrations, monorepo_components, signals (paging/on-call, prod deploy workflows)
-- repos.yaml facts use source_path='repos.yaml' and commit_sha = the config repo's own HEAD.

CREATE TABLE libraries (
  repo_id TEXT NOT NULL, ecosystem TEXT NOT NULL, name TEXT NOT NULL,
  version_declared TEXT, version_resolved TEXT,   -- resolved NULL = unresolved
  scope TEXT, manifest_path TEXT NOT NULL, commit_sha TEXT NOT NULL,
  PRIMARY KEY (repo_id, ecosystem, name, manifest_path));

CREATE TABLE vulns (
  repo_id TEXT NOT NULL, ecosystem TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
  vuln_id TEXT NOT NULL, severity TEXT NOT NULL,   -- CRITICAL|HIGH|MEDIUM|LOW|UNKNOWN
  summary TEXT, url TEXT, manifest_path TEXT NOT NULL, checked_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, ecosystem, name, version, vuln_id, manifest_path));

CREATE TABLE eol (
  repo_id TEXT NOT NULL, product TEXT NOT NULL, version TEXT NOT NULL,
  eol_date TEXT, is_eol INTEGER, source_path TEXT NOT NULL, checked_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, product, source_path));

CREATE TABLE symbols (
  repo_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,   -- class|function|method|module
  name TEXT NOT NULL, signature TEXT, line INTEGER NOT NULL, commit_sha TEXT NOT NULL);
CREATE INDEX idx_symbols ON symbols(repo_id, path);

CREATE TABLE routes (
  repo_id TEXT NOT NULL, method TEXT, route TEXT NOT NULL,
  handler_path TEXT, handler_name TEXT, line INTEGER,
  source TEXT NOT NULL,              -- openapi|code
  source_path TEXT NOT NULL, commit_sha TEXT NOT NULL);

CREATE TABLE features (              -- what semantic_search ranks
  id INTEGER PRIMARY KEY, repo_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- purpose|openapi_op|route|readme_heading|module
  text TEXT NOT NULL, path TEXT, line INTEGER, commit_sha TEXT NOT NULL);
CREATE VIRTUAL TABLE vec_features USING vec0(embedding float[1024]);   -- rowid = features.id; dim from EMBED_DIM

CREATE TABLE cache (kind TEXT, input_hash TEXT, model TEXT, output TEXT,
  PRIMARY KEY (kind, input_hash, model));   -- kind: purpose|embedding

CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, detail TEXT);                -- kind: update|vulns|selfcheck
CREATE TABLE errors (run_id INTEGER, repo_id TEXT, stage TEXT, message TEXT, at TEXT);
```

Stale rule, used by every tool: a repo is stale if `remote_sha != indexed_sha`, or `remote_checked_at` is older than `2 x POLL_MINUTES` (polling itself is broken).

## 5. Module layout

```
repo_index/
  __main__.py     CLI: init | update [--repo X] | vulns | selfcheck | health | serve
  config.py       load + validate .env, repos.yaml, integrations.yaml, acceptance.yaml
  db.py           connect, load sqlite-vec, apply schema, rebuild_repo(conn, repo_id, rows) txn
  sync.py         ls-remote, shallow clone/fetch, last_commit_at, stale computation
  manifests.py    parsers + lockfile resolution, per ecosystem
  facts.py        languages, frameworks, integrations, signals, repos.yaml facts
  codemap.py      tree-sitter symbols, route rules, OpenAPI parsing, feature items
  llm.py          purpose line + embeddings, both cached by input hash
  vulns.py        OSV + endoflife.date
  tools.py        pure query functions (conn, args) -> envelope; shared by server and selfcheck
  server.py       MCP tool registration + bearer auth for HTTP
  selfcheck.py    run acceptance.yaml against tools.py, alert on failure
schema.sql
repos.yaml  integrations.yaml  acceptance.yaml  .env.example
tests/fixtures/<one tiny repo per ecosystem>/   tests/test_*.py
.claude/agents/repo-live-search.md
USAGE.md          rules for any Claude that queries the index (section 9)
```

## 6. Build phases

Each phase ends with a check that must pass.

### Phase 1: Skeleton

`config.py` with full validation; `db.py` applies the schema and loads `sqlite-vec`; `init` creates `data/index.db`, writes `meta`, inserts one `repos` row per `repos.yaml` entry.

- **Check:** `uv run python -m repo_index init` succeeds. A `repos.yaml` with a bad criticality level fails with a clear message. `meta.embed_dim` matches `.env`.

### Phase 2: Sync

`sync.py`: `git ls-remote` gives `remote_sha` and `remote_checked_at`. A changed or missing clone gets `git clone --depth 1` (or fetch + hard reset) into `DATA_DIR/clones/owner__name`. Record `last_commit_at` from the clone. The token is passed through a git credential or the HTTPS URL and is never logged.

- **Check:** `update` clones every repo; a second run makes no clone changes; `health` shows `remote_sha == indexed_sha` is false until Phase 3 sets `indexed_sha`.

### Phase 3: Facts and libraries

- `manifests.py`: npm (`package.json` + `package-lock.json`), PyPI (`pyproject.toml`, `requirements*.txt`, `uv.lock` / `poetry.lock`), Go (`go.mod`), Maven (`pom.xml`, resolving `${props}` from the same file; parent- or BOM-managed versions stay `unresolved`), Cargo (`Cargo.toml` + `Cargo.lock`), NuGet (`*.csproj`, `packages.lock.json`). Walk the monorepo folders. Skip the excluded folders.
- `facts.py`: languages by file extension over `git ls-files`; frameworks from a small library-to-framework map (`ponytail:` declared-library only; add import confirmation if false positives appear); integrations from `integrations.yaml`; signals; all `repos.yaml` facts.
- `update` now calls `db.rebuild_repo` per changed repo and sets `indexed_sha` last.
- **Check:** tests per fixture repo assert exact library rows, including one `unresolved` version. Killing the process mid-rebuild leaves the repo's old rows and old `indexed_sha`.

### Phase 4: Code map and feature items

`codemap.py`: tree-sitter extracts classes, functions and methods (name, signature, line). Unsupported languages get `module` rows per folder only. Route rules are one small table per framework (Spring `@GetMapping`, Express `app.get`, FastAPI `@router.get`, ASP.NET `[HttpGet]`, Go `HandleFunc`/gin, Next.js file-based routes). OpenAPI files (`openapi*.yaml/json`, `swagger*`) are parsed as `routes(source='openapi')`. Feature items: OpenAPI operations, routes, README headings (h1 to h3), top-level module folders. Respect `MAX_FILE_KB`.

- **Check:** fixture tests assert known symbols with correct lines and one route mapped to its handler file.

### Phase 5: Purpose and embeddings

`llm.py`: purpose line from the README (first ~8 KB) + library names + top folders. The output must be one sentence, stored only in `repos.search_aid_purpose` and as a `features(kind='purpose')` row. Skip the LLM for `exclude_from_llm` repos. Embed every `features.text` (with `EMBED_QUERY_PREFIX` on queries only) into `vec_features`. Cache both by input hash.

- **Check:** a second `update` with no changes makes zero LLM and embedding calls (assert with a counter). Changing `EMBED_MODEL` without a rebuild makes startup refuse with a clear message.

### Phase 6: Vulnerabilities and end of life

`vulns.py`: OSV batch query for every resolved version (ecosystems `npm`, `PyPI`, `Go`, `Maven`, `crates.io`, `NuGet`), then fetch details per vulnerability id. Severity is the advisory's own label if present, else derived from CVSS, else `UNKNOWN`. `UNKNOWN` is reported, never hidden. Unresolved versions are listed as "not checked". endoflife.date for detected runtimes and frameworks (Node, Python, Java, Spring Boot, .NET, others found). The `vulns` command runs daily.

- **Check:** a fixture pinned to a known-vulnerable version produces that vulnerability id. A test with the OSV client mocked to fail records an error and keeps the previous results with their old `checked_at`.

### Phase 7: MCP tools and server

Implement section 7 in `tools.py` and register in `server.py`. The HTTP transport requires `Authorization: Bearer $MCP_BEARER_TOKEN` on every request and binds to `MCP_HOST`.

- **Check:** `tests/test_tools.py` covers every tool against a fixture database, including a stale repo (asserts `stale: true` and a warning) and the "no LLM text as fact" rule (asserts LLM text only ever appears in `search_aid`). An HTTP request without the token gets 401.

### Phase 8: Live agent and usage rules

Write `.claude/agents/repo-live-search.md` and `USAGE.md` exactly as in sections 8 and 9.

- **Check:** in a Claude session with the MCP server registered, ask one code-level question; the main agent calls `handoff`, spawns the sub-agent, and the answer cites `file:line` and commit.

### Phase 9: Polling, self-check, alerts

`selfcheck.py` runs `acceptance.yaml` through `tools.py`, records a `runs` row, and on any failure POSTs to `ALERT_WEBHOOK_URL` (or exits non-zero for cron mail). Cron:

```
*/10 * * * *  cd /opt/repo-index && uv run python -m repo_index update && uv run python -m repo_index selfcheck
0 6 * * *     cd /opt/repo-index && uv run python -m repo_index vulns
```

- **Check:** break one expectation in `acceptance.yaml`; the next run alerts. Restore it; the next run is green and `health` shows the last self-check passed.

## 7. MCP tool contracts

Every tool returns the same envelope:

```json
{
  "data": {},
  "provenance": [{"repo": "owner/x", "fact": "library:spring-boot", "source_path": "pom.xml", "commit_sha": "a1b2c3"}],
  "stale_repos": ["owner/y"],
  "warnings": ["owner/y is behind its remote by at least one commit; confirm with the live agent"]
}
```

| Tool | Args | `data` |
|---|---|---|
| `search_repos` | `language?`, `library? {name, ecosystem?, version?}` (a version ending in `.` is a prefix), `framework?`, `integration?`, `criticality?`, `owner?`, `jira_project?`, `confluence_space?`, `vuln_min_severity?`, `eol_only?`, `limit=20` | `{total, results: [{repo, criticality, owner, indexed_sha, stale, search_aid}]}`. Unclassified repos never match a `criticality` filter. |
| `find_library_users` | `name`, `ecosystem?`, `version?` | `{repo_count, version_distribution: {ver: n}, unresolved_count, results: [{repo, version_declared, version_resolved, manifest_path}]}` |
| `semantic_search` | `query`, the `search_repos` filters, `limit=10` | `{results: [{repo, kind, path, line, text, rank, stale}]}`. `rank` is an ordering only, not a confidence. Empty or weak results add the warning "not found does not mean it doesn't exist; escalate to live search". |
| `get_repo` | `repo` | All facts, libraries, vulnerabilities, end of life, routes, Jira/Confluence links built from the base URLs, `search_aid`, with provenance per item. |
| `code_map` | `repo`, `path_prefix?`, `kind?`, `limit=200` | `{symbols: [...], routes: [...]}` with `path`, `line`. |
| `vulnerabilities` | `repo?`, `min_severity=OSV_MIN_SEVERITY`, `criticality?` | `{results: [{repo, criticality, library, version, vuln_id, severity, url, checked_at}], not_checked: [{repo, library, reason: "unresolved version"}]}` |
| `criticality_report` | none | `{classified, total, unclassified: [{repo, signals}], stale_critical: [{repo, level, last_commit_at}]}`. Signals help a human; they never set a level. |
| `health` | none | Per repo: `indexed_sha`, `remote_sha`, `stale`, `remote_checked_at`, last errors. Global: last update, vulns and selfcheck runs with status. |
| `handoff` | `question`, `repos?`, `paths?` | The live-agent brief: `{question, repos: [{repo, clone_path, indexed_sha, stale}], start_paths, keywords, budget_tokens}`. Fills `repos` and `start_paths` from `semantic_search` + `code_map` when not given. |

## 8. Live agent definition (`.claude/agents/repo-live-search.md`)

```markdown
---
name: repo-live-search
description: Reads the real code in the team's local repo clones to confirm or find something the
  repo index could not answer. Use when given a handoff brief from the repo-index `handoff` tool.
tools: Read, Grep, Glob, Bash
---

You receive a handoff brief: question, repos with clone_path and indexed_sha, start_paths, keywords,
budget_tokens.

1. Search ONLY the listed clone paths. Start at start_paths, then widen with `rg` using the keywords
   and close synonyms.
2. If a repo is marked stale, say so in the answer; the clone may be behind the remote.
3. Stop when the question is answered or the token budget is reached.
4. Never paste whole files. Quote at most a few lines per finding.

Return exactly:
- **Answer:** one or two sentences.
- **Evidence:** `repo` · `path:line` · commit, and what each shows.
- **Searched but not found:** paths and keywords tried (required if the answer is "not found").
- **Confidence limits:** anything you could not check.
```

## 9. Usage rules for querying agents (`USAGE.md`)

Add to the team's `CLAUDE.md` or load as a skill:

1. Start with the index tools; they are cheap.
2. Always show the `provenance` source and commit in your answer.
3. Call `handoff` and spawn `repo-live-search` when any of these is true: results are empty or weak; the question is about code-level detail; a result repo is stale; the answer will drive a design or reuse decision.
4. Never present `search_aid` text as fact. It only explains why a result ranked.
5. Report `unclassified`, `unknown`, `unresolved` and `UNKNOWN` severity as they are. Never fill them in.

## 10. Tests and definition of done

Unit tests, one file per module, fixtures in `tests/fixtures/`: manifest parsing per ecosystem, including lockfile resolution and `unresolved`; `repos.yaml` validation failures; atomic rebuild; the stale rule (both conditions); route-to-handler mapping; cache hit = zero calls; envelope shape and the `search_aid` rule; OSV failure keeping old results; bearer auth.

Done when all of these hold:

1. Every phase check passes, and `uv run pytest` and `ruff check` are green.
2. All acceptance questions pass via `selfcheck` against the real repos.
3. One deliberate push to a real repo appears in the index within `POLL_MINUTES` + one run, and is flagged stale until then.
4. Turning off the network for one cycle produces an alert, not silent stale answers.
5. `health` shows every repo current, the last vulnerability check under 24 hours old, and the last self-check green.
6. The setup's named owner has run `update`, `selfcheck` and `health` themselves once.

**Not built (do not add):** memory or answer cache, webhooks, LLM-suggested criticality, LLM-written module descriptions, dashboards, per-user visibility, repo-to-repo links, a recent-changes layer.
