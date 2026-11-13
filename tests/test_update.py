import pytest

from repo_index import db, sync
from repo_index.__main__ import STAGES, main, run_update
from tests.conftest import make_remote, push_commit, run_git


@pytest.fixture
def remotes(tmp_path, monkeypatch):
    urls, works = {}, {}
    for repo_id, fixture in (("owner/payments-api", "maven"), ("owner/address-ui", "npm")):
        urls[repo_id], works[repo_id] = make_remote(tmp_path, repo_id.split("/")[1], fixture)
    monkeypatch.setattr(sync, "remote_url", urls.__getitem__)
    return works


@pytest.fixture
def conn(settings, repos_config):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    return conn


def indexed(conn):
    return dict(conn.execute("SELECT repo_id, indexed_sha FROM repos").fetchall())


def test_update_indexes_every_repo_then_second_run_is_a_no_op(settings, conn, remotes):
    assert run_update(settings) == {"rebuilt": ["owner/payments-api", "owner/address-ui"], "unchanged": [],
                                    "failed": []}
    for repo_id, work in remotes.items():
        assert indexed(conn)[repo_id] == run_git("rev-parse", "HEAD", cwd=work)
    libs = conn.execute("SELECT name, version_resolved, commit_sha FROM libraries "
                        "WHERE repo_id = 'owner/address-ui' AND name = 'lodash'").fetchone()
    assert tuple(libs) == ("lodash", "4.17.15", indexed(conn)["owner/address-ui"])
    assert all(not r["stale"] for r in sync.health(conn, settings)["repos"])

    assert run_update(settings)["unchanged"] == ["owner/payments-api", "owner/address-ui"]


def test_push_triggers_rebuild_of_only_that_repo(settings, conn, remotes):
    run_update(settings)
    new_sha = push_commit(remotes["owner/address-ui"], "requirements.txt", "flask==3.0.0\n")
    summary = run_update(settings)
    assert summary["rebuilt"] == ["owner/address-ui"] and summary["unchanged"] == ["owner/payments-api"]
    assert indexed(conn)["owner/address-ui"] == new_sha
    assert conn.execute("SELECT count(*) FROM libraries WHERE name = 'flask'").fetchone()[0] == 1


def test_repos_yaml_change_rebuilds_facts_without_a_push(settings, conn, remotes):
    run_update(settings)
    path = settings.config_dir / "repos.yaml"
    path.write_text(path.read_text().replace("  - repo: owner/address-ui", "  - repo: owner/address-ui\n    owner: web"))
    assert "owner/address-ui" in run_update(settings)["rebuilt"]
    owner = conn.execute("SELECT value FROM facts WHERE repo_id = 'owner/address-ui' AND key = 'owner'").fetchone()
    assert owner[0] == '"web"'


def test_failing_stage_keeps_previous_index_and_records_error(settings, conn, remotes, monkeypatch):
    run_update(settings)
    before = indexed(conn)["owner/address-ui"]
    push_commit(remotes["owner/address-ui"])

    def broken_stage(clone, repo, ctx):
        raise RuntimeError("stage exploded")

    monkeypatch.setattr("repo_index.__main__.STAGES", [*STAGES, broken_stage])
    summary = run_update(settings, "owner/address-ui")
    assert summary["failed"][0]["repo"] == "owner/address-ui"
    assert indexed(conn)["owner/address-ui"] == before
    assert sync.health(conn, settings)["repos"][0]["stale"] is True  # address-ui sorts first
    assert "stage exploded" in sync.health(conn, settings)["repos"][0]["last_errors"][0]["message"]


def test_unreachable_remote_fails_loudly(settings, conn, remotes, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "remote_url", lambda repo_id: (tmp_path / "nowhere.git").as_uri())
    summary = run_update(settings)
    assert len(summary["failed"]) == 2
    assert sync.health(conn, settings)["last_runs"]["update"]["status"] == "failed"


def test_cli_stubs_exit_non_zero(capsys, monkeypatch, tmp_path):
    for command in ("selfcheck", "serve"):
        assert main(["--env", str(tmp_path / "none.env"), command]) == 2
        assert "not built yet" in capsys.readouterr().err
