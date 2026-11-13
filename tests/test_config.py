import pytest

from repo_index.config import ConfigError, load_acceptance, load_integrations, load_repos, load_settings
from tests.conftest import CONFIG_ROOT, REPOS_YAML


def write(tmp_path, text, name="repos.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_example_config_files_are_valid():
    assert [r.repo for r in load_repos(CONFIG_ROOT / "repos.yaml").repos] == ["owner/payments-api", "owner/address-ui"]
    assert set(load_integrations(CONFIG_ROOT / "integrations.yaml")) == {"jira", "slack"}
    assert len(load_acceptance(CONFIG_ROOT / "acceptance.yaml")) == 2


def test_missing_criticality_and_owner_stay_empty_not_guessed(tmp_path):
    repo = load_repos(write(tmp_path, REPOS_YAML)).get("owner/address-ui")
    assert repo.criticality is None and repo.owner is None


def test_bad_criticality_level_names_repo_and_allowed_levels(tmp_path):
    text = REPOS_YAML.replace("criticality: critical", "criticality: urgent")
    with pytest.raises(ConfigError, match=r"owner/payments-api.*'urgent'.*\['critical', 'important', 'low'\]"):
        load_repos(write(tmp_path, text))


@pytest.mark.parametrize("change, message", [
    (("owner: team-payments", "owner: team-payments\n    ownr: typo"), "unknown key.*ownr"),
    (("  - repo: owner/address-ui", "  - repo: owner/payments-api"), "duplicate repo 'owner/payments-api'"),
    (("levels: [critical]", "levels: [gold]"), "stale_critical.levels entry 'gold'"),
    (("version: 1", "version: 2"), "unsupported version"),
    (("monorepo_folders", "monorepo_dirs"), "unknown key.*monorepo_dirs"),
    (("  - repo: owner/address-ui", "  - repo: not-a-slug"), "owner/name"),
])
def test_invalid_repos_yaml_fails_whole_load(tmp_path, change, message):
    with pytest.raises(ConfigError, match=message):
        load_repos(write(tmp_path, REPOS_YAML.replace(*change)))


def test_integrations_normalize_pypi_names_and_reject_bad_regex(tmp_path):
    rules = load_integrations(write(tmp_path, "slack:\n  sdks: [{ecosystem: PyPI, name: Slack_SDK}]\n", "i.yaml"))
    assert rules["slack"].sdks == (("PyPI", "slack-sdk"),)
    with pytest.raises(ConfigError, match="bad regex"):
        load_integrations(write(tmp_path, "x:\n  hosts: ['(']\n", "i.yaml"))


def test_acceptance_rejects_unknown_expect_key(tmp_path):
    text = "- {id: a, ask: q, tool: health, expect: {repos_exact: []}}\n"
    with pytest.raises(ConfigError, match="repos_exact"):
        load_acceptance(write(tmp_path, text, "acceptance.yaml"))


def test_env_file_with_inline_comments(tmp_path, monkeypatch):
    for key in ("EMBED_DIM", "EMBED_MODEL", "MCP_TRANSPORT", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    env = write(tmp_path, "GITHUB_TOKEN=   # read-only\nEMBED_DIM=768\nEMBED_MODEL=nomic\nDATA_DIR=./d  # x\n", ".env")
    settings = load_settings(env)
    assert (settings.embed_dim, settings.embed_model, settings.github_token) == (768, "nomic", "")
    assert str(settings.data_dir) == "d"


def test_env_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="MCP_BEARER_TOKEN is required"):
        load_settings(write(tmp_path, "MCP_TRANSPORT=http\n", ".env"))
    with pytest.raises(ConfigError, match="EMBED_DIM must be an integer"):
        load_settings(write(tmp_path, "EMBED_DIM=big\n", ".env"))


def test_token_is_not_in_settings_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secretvalue")
    assert "ghp_secretvalue" not in repr(load_settings(tmp_path / "missing.env"))
