import json

from repo_index import facts
from repo_index.config import load_integrations
from repo_index.manifests import parse_manifests
from tests.conftest import CONFIG_ROOT, FIXTURES, fixture_files


def build(fixture, repos_config, repo_id="owner/address-ui"):
    root = FIXTURES / fixture
    files = fixture_files(root)
    libraries, _ = parse_manifests(root, files, "sha1", repos_config.exclude_folders)
    rows = facts.build_facts(root, files, libraries, repos_config.get(repo_id), repos_config,
                             load_integrations(CONFIG_ROOT / "integrations.yaml"), "sha1", "cfg1", 500)
    return {r["key"]: {**r, "value": json.loads(r["value"])} for r in rows}


def test_every_fact_has_source_and_commit(repos_config):
    for fixture in ("npm", "pypi", "go", "maven", "cargo", "nuget"):
        for row in build(fixture, repos_config).values():
            assert row["source_path"] and row["commit_sha"] in ("sha1", "cfg1"), (fixture, row)


def test_repos_yaml_facts_never_guess(repos_config):
    unknown = build("npm", repos_config, "owner/address-ui")
    assert unknown["criticality"]["value"] == "unclassified"
    assert unknown["owner"]["value"] == "unknown"
    assert "jira_projects" not in unknown
    known = build("maven", repos_config, "owner/payments-api")
    assert known["criticality"]["value"] == "critical"
    assert known["jira_projects"] == {"key": "jira_projects", "value": ["PAY"], "source_path": "repos.yaml",
                                      "commit_sha": "cfg1"}


def test_integrations_count_code_and_config_only(repos_config):
    found = build("npm", repos_config)["integrations"]["value"]
    assert set(found) == {"slack"}  # Jira appears only under docs/ and in README.md
    assert found["slack"]["sdks"] == [{"library": "@slack/web-api", "manifest_path": "package.json"}]
    assert {(m["kind"], m["text"], m["source_path"], m["line"]) for m in found["slack"]["matches"]} == {
        ("env_var", "SLACK_WEBHOOK_URL", "src/notify.js", 1), ("host", "hooks.slack.com", "src/notify.js", 1)}


def test_languages_frameworks_runtimes(repos_config):
    npm = build("npm", repos_config)
    assert npm["languages"]["value"] == [{"language": "JavaScript", "files": 2}]
    assert [f["framework"] for f in npm["frameworks"]["value"]] == ["express"]
    assert npm["runtimes"]["value"][0] == {"runtime": "nodejs", "declared": ">=18", "version": "18",
                                                "source_path": "package.json"}
    maven = build("maven", repos_config)
    spring = [f for f in maven["frameworks"]["value"] if f["library"].endswith("starter-parent")]
    assert spring == [{"framework": "spring-boot", "library": "org.springframework.boot:spring-boot-starter-parent",
                       "version": "2.7.18", "manifest_path": "pom.xml"}]
    assert maven["runtimes"]["value"][0]["version"] == "17"
    nuget = build("nuget", repos_config)
    assert nuget["runtimes"]["value"][0]["runtime"] == "dotnet"
    assert nuget["runtimes"]["value"][0]["version"] == "6.0"
    assert any(f["framework"] == "aspnetcore" for f in nuget["frameworks"]["value"])


def test_signals_and_monorepo_components(tmp_path, repos_config):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "deploy.yml").write_text("jobs:\n  ship:\n    environment: production\n")
    (tmp_path / "packages" / "web").mkdir(parents=True)
    (tmp_path / "packages" / "web" / "index.ts").write_text("export {}\n")
    files = fixture_files(tmp_path)
    libraries = [{"ecosystem": "npm", "name": "@pagerduty/pdjs", "manifest_path": "package.json"}]
    assert facts.signals(tmp_path, files, libraries, "s")["value"] == json.dumps([
        {"detail": "@pagerduty/pdjs", "signal": "paging", "source_path": "package.json"},
        {"detail": "deploy.yml", "signal": "prod_deploy_workflow", "source_path": ".github/workflows/deploy.yml"},
    ], sort_keys=True)
    assert json.loads(facts.monorepo_components(files, ("packages",), "s")["value"]) == ["packages/web"]
