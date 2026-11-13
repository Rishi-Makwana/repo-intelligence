import dataclasses

import httpx
import pytest

from repo_index import db, selfcheck, sync, tools
from repo_index.__main__ import main, run_update
from tests.conftest import make_remote


@pytest.fixture
def remotes(tmp_path, monkeypatch):
    urls = {}
    for repo_id, fixture in (("owner/payments-api", "maven"), ("owner/address-ui", "codemap")):
        urls[repo_id], _ = make_remote(tmp_path, repo_id.split("/")[1], fixture)
    monkeypatch.setattr(sync, "remote_url", urls.__getitem__)


@pytest.fixture
def conn(settings, repos_config, remotes):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    run_update(settings)
    return conn


def test_check_expect_covers_every_supported_key():
    data = {"results": [{"repo": "a"}, {"repo": "b"}, {"repo": "c"}]}
    assert selfcheck.check_expect(data, {"repos_exactly": ["a", "b", "c"]}) is None
    assert selfcheck.check_expect(data, {"repos_exactly": ["a"]}) is not None
    assert selfcheck.check_expect(data, {"repos_include": ["b"]}) is None
    assert selfcheck.check_expect(data, {"repos_include": ["z"]}) is not None
    assert selfcheck.check_expect(data, {"repo_in_top": {"repo": "c", "k": 3}}) is None
    assert selfcheck.check_expect(data, {"repo_in_top": {"repo": "c", "k": 2}}) is not None
    assert selfcheck.check_expect({"total": 3}, {"field_equals": {"path": "total", "value": 3}}) is None
    assert selfcheck.check_expect({"total": 3}, {"field_equals": {"path": "total", "value": 4}}) is not None
    assert selfcheck.check_expect(data, {"count": {"min": 2, "max": 3}}) is None
    assert selfcheck.check_expect(data, {"count": {"min": 4}}) is not None


def test_acceptance_yaml_passes_against_matching_fixtures(settings, conn):
    summary = selfcheck.run_selfcheck(conn, settings)
    assert summary == {"total": 2, "failed": [],
                       "results": [{"id": "critical-spring2", "ok": True, "detail": None},
                                  {"id": "autocomplete-exists", "ok": True, "detail": None}]}
    assert tools.health(conn, settings)["data"]["last_runs"]["selfcheck"]["status"] == "ok"


def test_breaking_one_expectation_fails_and_restoring_it_is_green_again(settings, conn):
    path = settings.config_dir / "acceptance.yaml"
    broken = path.read_text().replace("[owner/payments-api]", "[owner/address-ui]")
    path.write_text(broken)
    summary = selfcheck.run_selfcheck(conn, settings)
    assert summary["failed"] == ["critical-spring2"]
    assert "repos_exactly" in summary["results"][0]["detail"]
    assert tools.health(conn, settings)["data"]["last_runs"]["selfcheck"]["status"] == "failed"

    restored = broken.replace("[owner/address-ui]", "[owner/payments-api]")
    path.write_text(restored)
    summary = selfcheck.run_selfcheck(conn, settings)
    assert summary["failed"] == []
    assert tools.health(conn, settings)["data"]["last_runs"]["selfcheck"]["status"] == "ok"


def test_failure_posts_to_the_webhook_and_success_does_not(settings, conn, monkeypatch):
    posted = []
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: posted.append((url, json)))
    with_webhook = dataclasses.replace(settings, alert_webhook_url="https://alerts.example.com/hook")

    selfcheck.alert(with_webhook, {"total": 2, "failed": [], "results": [{"id": "x", "ok": True, "detail": None}]})
    assert posted == []

    selfcheck.alert(with_webhook, {"total": 2, "failed": ["x"],
                                   "results": [{"id": "x", "ok": False, "detail": "boom"}]})
    assert posted[0][0] == "https://alerts.example.com/hook"
    assert posted[0][1]["failed"] == [{"id": "x", "detail": "boom"}]

    selfcheck.alert(settings, {"total": 1, "failed": ["x"], "results": [{"id": "x", "ok": False, "detail": "boom"}]})
    assert len(posted) == 1  # no ALERT_WEBHOOK_URL on `settings`: no call, no crash


def test_cli_exits_non_zero_on_failure_with_no_webhook_set(settings, conn, capsys):
    path = settings.config_dir / "acceptance.yaml"
    path.write_text(path.read_text().replace("[owner/payments-api]", "[owner/address-ui]"))
    env = settings.config_dir / "test.env"
    env.write_text(f"CONFIG_DIR={settings.config_dir}\nDATA_DIR={settings.data_dir}\nEMBED_DIM=8\n"
                   "EMBED_MODEL=fake-embed\nLLM_MODEL=fake-llm\nANTHROPIC_API_KEY=test-key\n")
    assert main(["--env", str(env), "selfcheck"]) == 1
    printed = capsys.readouterr().out
    assert '"critical-spring2"' in printed and '"failed"' in printed


def test_selfcheck_never_logs_the_webhook_url_or_bearer_token(settings, conn, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: None)
    secret = dataclasses.replace(settings, alert_webhook_url="https://alerts.example.com/s3cret-path",
                                 mcp_bearer_token="top-secret-token")
    path = settings.config_dir / "acceptance.yaml"
    path.write_text(path.read_text().replace("[owner/payments-api]", "[owner/address-ui]"))
    selfcheck.main(secret)
    out = capsys.readouterr()
    assert "s3cret-path" not in out.out + out.err and "top-secret-token" not in out.out + out.err
