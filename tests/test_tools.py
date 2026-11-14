import json

import pytest

from repo_index import db, sync, tools
from repo_index.__main__ import run_update
from repo_index.config import load_repos
from tests.conftest import make_remote

MARKER = "MARKER_f7c3a9_do_not_leak"


@pytest.fixture
def remotes(tmp_path, monkeypatch):
    urls = {}
    for repo_id, fixture in (("owner/payments-api", "maven"), ("owner/address-ui", "codemap")):
        urls[repo_id], _ = make_remote(tmp_path, repo_id.split("/")[1], fixture)
    monkeypatch.setattr(sync, "remote_url", urls.__getitem__)


@pytest.fixture
def conn(settings, repos_config, remotes, providers):
    providers.purpose = f"Serves address lookups, {MARKER}."
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    run_update(settings)
    # owner/address-ui pushed since the last index: stale by commit, not by poll staleness.
    conn.execute("UPDATE repos SET remote_sha = 'deadbeef' WHERE repo_id = 'owner/address-ui'")
    conn.execute("INSERT INTO vulns VALUES ('owner/payments-api', 'Maven', 'org.apache.logging.log4j:log4j-core', "
                "'2.14.1', 'GHSA-test-log4j', 'CRITICAL', 'RCE', 'https://example/GHSA-test-log4j', "
                "'pom.xml', '2026-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO eol VALUES ('owner/payments-api', 'eclipse-temurin', '17', '2029-09-30', 0, "
                "'pom.xml', '2026-01-01T00:00:00+00:00')")
    run_id = db.start_run(conn, "vulns")
    db.finish_run(conn, run_id, "ok", "{}")
    return conn


def envelope_keys(obj) -> set[str]:
    return set(obj) if isinstance(obj, dict) else ({"data", "provenance", "stale_repos", "warnings"})


def test_every_tool_returns_the_envelope_shape(settings, conn):
    for name, fn in tools.TOOLS.items():
        args = {"search_repos": {}, "find_library_users": {"name": "lodash"}, "semantic_search": {"query": "x"},
                "get_repo": {"repo": "owner/payments-api"}, "code_map": {"repo": "owner/payments-api"},
                "vulnerabilities": {}, "criticality_report": {}, "health": {}, "handoff": {"question": "x?"}}[name]
        result = fn(conn, settings, **args)
        assert set(result) == {"data", "provenance", "stale_repos", "warnings"}, name
        assert isinstance(result["provenance"], list) and isinstance(result["warnings"], list)


def assert_marker_only_under_search_aid(obj, path=()):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and MARKER in value:
                assert key == "search_aid", f"LLM text leaked outside search_aid at {path + (key,)}: {value!r}"
            assert_marker_only_under_search_aid(value, path + (key,))
    elif isinstance(obj, list):
        for item in obj:
            assert_marker_only_under_search_aid(item, path)


def test_llm_text_appears_only_under_search_aid_everywhere(settings, conn):
    calls = [(tools.search_repos, {}), (tools.semantic_search, {"query": "address lookup"}),
             (tools.get_repo, {"repo": "owner/address-ui"}), (tools.handoff, {"question": "address lookup?"})]
    saw_search_aid = False
    for fn, args in calls:
        result = fn(conn, settings, **args)
        assert_marker_only_under_search_aid(result)
        saw_search_aid = saw_search_aid or MARKER in json.dumps(result)
    assert saw_search_aid, "the planted marker never showed up anywhere, so this test proved nothing"


def test_semantic_search_purpose_hit_puts_llm_text_under_search_aid_not_text(settings, conn):
    result = tools.semantic_search(conn, settings, query="Serves address lookups", limit=20)
    purpose_hits = [r for r in result["data"]["results"] if r["kind"] == "purpose"]
    assert purpose_hits and purpose_hits[0]["text"] is None
    assert MARKER in purpose_hits[0]["search_aid"]


def test_search_repos_filters_by_criticality_library_and_version_prefix(settings, conn):
    result = tools.search_repos(conn, settings, criticality="critical",
                                library={"name": "spring-boot", "version": "2."})
    assert [r["repo"] for r in result["data"]["results"]] == ["owner/payments-api"]
    assert result["data"]["results"][0]["criticality"] == "critical"
    assert tools.search_repos(conn, settings, criticality="critical", library={"name": "spring-boot",
                                                                               "version": "3."})["data"]["total"] == 0


def test_unclassified_repos_never_match_a_criticality_filter(settings, conn):
    conn.execute("DELETE FROM facts WHERE repo_id = 'owner/address-ui' AND key = 'criticality'")
    conn.execute("INSERT INTO facts VALUES ('owner/address-ui', 'criticality', '\"unclassified\"', "
                "'repos.yaml', 'x')")
    for level in load_repos(settings.config_dir / "repos.yaml").criticality_levels:
        result = tools.search_repos(conn, settings, criticality=level)
        assert "owner/address-ui" not in [r["repo"] for r in result["data"]["results"]]


def test_find_library_users_reports_version_distribution_and_unresolved(settings, conn):
    result = tools.find_library_users(conn, settings, name="spring-boot-starter-web")
    assert result["data"]["unresolved_count"] == 1 and result["data"]["version_distribution"] == {}
    assert result["data"]["results"][0]["version_resolved"] == "unresolved"
    web = tools.find_library_users(conn, settings, name="next")
    assert web["data"]["version_distribution"] == {"14.2.3": 1}


