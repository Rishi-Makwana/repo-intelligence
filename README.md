# Repo Index

A small, low-maintenance index of a team's GitHub repositories (up to about ten), built to be queried
by Claude through MCP tools. Every fact comes from a file in a repo, from `repos.yaml`, or from an
external source of truth (OSV, endoflife.date), and carries its source path and commit. LLM-written
text is a search aid only and only ever appears under a `search_aid` key. `BUILD.md` is the full
specification; `USAGE.md` is the rules for any agent querying the index.

Built: config validation, the SQLite index, shallow-clone sync with stale detection, manifest and
lockfile parsing for six ecosystems, repo facts, OSV / end-of-life checks, the tree-sitter code map
and route rules, the cached purpose line and embeddings, the MCP tools and server (stdio and
bearer-auth streamable HTTP), and the deterministic self-check. Not built: anything in BUILD.md's
"Not built" list (memory/answer cache, webhooks, LLM-suggested criticality, dashboards, and so on).

## Install

Needs `git`, and [`uv`](https://docs.astral.sh/uv/) with a uv-managed Python 3.11+ (it must support
`sqlite3.Connection.enable_load_extension`, which `sqlite-vec` needs).

```sh
uv sync
cp .env.example .env        # then fill in GITHUB_TOKEN (read-only)
```

Edit `repos.yaml` to list your repositories, and `integrations.yaml` for the integrations to detect.

## Run

```sh
uv run python -m repo_index init       # create data/index.db, validate config, one row per repo
uv run python -m repo_index update     # ls-remote, shallow clone/fetch, rebuild changed repos
uv run python -m repo_index update --repo owner/payments-api
uv run python -m repo_index vulns      # OSV vulnerabilities + endoflife.date, run daily
uv run python -m repo_index health     # per-repo indexed vs remote commit, stale flag, last runs
uv run python -m repo_index selfcheck  # run acceptance.yaml through the tools, alert on failure
uv run python -m repo_index serve      # the MCP server, per MCP_TRANSPORT in .env
```

Also fill in `LLM_MODEL` and `ANTHROPIC_API_KEY` (the purpose line) and, for embeddings, run
[Ollama](https://ollama.com) locally with `EMBED_MODEL` pulled (`ollama pull mxbai-embed-large`).
A repo with `exclude_from_llm: true` never reaches the LLM; it is still embedded, but only when
`OLLAMA_URL` is a local host, since embedding sends its README and code text to whatever is there.

## Self-check

`acceptance.yaml` holds a handful of questions with a known answer, run through the same tools an
agent would call:

```sh
uv run python -m repo_index selfcheck
```

It records the run, and on any failure posts to `ALERT_WEBHOOK_URL` if one is set in `.env`, or exits
non-zero (for cron mail) if not. `health` always shows whether the last self-check passed.

## Register the MCP server in Claude Code

Stdio (one user, one machine): add to the Claude Code MCP config

```json
{"mcpServers": {"repo-index": {"command": "uv", "args": ["run", "python", "-m", "repo_index", "serve"],
                               "cwd": "/opt/repo-index"}}}
```

Shared machine: set `MCP_TRANSPORT=http` in `.env`, pick an `MCP_BEARER_TOKEN`, run
`uv run python -m repo_index serve`, and register it as a remote MCP server at
`http://<MCP_HOST>:<MCP_PORT>/mcp` with that bearer token. A request without a matching token gets
401; the server refuses to start at all if the token is empty.

Then copy `.claude/agents/repo-live-search.md` into the querying project (or a shared plugin) and add
`USAGE.md`'s rules to that project's `CLAUDE.md`, so the main agent knows when to call `handoff` and
spawn the live-search sub-agent instead of trusting a thin or stale index result.

Cron, from `BUILD.md`:

```
*/10 * * * *  cd /opt/repo-index && uv run python -m repo_index update && uv run python -m repo_index selfcheck
0 6 * * *     cd /opt/repo-index && uv run python -m repo_index vulns
```

A failed command exits non-zero and records the error, shown by `health`.

## Test

```sh
uv run pytest
uv run ruff check
```

Tests run offline: git remotes are local bare repos, OSV and endoflife.date are mocked.
