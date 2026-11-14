"""The MCP tools as pure query functions: tool(conn, settings, **args) -> envelope.

Shared by server.py and selfcheck.py. Every envelope is {data, provenance, stale_repos, warnings}.
Every answer is read from the index tables; LLM-written text only ever appears under a `search_aid` key.
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import sqlite_vec

from . import llm, sync
from .config import SEVERITIES, Settings, load_repos

NOT_FOUND = "not found does not mean it doesn't exist; escalate to live search"
# ponytail: one cosine-distance cut-off for "weak" results; tune per embedding model if it warns too often or never.
WEAK_DISTANCE = 0.5
VULNS_MAX_AGE = timedelta(hours=24)
HANDOFF_BUDGET_TOKENS = 40_000
HANDOFF_MAX_REPOS = 3
HANDOFF_MAX_PATHS = 15
STOPWORDS = {"a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has",
            "have", "how", "in", "is", "it", "its", "of", "on", "or", "our", "that", "the", "this", "to", "use",
            "uses", "using", "we", "what", "when", "where", "which", "who", "why", "will", "with"}


def _envelope(conn: sqlite3.Connection, settings: Settings, data, provenance=(), warnings=()) -> dict:
    """Stale is loud: every envelope lists every stale repo in the index, with a warning each."""
    stale, stale_warnings = [], []
    for row in conn.execute("SELECT * FROM repos ORDER BY repo_id"):
        if not _is_stale(row, settings):
            continue
        repo = row["repo_id"]
        stale.append(repo)
        if row["indexed_sha"] is None:
            reason = "has not been indexed yet"
        elif row["indexed_sha"] != row["remote_sha"]:
            reason = "is behind its remote by at least one commit"
        else:
            reason = f"has not been checked against its remote since {row['remote_checked_at']} (polling may be broken)"
        stale_warnings.append(f"{repo} {reason}; confirm with the live agent")
    return {"data": data, "provenance": list(provenance), "stale_repos": stale,
            "warnings": stale_warnings + list(dict.fromkeys(warnings))}


def _is_stale(row, settings: Settings) -> bool:
    return sync.is_stale(row["indexed_sha"], row["remote_sha"], row["remote_checked_at"], settings.poll_minutes)


def _prov(repo: str, fact: str, source_path: str | None, commit_sha: str | None) -> dict:
    return {"repo": repo, "fact": fact, "source_path": source_path, "commit_sha": commit_sha}


def _repo_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["repo_id"]: row for row in conn.execute("SELECT * FROM repos ORDER BY repo_id")}


def _resolve_repo(conn: sqlite3.Connection, repo: str) -> str:
    """Callers often pass the bare repo name; map it to owner/name when exactly one indexed repo has it."""
    rows = _repo_rows(conn)
    if repo in rows:
        return repo
    matches = [r for r in rows if r.split("/")[-1].lower() == repo.split("/")[-1].lower()]
    return matches[0] if len(matches) == 1 else repo


def _facts(conn: sqlite3.Connection, repo: str) -> dict[str, sqlite3.Row]:
    return {row["key"]: row for row in conn.execute("SELECT * FROM facts WHERE repo_id = ?", (repo,))}


def _value(facts: dict, key: str, default=None):
    return json.loads(facts[key]["value"]) if key in facts else default


def _fact_prov(repo: str, facts: dict, key: str) -> dict:
    row = facts[key]
    return _prov(repo, key, row["source_path"], row["commit_sha"])


def _severity_ok(severity: str, minimum: str) -> bool:
    """UNKNOWN is reported, never hidden: it passes every minimum."""
    if minimum not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, got {minimum!r}")
    return severity == "UNKNOWN" or SEVERITIES.index(severity) <= SEVERITIES.index(minimum)


# --- shared filters ----------------------------------------------------------------------------

def _library_name_matches(wanted: str, ecosystem: str | None, row) -> bool:
    names = {row["name"].lower()}
    if row["ecosystem"] == "Maven":
        names.add(row["name"].split(":")[-1].lower())
    return wanted.lower() in names and (not ecosystem or row["ecosystem"].lower() == ecosystem.lower())


def _version_matches(wanted: str | None, version: str | None) -> bool:
    """A version ending in '.' is a prefix ("2." matches 2.7.18). Unresolved never matches a version filter."""
    if not wanted:
        return True
    if version is None:
        return False
    return version.startswith(wanted) if wanted.endswith(".") else version == wanted


def _match_library(conn, repo: str, facts: dict, library: dict, warnings: list[str]) -> list[dict]:
    """Evidence that `repo` uses `library` {name, ecosystem?, version?}: library rows by name (or Maven
    artifactId), and framework facts by framework name (spring-boot, nextjs, ...)."""
    if not isinstance(library, dict) or not library.get("name"):
        raise ValueError("library must be an object with at least a name")
    name, ecosystem, version = library["name"], library.get("ecosystem"), library.get("version")
    evidence, unresolved = [], 0
    for row in conn.execute("SELECT * FROM libraries WHERE repo_id = ?", (repo,)):
        if not _library_name_matches(name, ecosystem, row):
            continue
        if version and row["version_resolved"] is None:
            unresolved += 1
        elif _version_matches(version, row["version_resolved"]):
            evidence.append(_prov(repo, f"library:{row['name']}", row["manifest_path"], row["commit_sha"]))
    if not ecosystem:
        for item in _value(facts, "frameworks", []):
            if item["framework"].lower() == name.lower() and _version_matches(version, item.get("version")):
                evidence.append(_prov(repo, f"framework:{item['framework']}", item["manifest_path"],
                                      facts["frameworks"]["commit_sha"]))
    if unresolved and not evidence:
        warnings.append(f"{repo}: {unresolved} {name} version(s) are unresolved, so not matched against "
                        f"version {version!r}")
    return evidence


def _filter_repos(conn, settings: Settings, language=None, library=None, framework=None, integration=None,
                  criticality=None, owner=None, jira_project=None, confluence_space=None, vuln_min_severity=None,
                  eol_only=False) -> tuple[list[str], list[dict], list[str]]:
    """Repos passing every given filter, with the provenance of each match."""
    def has(repo, facts, evidence, key, test) -> bool:
        if key in facts and test(_value(facts, key)):
            evidence.append(_fact_prov(repo, facts, key))
            return True
        return False

    kept, provenance, warnings = [], [], []
    for repo in _repo_rows(conn):
        facts = _facts(conn, repo)
        evidence = []
        checks = [
            criticality is None or has(repo, facts, evidence, "criticality",
                                       lambda v: v != "unclassified" and v == criticality),
            owner is None or has(repo, facts, evidence, "owner", lambda v: v.lower() == owner.lower()),
            language is None or has(repo, facts, evidence, "languages",
                                    lambda v: any(x["language"].lower() == language.lower() for x in v)),
            framework is None or has(repo, facts, evidence, "frameworks",
                                     lambda v: any(x["framework"].lower() == framework.lower() for x in v)),
            integration is None or has(repo, facts, evidence, "integrations", lambda v: integration in v),
            jira_project is None or has(repo, facts, evidence, "jira_projects", lambda v: jira_project in v),
            confluence_space is None or has(repo, facts, evidence, "confluence_space",
                                            lambda v: v == confluence_space),
        ]
        if not all(checks):
            continue
        if library is not None:
            matched = _match_library(conn, repo, facts, library, warnings)
            if not matched:
                continue
            evidence += matched
        if vuln_min_severity is not None:
            vulns = [v for v in conn.execute("SELECT * FROM vulns WHERE repo_id = ?", (repo,))
                     if _severity_ok(v["severity"], vuln_min_severity.upper())]
            if not vulns:
                continue
            evidence += [_prov(repo, f"vuln:{v['vuln_id']}", v["manifest_path"], None) for v in vulns]
        if eol_only:
            eol = conn.execute("SELECT * FROM eol WHERE repo_id = ? AND is_eol = 1", (repo,)).fetchall()
            if not eol:
                continue
            evidence += [_prov(repo, f"eol:{e['product']}", e["source_path"], None) for e in eol]
        kept.append(repo)
        provenance += evidence
    return kept, provenance, warnings


# --- tools -------------------------------------------------------------------------------------

def search_repos(conn: sqlite3.Connection, settings: Settings, language: str | None = None,
                 library: dict | None = None, framework: str | None = None, integration: str | None = None,
                 criticality: str | None = None, owner: str | None = None, jira_project: str | None = None,
                 confluence_space: str | None = None, vuln_min_severity: str | None = None, eol_only: bool = False,
                 limit: int = 20) -> dict:
    """Repos matching every given filter. library is {name, ecosystem?, version?}; a version ending in "."
    is a prefix. Unclassified repos never match a criticality filter. search_aid is ranking help, not fact."""
    repos, provenance, warnings = _filter_repos(conn, settings, language, library, framework, integration,
                                                criticality, owner, jira_project, confluence_space,
                                                vuln_min_severity, eol_only)
    rows = _repo_rows(conn)
    results = []
    for repo in repos[:limit]:
        facts = _facts(conn, repo)
        results.append({"repo": repo, "criticality": _value(facts, "criticality", "unclassified"),
                        "owner": _value(facts, "owner", "unknown"), "indexed_sha": rows[repo]["indexed_sha"],
                        "stale": _is_stale(rows[repo], settings), "search_aid": rows[repo]["search_aid_purpose"]})
    if not results:
        warnings.append(NOT_FOUND)
    return _envelope(conn, settings, {"total": len(repos), "results": results}, provenance, warnings)


def find_library_users(conn: sqlite3.Connection, settings: Settings, name: str, ecosystem: str | None = None,
                       version: str | None = None) -> dict:
    """Every repo declaring a library (Maven also matches by artifactId), with resolved versions."""
    rows = _repo_rows(conn)
    results, provenance, distribution, unresolved = [], [], {}, 0
    libraries = conn.execute("SELECT * FROM libraries ORDER BY repo_id, manifest_path").fetchall()
    matched = [r for r in libraries if _library_name_matches(name, ecosystem, r)]
    family = []
    if not matched:
        # A family name ("opentelemetry") has no exact row; its packages are "OpenTelemetry.Api", "@opentelemetry/api".
        matched = [r for r in libraries if name.lower() in r["name"].lower()
                   and (not ecosystem or r["ecosystem"].lower() == ecosystem.lower())]
        if matched:
            family = [f"no library is named exactly {name!r}; showing libraries whose name contains it"]
    for row in matched:
        resolved = row["version_resolved"]
        if version and not _version_matches(version, resolved):
            unresolved += resolved is None
            continue
        if resolved is None:
            unresolved += 1
        else:
            distribution[resolved] = distribution.get(resolved, 0) + 1
        results.append({"repo": row["repo_id"], "library": row["name"], "ecosystem": row["ecosystem"],
                        "version_declared": row["version_declared"], "version_resolved": resolved or "unresolved",
                        "manifest_path": row["manifest_path"], "stale": _is_stale(rows[row["repo_id"]], settings)})
        provenance.append(_prov(row["repo_id"], f"library:{row['name']}", row["manifest_path"], row["commit_sha"]))
    warnings = family if results else [NOT_FOUND]
    if version and unresolved:
        warnings.append(f"{unresolved} declaration(s) have unresolved versions and were not matched against "
                        f"version {version!r}")
    data = {"repo_count": len({r["repo"] for r in results}), "version_distribution": distribution,
            "unresolved_count": unresolved, "results": results}
    return _envelope(conn, settings, data, provenance, warnings)


def semantic_search(conn: sqlite3.Connection, settings: Settings, query: str, language: str | None = None,
                    library: dict | None = None, framework: str | None = None, integration: str | None = None,
                    criticality: str | None = None, owner: str | None = None, jira_project: str | None = None,
                    confluence_space: str | None = None, vuln_min_severity: str | None = None,
                    eol_only: bool = False, limit: int = 10, *, repo: str | None = None,
                    kind: str | None = None) -> dict:
    """Rank features (routes, OpenAPI operations, README headings, screen text, folders, purpose) by
    meaning, optionally within one repo or one kind (route, ui_text, ...). rank is an ordering only, not
    a confidence. Weak or empty results mean: escalate to live search."""
    repos, _, warnings = _filter_repos(conn, settings, language, library, framework, integration, criticality,
                                       owner, jira_project, confluence_space, vuln_min_severity, eol_only)
    if repo is not None:
        repos = [r for r in repos if r == _resolve_repo(conn, repo)]
    hits = []
    if repos:
        vector = sqlite_vec.serialize_float32(llm.embed_query(conn, settings, query))
        marks = ",".join("?" for _ in repos)
        # Each repo gets an equal share, so a repo with many features (one per screen) cannot push the
        # others out of the results. Which repo has more hits therefore says nothing about where a change belongs.
        share = -(-limit // len(repos))
        # ponytail: exact scan over every feature vector; fine for ~10 repos, use vec0 KNN (MATCH) if it gets slow.
        hits = conn.execute(
            f"SELECT * FROM (SELECT f.*, vec_distance_cosine(v.embedding, ?) AS distance, ROW_NUMBER() OVER "
            f"(PARTITION BY f.repo_id ORDER BY vec_distance_cosine(v.embedding, ?), f.id) AS place "
            f"FROM features f JOIN vec_features v ON v.rowid = f.id "
            f"WHERE f.repo_id IN ({marks}) AND (? IS NULL OR f.kind = ?)) "
            f"WHERE place <= ? ORDER BY distance, id LIMIT ?",
            [vector, vector, *repos, kind, kind, share, limit]).fetchall()
    rows = _repo_rows(conn)
    results, provenance = [], []
    for rank, hit in enumerate(hits, 1):
        result = {"repo": hit["repo_id"], "kind": hit["kind"], "path": hit["path"], "line": hit["line"],
                  "text": hit["text"], "rank": rank, "stale": _is_stale(rows[hit["repo_id"]], settings)}
        if hit["kind"] == "purpose":
            result["text"] = None
            result["search_aid"] = hit["text"]
        else:
            provenance.append(_prov(hit["repo_id"], f"feature:{hit['kind']}", hit["path"], hit["commit_sha"]))
        results.append(result)
    if not hits or hits[0]["distance"] > WEAK_DISTANCE:
        warnings.append(NOT_FOUND)
    return _envelope(conn, settings, {"results": results}, provenance, warnings)


def _links(repo: str, facts: dict, settings: Settings) -> tuple[dict, list[dict]]:
    config = load_repos(settings.config_dir / "repos.yaml")
    links, provenance = {"jira": [], "confluence": None}, []
    if "jira_projects" in facts and config.jira_base_url:
        base = config.jira_base_url.rstrip("/")
        links["jira"] = [{"project": key, "url": f"{base}/browse/{key}"} for key in _value(facts, "jira_projects")]
        provenance.append(_fact_prov(repo, facts, "jira_projects"))
    if "confluence_url" in facts:
        links["confluence"] = {"space": _value(facts, "confluence_space"), "url": _value(facts, "confluence_url")}
        provenance.append(_fact_prov(repo, facts, "confluence_url"))
    elif "confluence_space" in facts and config.confluence_base_url:
        space = _value(facts, "confluence_space")
        links["confluence"] = {"space": space, "url": f"{config.confluence_base_url.rstrip('/')}/spaces/{space}"}
        provenance.append(_fact_prov(repo, facts, "confluence_space"))
    return links, provenance


def get_repo(conn: sqlite3.Connection, settings: Settings, repo: str) -> dict:
    """Everything indexed about one repo, each item with its source path and commit."""
    repo = _resolve_repo(conn, repo)
    row = _repo_rows(conn).get(repo)
    if row is None:
        return _envelope(conn, settings, None, warnings=[f"{repo} is not in the index; check repos.yaml"])
    facts = _facts(conn, repo)
    sha = row["indexed_sha"]
    libraries = [dict(r) for r in conn.execute("SELECT ecosystem, name, version_declared, version_resolved, scope, "
                                               "manifest_path, commit_sha FROM libraries WHERE repo_id = ? "
                                               "ORDER BY ecosystem, name", (repo,))]
    for lib in libraries:
        lib["version_resolved"] = lib["version_resolved"] or "unresolved"
    vulns = [dict(r) for r in conn.execute("SELECT ecosystem, name, version, vuln_id, severity, summary, url, "
                                           "manifest_path, checked_at FROM vulns WHERE repo_id = ?", (repo,))]
    eol = [dict(r) for r in conn.execute("SELECT product, version, eol_date, is_eol, source_path, checked_at "
                                         "FROM eol WHERE repo_id = ?", (repo,))]
    routes = [dict(r) for r in conn.execute("SELECT method, route, handler_path, handler_name, line, source, "
                                            "source_path, commit_sha FROM routes WHERE repo_id = ? "
                                            "ORDER BY source_path, line", (repo,))]
    links, link_prov = _links(repo, facts, settings)
    provenance = ([_fact_prov(repo, facts, key) for key in sorted(facts)]
                  + [_prov(repo, f"library:{x['name']}", x["manifest_path"], x["commit_sha"]) for x in libraries]
                  + [_prov(repo, f"vuln:{x['vuln_id']}", x["manifest_path"], sha) for x in vulns]
                  + [_prov(repo, f"eol:{x['product']}", x["source_path"], sha) for x in eol]
                  + [_prov(repo, f"route:{x['route']}", x["source_path"], x["commit_sha"]) for x in routes]
                  + link_prov)
    data = {"repo": repo, "indexed_sha": sha, "stale": _is_stale(row, settings),
            "last_commit_at": row["last_commit_at"],
            "facts": {key: _value(facts, key) for key in sorted(facts)},
            "libraries": libraries, "vulnerabilities": vulns, "end_of_life": eol, "routes": routes,
            "links": links, "search_aid": row["search_aid_purpose"]}
    return _envelope(conn, settings, data, provenance)


def code_map(conn: sqlite3.Connection, settings: Settings, repo: str, path_prefix: str | None = None,
             kind: str | None = None, limit: int = 200) -> dict:
    """Classes, functions, methods (or folder `module` rows) and routes with path and line.
    kind is class|function|method|module|route."""
    repo = _resolve_repo(conn, repo)
    prefix = path_prefix or ""
    symbols, routes = [], []
    if kind != "route":
        symbols = [dict(r) for r in conn.execute(
            "SELECT path, kind, name, signature, line, commit_sha FROM symbols WHERE repo_id = ? "
            "AND substr(path, 1, ?) = ? AND (? IS NULL OR kind = ?) ORDER BY path, line LIMIT ?",
            (repo, len(prefix), prefix, kind, kind, limit + 1))]
    if kind in (None, "route"):
        routes = [dict(r) for r in conn.execute(
            "SELECT method, route, handler_path AS path, handler_name, line, source, source_path, commit_sha "
            "FROM routes WHERE repo_id = ? AND substr(coalesce(handler_path, source_path), 1, ?) = ? "
            "ORDER BY source_path, line LIMIT ?", (repo, len(prefix), prefix, limit + 1))]
    warnings = []
    if len(symbols) > limit or len(routes) > limit:
        warnings.append(f"truncated at limit={limit}; narrow path_prefix or kind")
    symbols, routes = symbols[:limit], routes[:limit]
    if not symbols and not routes:
        warnings.append(NOT_FOUND)
    paths = dict.fromkeys([(s["path"], s["commit_sha"]) for s in symbols]
                          + [(r["path"] or r["source_path"], r["commit_sha"]) for r in routes])
    provenance = [_prov(repo, "code_map", path, sha) for path, sha in paths]
    return _envelope(conn, settings, {"symbols": symbols, "routes": routes}, provenance, warnings)


def vulnerabilities(conn: sqlite3.Connection, settings: Settings, repo: str | None = None,
                    min_severity: str | None = None, criticality: str | None = None) -> dict:
    """Known vulnerabilities at or above min_severity (default OSV_MIN_SEVERITY; UNKNOWN always shown), and
    libraries not checked because their version is unresolved."""
    minimum = (min_severity or settings.osv_min_severity).upper()
    if repo is not None:
        repo = _resolve_repo(conn, repo)
    repos = [r for r in _repo_rows(conn) if repo in (None, r)]
    if criticality is not None:
        repos, _, _ = _filter_repos(conn, settings, criticality=criticality)
        repos = [r for r in repos if repo in (None, r)]
    rows = _repo_rows(conn)
    results, not_checked, provenance = [], [], []
    for repo_id in repos:
        level = _value(_facts(conn, repo_id), "criticality", "unclassified")
        for v in conn.execute("SELECT * FROM vulns WHERE repo_id = ? ORDER BY name, vuln_id", (repo_id,)):
            if _severity_ok(v["severity"], minimum):
                results.append({"repo": repo_id, "criticality": level, "library": v["name"],
                                "ecosystem": v["ecosystem"], "version": v["version"], "vuln_id": v["vuln_id"],
                                "severity": v["severity"], "url": v["url"], "checked_at": v["checked_at"]})
                provenance.append(_prov(repo_id, f"vuln:{v['vuln_id']}", v["manifest_path"],
                                        rows[repo_id]["indexed_sha"]))
        not_checked += [{"repo": repo_id, "library": lib["name"], "reason": "unresolved version"} for lib in
                        conn.execute("SELECT name FROM libraries WHERE repo_id = ? AND version_resolved IS NULL "
                                     "ORDER BY name", (repo_id,))]
    results.sort(key=lambda r: (SEVERITIES.index(r["severity"]), r["repo"], r["library"]))
    warnings = []
    last = conn.execute("SELECT finished_at, status FROM runs WHERE kind = 'vulns' AND finished_at IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if last is None:
        warnings.append("no vulnerability check has run yet; run `python -m repo_index vulns`")
    elif datetime.now(UTC) - datetime.fromisoformat(last["finished_at"]) > VULNS_MAX_AGE:
        warnings.append(f"the last vulnerability check finished at {last['finished_at']}, over 24 hours ago")
    if last is not None and last["status"] != "ok":
        warnings.append("the last vulnerability check had errors; see `health`")
    return _envelope(conn, settings, {"results": results, "not_checked": not_checked}, provenance, warnings)


def _months_ago(now: datetime, months: int) -> datetime:
    month_index = now.year * 12 + now.month - 1 - months
    year, month = divmod(month_index, 12)
    day = min(now.day, calendar.monthrange(year, month + 1)[1])
    return now.replace(year=year, month=month + 1, day=day)


def criticality_report(conn: sqlite3.Connection, settings: Settings) -> dict:
    """How many repos are classified; unclassified repos with signals that may help a human classify them
    (signals never set a level); and repos at a stale-critical level with no commit in stale_critical.months."""
    config = load_repos(settings.config_dir / "repos.yaml")
    cutoff = _months_ago(datetime.now(UTC), config.stale_critical_months)
    rows = _repo_rows(conn)
    classified, unclassified, stale_critical, provenance = 0, [], [], []
    for repo, row in rows.items():
        facts = _facts(conn, repo)
        level = _value(facts, "criticality", "unclassified")
        if "criticality" in facts:
            provenance.append(_fact_prov(repo, facts, "criticality"))
        if level == "unclassified":
            unclassified.append({"repo": repo, "signals": _value(facts, "signals", [])})
            if "signals" in facts:
                provenance.append(_fact_prov(repo, facts, "signals"))
            continue
        classified += 1
        last = row["last_commit_at"]
        if level in config.stale_critical_levels and last and datetime.fromisoformat(last) < cutoff:
            stale_critical.append({"repo": repo, "level": level, "last_commit_at": last})
    data = {"classified": classified, "total": len(rows), "unclassified": unclassified,
            "stale_critical": stale_critical}
    return _envelope(conn, settings, data, provenance)


def health(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Per repo: indexed vs remote commit, stale flag, last errors. Global: last update, vulns, selfcheck runs."""
    return _envelope(conn, settings, sync.health(conn, settings))