def test_find_library_users_falls_back_to_a_family_name_with_a_warning(settings, conn):
    result = tools.find_library_users(conn, settings, name="spring-boot")
    assert "org.springframework.boot:spring-boot-starter-web" in [r["library"] for r in result["data"]["results"]]
    assert any("named exactly 'spring-boot'" in w for w in result["warnings"])
    assert not any("named exactly" in w for w in tools.find_library_users(conn, settings, name="next")["warnings"])


def test_a_bare_repo_name_resolves_to_the_indexed_repo(settings, conn):
    entry = tools.handoff(conn, settings, question="anything", repos=["Address-UI"], paths=[])["data"]["repos"][0]
    assert entry["repo"] == "owner/address-ui" and entry["clone_path"].endswith("owner__address-ui")
    assert entry["indexed_sha"] is not None
    assert tools.get_repo(conn, settings, repo="payments-api")["data"]["repo"] == "owner/payments-api"
    assert tools.code_map(conn, settings, repo="address-ui", kind="route")["data"]["routes"]


def test_a_stale_repo_is_flagged_true_with_a_warning_in_every_envelope(settings, conn):
    result = tools.search_repos(conn, settings)
    assert "owner/address-ui" in result["stale_repos"]
    assert any("owner/address-ui" in w for w in result["warnings"])
    stale_result = next(r for r in result["data"]["results"] if r["repo"] == "owner/address-ui")
    assert stale_result["stale"] is True
    fresh_result = next(r for r in result["data"]["results"] if r["repo"] == "owner/payments-api")
    assert fresh_result["stale"] is False


def test_empty_semantic_search_warns_to_escalate_to_live_search(settings, conn):
    result = tools.semantic_search(conn, settings, query="something nobody wrote about", criticality="low")
    assert result["data"]["results"] == []
    assert tools.NOT_FOUND in result["warnings"]


def test_get_repo_includes_facts_libraries_vulns_eol_routes_and_links_with_provenance(settings, conn):
    result = tools.get_repo(conn, settings, repo="owner/payments-api")
    data = result["data"]
    assert data["facts"]["criticality"] == "critical"
    assert any(v["vuln_id"] == "GHSA-test-log4j" for v in data["vulnerabilities"])
    assert any(e["product"] == "eclipse-temurin" for e in data["end_of_life"])
    assert data["links"]["jira"] == [{"project": "PAY", "url": "https://example.atlassian.net/browse/PAY"}]
    assert all(p.get("source_path") for p in result["provenance"])
    missing = tools.get_repo(conn, settings, repo="owner/not-tracked")
    assert missing["data"] is None and any("not in the index" in w for w in missing["warnings"])


def test_code_map_returns_symbols_and_routes_filtered_by_prefix_and_kind(settings, conn):
    result = tools.code_map(conn, settings, repo="owner/address-ui", path_prefix="src/Api/Controllers")
    assert result["data"]["symbols"][0]["name"] == "OrdersController"
    routes = tools.code_map(conn, settings, repo="owner/address-ui", kind="route")["data"]
    assert routes["symbols"] == [] and routes["routes"]


def test_vulnerabilities_filters_by_severity_and_lists_unresolved_as_not_checked(settings, conn):
    result = tools.vulnerabilities(conn, settings, min_severity="CRITICAL")
    assert [v["vuln_id"] for v in result["data"]["results"]] == ["GHSA-test-log4j"]
    assert {"repo": "owner/payments-api", "library": "org.springframework.boot:spring-boot-starter-web",
            "reason": "unresolved version"} in result["data"]["not_checked"]


def test_vulnerabilities_rejects_an_unknown_severity():
    with pytest.raises(ValueError, match="CRITICAL.*HIGH"):
        tools._severity_ok("HIGH", "not-a-level")


def test_criticality_report_counts_and_flags_a_stale_critical_repo(settings, conn):
    old = "2020-01-01T00:00:00+00:00"
    conn.execute("UPDATE repos SET last_commit_at = ? WHERE repo_id = 'owner/payments-api'", (old,))
    result = tools.criticality_report(conn, settings)
    assert result["data"]["classified"] == 1 and result["data"]["total"] == 2
    assert result["data"]["unclassified"] == [{"repo": "owner/address-ui", "signals": []}]
    assert result["data"]["stale_critical"] == [{"repo": "owner/payments-api", "level": "critical",
                                                 "last_commit_at": old}]


def test_health_reports_last_runs_and_wraps_syncs_health(settings, conn):
    result = tools.health(conn, settings)
    assert {r["repo"] for r in result["data"]["repos"]} == {"owner/payments-api", "owner/address-ui"}
    assert result["data"]["last_runs"]["vulns"]["status"] == "ok"


def test_handoff_fills_repos_and_start_paths_from_search_when_not_given(settings, conn):
    result = tools.handoff(conn, settings, question="Where does address autocomplete live?")
    repo_ids = {r["repo"] for r in result["data"]["repos"]}
    assert "owner/address-ui" in repo_ids
    entry = next(r for r in result["data"]["repos"] if r["repo"] == "owner/address-ui")
    assert entry["clone_path"].endswith("owner__address-ui") and entry["stale"] is True
    assert result["data"]["start_paths"]
    assert "address" in result["data"]["keywords"]


def test_handoff_respects_explicit_repos_and_paths(settings, conn):
    result = tools.handoff(conn, settings, question="anything", repos=["owner/payments-api"], paths=["pom.xml"])
    assert [r["repo"] for r in result["data"]["repos"]] == ["owner/payments-api"]
    assert result["data"]["start_paths"] == [{"repo": "owner/payments-api", "path": "pom.xml", "line": None}]
