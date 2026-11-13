import json

import httpx
import pytest

from repo_index import db, facts, vulns
from repo_index.manifests import parse_manifests
from tests.conftest import FIXTURES, fixture_files

LODASH_VULN = "GHSA-29mw-wpgm-hmr9"  # real advisory affecting lodash 4.17.15
LODASH_ADVISORY = {
    "id": LODASH_VULN, "summary": "ReDoS in lodash",
    "database_specific": {"severity": "MODERATE"},
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L"}],
    "references": [{"type": "ADVISORY", "url": "https://nvd.nist.gov/vuln/detail/CVE-2020-28500"}],
}


def osv_and_eol(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/querybatch":
        queries = json.loads(request.content)["queries"]
        return httpx.Response(200, json={"results": [
            {"vulns": [{"id": LODASH_VULN, "modified": "2025-01-01T00:00:00Z"}]}
            if q["package"] == {"ecosystem": "npm", "name": "lodash"} and q["version"] == "4.17.15" else {}
            for q in queries]})
    if request.url.path == f"/v1/vulns/{LODASH_VULN}":
        return httpx.Response(200, json=LODASH_ADVISORY)
    if request.url.path == "/api/v1/products/nodejs/releases/18":
        return httpx.Response(200, json={"result": {"name": "18", "isEol": True, "eolFrom": "2025-04-30"}})
    if request.url.path.startswith("/api/v1/products/"):
        return httpx.Response(404, json={"message": "not found"})
    raise AssertionError(f"unexpected request {request.url}")


def failing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="unavailable")


@pytest.fixture
def conn(settings, repos_config):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    root = FIXTURES / "npm"
    files = fixture_files(root)
    libraries, _ = parse_manifests(root, files, "sha1", repos_config.exclude_folders)
    fact_rows = facts.build_facts(root, files, libraries, repos_config.get("owner/address-ui"), repos_config, {},
                                  "sha1", "cfg", 500)
    db.rebuild_repo(conn, "owner/address-ui", {"libraries": libraries, "facts": fact_rows}, indexed_sha="sha1")
    return conn


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pinned_vulnerable_version_reports_its_advisory(conn, settings):
    summary = vulns.run_vulns(conn, settings, client(osv_and_eol))
    assert summary["errors"] == []
    assert summary["repos"]["owner/address-ui"]["osv"] == {"vulns": 1, "not_checked": 1}  # jest is unresolved
    row = conn.execute("SELECT * FROM vulns").fetchone()
    assert (row["name"], row["version"], row["vuln_id"], row["severity"], row["manifest_path"]) == \
        ("lodash", "4.17.15", LODASH_VULN, "MEDIUM", "package.json")
    assert row["url"] == "https://nvd.nist.gov/vuln/detail/CVE-2020-28500"
    eol = conn.execute("SELECT product, version, eol_date, is_eol, source_path FROM eol").fetchall()
    assert [tuple(r) for r in eol] == [("nodejs", "18", "2025-04-30", 1, "package.json")]


def test_osv_failure_records_error_and_keeps_previous_results(conn, settings):
    vulns.run_vulns(conn, settings, client(osv_and_eol))
    conn.execute("UPDATE vulns SET checked_at = '2026-01-01T00:00:00+00:00'")
    conn.execute("UPDATE eol SET checked_at = '2026-01-01T00:00:00+00:00'")

    summary = vulns.run_vulns(conn, settings, client(failing))
    assert {e["stage"] for e in summary["errors"]} == {"osv", "eol"}
    rows = conn.execute("SELECT vuln_id, checked_at FROM vulns").fetchall()
    assert [tuple(r) for r in rows] == [(LODASH_VULN, "2026-01-01T00:00:00+00:00")]
    assert conn.execute("SELECT checked_at FROM eol").fetchone()[0] == "2026-01-01T00:00:00+00:00"
    errors = conn.execute("SELECT repo_id, stage, message FROM errors").fetchall()
    assert {(r[0], r[1]) for r in errors} == {("owner/address-ui", "osv"), ("owner/address-ui", "eol")}
    assert "503" in errors[0][2]
    last = conn.execute("SELECT status FROM runs WHERE kind = 'vulns' ORDER BY id DESC").fetchone()
    assert last[0] == "failed"


@pytest.mark.parametrize("vector, score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L", 5.3),
    ("CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 6.2),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
    ("CVSS:3.0/AV:N/AC:H/PR:L/UI:R/S:C/C:L/I:L/A:N", 4.4),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
])
def test_cvss3_base_score_matches_published_scores(vector, score):
    assert vulns.cvss3_base_score(vector) == score


@pytest.mark.parametrize("advisory, severity", [
    ({"database_specific": {"severity": "MODERATE"}}, "MEDIUM"),
    ({"affected": [{"ecosystem_specific": {"severity": "HIGH"}}]}, "HIGH"),
    ({"severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}, "CRITICAL"),
    ({"severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N"}]}, "UNKNOWN"),
    ({}, "UNKNOWN"),
])
def test_severity_label_then_cvss_then_unknown(advisory, severity):
    assert vulns.severity_of(advisory) == severity


def test_eol_falls_back_from_major_minor_to_major():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/releases/18"):
            return httpx.Response(200, json={"result": {"name": "18", "isEol": False, "eolFrom": "2030-01-01"}})
        return httpx.Response(404)

    release = vulns.eol_release(client(handler), "react", "18.2", {})
    assert release["name"] == "18"
    assert seen == ["/api/v1/products/react/releases/18.2", "/api/v1/products/react/releases/18"]
