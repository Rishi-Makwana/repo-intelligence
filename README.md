# Repo Index

A small, low-maintenance index of a team's GitHub repositories (up to about ten), built to be queried
by Claude through MCP tools. Every fact comes from a file in a repo, from `repos.yaml`, or from an
external source of truth (OSV, endoflife.date), and carries its source path and commit.
`BUILD.md` is the full specification.

Built so far: config validation, the SQLite index, shallow-clone sync with stale detection,
manifest and lockfile parsing for six ecosystems, repo facts, and OSV / end-of-life checks.
Not built yet: the code map, embeddings, the MCP server and the self-check (`serve` and `selfcheck`
exit non-zero with "not built yet").

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
```

Cron, from `BUILD.md`:

```
*/10 * * * *  cd /opt/repo-index && uv run python -m repo_index update
0 6 * * *     cd /opt/repo-index && uv run python -m repo_index vulns
```

A failed command exits non-zero and records the error, shown by `health`.

## Test

```sh
uv run pytest
uv run ruff check
```

Tests run offline: git remotes are local bare repos, OSV and endoflife.date are mocked.
