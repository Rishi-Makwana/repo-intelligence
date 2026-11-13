import hashlib
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo_index import llm
from repo_index.config import Settings, load_repos

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_ROOT = Path(__file__).parent.parent

# Grammars download once from the language pack's releases, then load from this cache, so later runs are offline.
os.environ.setdefault("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR", str(CONFIG_ROOT / ".cache"))

REPOS_YAML = """\
version: 1
criticality_levels: [critical, important, low]
stale_critical: {months: 6, levels: [critical]}
monorepo_folders: [packages]
exclude_folders: [node_modules, vendor]
repos:
  - repo: owner/payments-api
    criticality: critical
    owner: team-payments
    jira_projects: [PAY]
  - repo: owner/address-ui
"""


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    """Tests never see the developer's git config (signing, global ignores, credential helpers)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


def run_git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fixture_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def make_remote(tmp_path: Path, name: str, fixture: str | None = None) -> tuple[str, Path]:
    """A bare repo seeded from a fixture folder. Returns (file:// url, working copy used to push)."""
    work = tmp_path / "work" / name
    if fixture:
        shutil.copytree(FIXTURES / fixture, work)
    else:
        work.mkdir(parents=True)
        (work / "README.md").write_text(f"# {name}\n")
    bare = tmp_path / "remotes" / f"{name}.git"
    run_git("init", "-q", "-b", "main", cwd=work)
    run_git("add", "-A", cwd=work)
    run_git("commit", "-q", "-m", "seed", cwd=work)
    run_git("clone", "-q", "--bare", str(work), str(bare))
    run_git("remote", "add", "origin", str(bare), cwd=work)
    return bare.as_uri(), work


def push_commit(work: Path, filename: str = "CHANGE.txt", text: str = "change\n") -> str:
    (work / filename).write_text(text)
    run_git("add", "-A", cwd=work)
    run_git("commit", "-q", "-m", "change", cwd=work)
    run_git("push", "-q", "origin", "main", cwd=work)
    return run_git("rev-parse", "HEAD", cwd=work)


@pytest.fixture
def settings(tmp_path) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "repos.yaml").write_text(REPOS_YAML)
    shutil.copy(CONFIG_ROOT / "integrations.yaml", config_dir / "integrations.yaml")
    shutil.copy(CONFIG_ROOT / "acceptance.yaml", config_dir / "acceptance.yaml")
    return Settings(config_dir=config_dir, data_dir=tmp_path / "data", embed_dim=8, embed_model="fake-embed",
                    llm_model="fake-llm", anthropic_api_key="test-key")


@pytest.fixture
def repos_config(settings):
    return load_repos(settings.config_dir / "repos.yaml")


def fake_vector(text: str, dim: int) -> list[float]:
    """Bag of words hashed into `dim` buckets: texts sharing words point the same way."""
    vector = [0.01] * dim
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dim] += 1.0
    return vector


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    """No test reaches Anthropic or Ollama. `providers` counts calls; set `.purpose` to change the LLM reply."""
    fake = SimpleNamespace(calls=Counter(),
                           purpose="Serves the team's example service. A second sentence that must be dropped.")

    def complete(settings, system, prompt):
        fake.calls["llm"] += 1
        return fake.purpose

    def embed_texts(settings, texts):
        fake.calls["embed"] += 1
        return [fake_vector(t, settings.embed_dim) for t in texts]

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(llm, "embed_texts", embed_texts)
    return fake