def handoff(conn: sqlite3.Connection, settings: Settings, question: str, repos: list[str] | None = None,
            paths: list[str] | None = None) -> dict:
    """The brief for the repo-live-search agent. Fills repos and start_paths from semantic_search and
    code_map when not given."""
    keywords = list(dict.fromkeys(w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", question)
                                  if w.lower() not in STOPWORDS))[:8]
    warnings: list[str] = []
    hits = []
    if repos is None or paths is None:
        found = semantic_search(conn, settings, query=question)
        hits = found["data"]["results"]
        warnings += found["warnings"]
    rows = _repo_rows(conn)
    if repos is None:
        repos = list(dict.fromkeys(h["repo"] for h in hits))[:HANDOFF_MAX_REPOS] or list(rows)
    else:
        repos = [_resolve_repo(conn, r) for r in repos]
    if paths is not None:
        start_paths = [{"repo": r, "path": p, "line": None} for r in repos for p in paths]
    else:
        start_paths = [{"repo": h["repo"], "path": h["path"], "line": h["line"]} for h in hits
                       if h["path"] and h["repo"] in repos]
        for word in keywords:
            marks = ",".join("?" for _ in repos)
            start_paths += [{"repo": s["repo_id"], "path": s["path"], "line": s["line"]} for s in conn.execute(
                f"SELECT repo_id, path, line FROM symbols WHERE repo_id IN ({marks}) AND kind != 'module' "
                f"AND instr(lower(name), ?) > 0 ORDER BY repo_id, path, line LIMIT 5", [*repos, word.lower()])]
        seen = set()
        start_paths = [p for p in start_paths
                       if (p["repo"], p["path"]) not in seen and not seen.add((p["repo"], p["path"]))]
    brief_repos = []
    for repo in repos:
        row = rows.get(repo)
        if row is None:
            warnings.append(f"{repo} is not in the index; check repos.yaml")
        brief_repos.append({"repo": repo, "clone_path": str(sync.clone_path(settings, repo).resolve()),
                            "indexed_sha": row["indexed_sha"] if row else None,
                            "stale": _is_stale(row, settings) if row else True})
    data = {"question": question, "repos": brief_repos, "start_paths": start_paths[:HANDOFF_MAX_PATHS],
            "keywords": keywords, "budget_tokens": HANDOFF_BUDGET_TOKENS}
    return _envelope(conn, settings, data, warnings=warnings)


TOOLS = {fn.__name__: fn for fn in (search_repos, find_library_users, semantic_search, get_repo, code_map,
                                    vulnerabilities, criticality_report, health, handoff)}
