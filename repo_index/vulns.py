"""Vulnerabilities from OSV and end-of-life dates from endoflife.date.

A failed lookup records an error and leaves that repo's previous rows (and their checked_at) untouched.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3

import httpx

from .config import OSV_ECOSYSTEMS, Settings
from .db import finish_run, record_error, start_run, utc_now

OSV_URL = "https://api.osv.dev"
EOL_URL = "https://endoflife.date"
OSV_BATCH_LIMIT = 1000
LABELS = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MODERATE": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW"}

# Detected runtime/framework -> endoflife.date product. Unknown products are tried as-is; a 404 means
# endoflife.date does not track it, which is not an error.
EOL_PRODUCTS = {"java": "eclipse-temurin", "nodejs": "nodejs", "python": "python", "go": "go",
                "dotnet": "dotnet", "spring-boot": "spring-boot"}
NOT_EOL_TRACKED = {"aspnetcore", "express", "fastapi", "flask", "gin", "echo", "actix-web", "axum", "nestjs"}


class OsvResponseError(Exception):
    pass


# --- severity ----------------------------------------------------------------------------------

_CVSS3 = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(value: float) -> float:
    scaled = round(value * 100000)
    return scaled / 100000 if scaled % 10000 == 0 else (math.floor(scaled / 10000) + 1) / 10


def cvss3_base_score(vector: str) -> float | None:
    """CVSS v3.0/v3.1 base score from its vector string (FIRST specification, section 7)."""
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        iss = 1 - math.prod(1 - _CVSS3["CIA"][m[k]] for k in ("C", "I", "A"))
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        exploitability = 8.22 * _CVSS3["AV"][m["AV"]] * _CVSS3["AC"][m["AC"]] * pr * _CVSS3["UI"][m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    total = (1.08 if changed else 1) * (impact + exploitability)
    return _roundup(min(total, 10))


def _band(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    return "LOW"


def severity_of(advisory: dict) -> str:
    """The advisory's own label if present, else derived from a CVSS v3 vector, else UNKNOWN."""
    specifics = [advisory.get("database_specific") or {}]
    for affected in advisory.get("affected") or []:
        specifics += [affected.get("database_specific") or {}, affected.get("ecosystem_specific") or {}]
    for specific in specifics:
        label = specific.get("severity")
        if isinstance(label, str) and label.upper() in LABELS:
            return LABELS[label.upper()]
    for entry in advisory.get("severity") or []:
        # ponytail: only CVSS v3 vectors are scored; v2/v4-only advisories are UNKNOWN until scored too.
        if entry.get("type") == "CVSS_V3":
            score = cvss3_base_score(entry.get("score", ""))
            if score is not None:
                return _band(score)
    return "UNKNOWN"


def advisory_url(advisory: dict) -> str:
    for ref in advisory.get("references") or []:
        if ref.get("type") == "ADVISORY" and ref.get("url"):
            return ref["url"]
    return f"https://osv.dev/vulnerability/{advisory['id']}"


# --- OSV ---------------------------------------------------------------------------------------

def osv_query(client: httpx.Client, packages: list[tuple[str, str, str]]) -> list[list[str]]:
    """For each (ecosystem, name, version) return the matching vulnerability ids."""
    ids: list[list[str]] = []
    for start in range(0, len(packages), OSV_BATCH_LIMIT):
        chunk = packages[start:start + OSV_BATCH_LIMIT]
        queries = [{"package": {"ecosystem": eco, "name": name}, "version": version} for eco, name, version in chunk]
        response = client.post(f"{OSV_URL}/v1/querybatch", json={"queries": queries})
        response.raise_for_status()
        results = response.json()["results"]
        if len(results) != len(chunk):
            raise OsvResponseError(f"OSV returned {len(results)} results for {len(chunk)} queries")
        # ponytail: next_page_token (a package with >1000 vulns) is not followed.
        ids += [[v["id"] for v in result.get("vulns") or []] for result in results]
    return ids


def osv_advisory(client: httpx.Client, vuln_id: str, cache: dict[str, dict]) -> dict:
    if vuln_id not in cache:
        response = client.get(f"{OSV_URL}/v1/vulns/{vuln_id}")
        response.raise_for_status()
        cache[vuln_id] = response.json()
    return cache[vuln_id]


def check_repo_vulns(conn: sqlite3.Connection, client: httpx.Client, repo_id: str, checked_at: str,
                     cache: dict[str, dict]) -> dict:
    libraries = conn.execute(
        "SELECT ecosystem, name, version_resolved, manifest_path FROM libraries WHERE repo_id = ?",
        (repo_id,)).fetchall()
    checkable = [lib for lib in libraries if lib["version_resolved"] and lib["ecosystem"] in OSV_ECOSYSTEMS]
    ids = osv_query(client, [(lib["ecosystem"], lib["name"], lib["version_resolved"]) for lib in checkable])
    rows = []
    for lib, vuln_ids in zip(checkable, ids, strict=True):
        for vuln_id in vuln_ids:
            advisory = osv_advisory(client, vuln_id, cache)
            rows.append((repo_id, lib["ecosystem"], lib["name"], lib["version_resolved"], vuln_id,
                         severity_of(advisory), advisory.get("summary"), advisory_url(advisory),
                         lib["manifest_path"], checked_at))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM vulns WHERE repo_id = ?", (repo_id,))
        conn.executemany("INSERT OR IGNORE INTO vulns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"vulns": len(rows), "not_checked": len(libraries) - len(checkable)}


# --- endoflife.date ----------------------------------------------------------------------------

def eol_release(client: httpx.Client, product: str, version: str, cache: dict) -> dict | None:
    """Look up the release cycle, trying major.minor then major. None when endoflife.date has neither."""
    parts = version.split(".")
    for cycle in dict.fromkeys([".".join(parts[:2]), parts[0]]):
        key = (product, cycle)
        if key not in cache:
            response = client.get(f"{EOL_URL}/api/v1/products/{product}/releases/{cycle}")
            if response.status_code == 404:
                cache[key] = None
            else:
                response.raise_for_status()
                cache[key] = response.json()["result"]
        if cache[key]:
            return cache[key]
    return None


def _eol_candidates(conn: sqlite3.Connection, repo_id: str) -> list[tuple[str, str, str]]:
    candidates = []
    for key, name_field, path_field in (("runtimes", "runtime", "source_path"),
                                        ("frameworks", "framework", "manifest_path")):
        row = conn.execute("SELECT value FROM facts WHERE repo_id = ? AND key = ?", (repo_id, key)).fetchone()
        for item in json.loads(row["value"]) if row else []:
            name = item[name_field]
            version = re.match(r"\d+(\.\d+)?", item.get("version") or "")
            if version and name not in NOT_EOL_TRACKED:
                candidates.append((EOL_PRODUCTS.get(name, name), version.group(0), item[path_field]))
    return list(dict.fromkeys(candidates))


def check_repo_eol(conn: sqlite3.Connection, client: httpx.Client, repo_id: str, checked_at: str,
                   cache: dict) -> int:
    rows = {}
    for product, version, source_path in _eol_candidates(conn, repo_id):
        release = eol_release(client, product, version, cache)
        if release is not None:
            rows.setdefault((product, source_path), (repo_id, product, version, release.get("eolFrom"),
                                                     int(bool(release.get("isEol"))), source_path, checked_at))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM eol WHERE repo_id = ?", (repo_id,))
        conn.executemany("INSERT INTO eol VALUES (?, ?, ?, ?, ?, ?, ?)", list(rows.values()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def run_vulns(conn: sqlite3.Connection, settings: Settings, client: httpx.Client | None = None) -> dict:
    client = client or httpx.Client(timeout=30, headers={"User-Agent": "repo-index"})
    run_id = start_run(conn, "vulns")
    checked_at = utc_now()
    summary: dict = {"repos": {}, "errors": []}
    osv_cache: dict[str, dict] = {}
    eol_cache: dict = {}
    repos = [r[0] for r in conn.execute("SELECT repo_id FROM repos WHERE indexed_sha IS NOT NULL ORDER BY repo_id")]
    for repo_id in repos:
        result = summary["repos"].setdefault(repo_id, {})
        for stage, check in (("osv", check_repo_vulns), ("eol", check_repo_eol)):
            try:
                result[stage] = check(conn, client, repo_id, checked_at, osv_cache if stage == "osv" else eol_cache)
            except (httpx.HTTPError, OsvResponseError, KeyError, ValueError) as exc:
                message = f"{type(exc).__name__}: {exc}"
                record_error(conn, run_id, repo_id, stage, message)
                summary["errors"].append({"repo": repo_id, "stage": stage, "error": message})
    finish_run(conn, run_id, "failed" if summary["errors"] else "ok", json.dumps(summary))
    return summary
