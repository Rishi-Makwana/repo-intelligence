"""Load and validate .env, repos.yaml, integrations.yaml and acceptance.yaml.

Any validation problem raises ConfigError and fails the whole command; nothing is skipped silently.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
OSV_ECOSYSTEMS = {"npm", "PyPI", "Go", "Maven", "crates.io", "NuGet"}
EXPECT_KEYS = {"repos_exactly", "repos_include", "repo_in_top", "field_equals", "count"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    github_token: str = field(default="", repr=False)
    llm_provider: str = "anthropic"
    llm_model: str = ""
    anthropic_api_key: str = field(default="", repr=False)
    embed_provider: str = "ollama"
    embed_model: str = "mxbai-embed-large"
    embed_dim: int = 1024
    embed_query_prefix: str = ""
    ollama_url: str = "http://localhost:11434"
    config_dir: Path = Path(".")
    data_dir: Path = Path("./data")
    poll_minutes: int = 10
    osv_min_severity: str = "HIGH"
    max_file_kb: int = 500
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8080
    mcp_bearer_token: str = field(default="", repr=False)
    alert_webhook_url: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def clones_dir(self) -> Path:
        return self.data_dir / "clones"


@dataclass(frozen=True)
class RepoConfig:
    repo: str
    criticality: str | None = None
    owner: str | None = None
    jira_projects: tuple[str, ...] = ()
    confluence_space: str | None = None
    confluence_url: str | None = None
    exclude_from_llm: bool = False


@dataclass(frozen=True)
class ReposConfig:
    criticality_levels: tuple[str, ...]
    stale_critical_months: int
    stale_critical_levels: tuple[str, ...]
    jira_base_url: str | None
    confluence_base_url: str | None
    monorepo_folders: tuple[str, ...]
    exclude_folders: tuple[str, ...]
    repos: tuple[RepoConfig, ...]

    def get(self, repo_id: str) -> RepoConfig:
        for repo in self.repos:
            if repo.repo == repo_id:
                return repo
        raise ConfigError(f"repo {repo_id!r} is not listed in repos.yaml")


@dataclass(frozen=True)
class Integration:
    name: str
    sdks: tuple[tuple[str, str], ...]   # (ecosystem, package name)
    hosts: tuple[re.Pattern, ...]
    env_vars: tuple[re.Pattern, ...]


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key) or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f".env: {key} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f".env: {key} must be positive, got {value}")
    return value


def load_settings(env_path: Path | str = ".env") -> Settings:
    """Read .env, then let real environment variables override it."""
    env = _parse_env_file(Path(env_path))
    env.update({k: v for k, v in os.environ.items() if k in _ENV_KEYS})

    def get(key: str, default: str = "") -> str:
        return env.get(key) or default

    osv_min = get("OSV_MIN_SEVERITY", "HIGH").upper()
    if osv_min not in SEVERITIES:
        raise ConfigError(f".env: OSV_MIN_SEVERITY must be one of {SEVERITIES}, got {osv_min!r}")
    transport = get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "http"):
        raise ConfigError(f".env: MCP_TRANSPORT must be stdio or http, got {transport!r}")
    if transport == "http" and not get("MCP_BEARER_TOKEN"):
        raise ConfigError(".env: MCP_BEARER_TOKEN is required when MCP_TRANSPORT=http")

    return Settings(
        github_token=get("GITHUB_TOKEN"),
        llm_provider=get("LLM_PROVIDER", "anthropic"),
        llm_model=get("LLM_MODEL"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        embed_provider=get("EMBED_PROVIDER", "ollama"),
        embed_model=get("EMBED_MODEL", "mxbai-embed-large"),
        embed_dim=_int(env, "EMBED_DIM", 1024),
        embed_query_prefix=get("EMBED_QUERY_PREFIX"),
        ollama_url=get("OLLAMA_URL", "http://localhost:11434"),
        config_dir=Path(get("CONFIG_DIR", ".")),
        data_dir=Path(get("DATA_DIR", "./data")),
        poll_minutes=_int(env, "POLL_MINUTES", 10),
        osv_min_severity=osv_min,
        max_file_kb=_int(env, "MAX_FILE_KB", 500),
        mcp_transport=transport,
        mcp_host=get("MCP_HOST", "127.0.0.1"),
        mcp_port=_int(env, "MCP_PORT", 8080),
        mcp_bearer_token=get("MCP_BEARER_TOKEN"),
        alert_webhook_url=get("ALERT_WEBHOOK_URL"),
    )


_ENV_KEYS = {
    "GITHUB_TOKEN", "LLM_PROVIDER", "LLM_MODEL", "ANTHROPIC_API_KEY", "EMBED_PROVIDER", "EMBED_MODEL",
    "EMBED_DIM", "EMBED_QUERY_PREFIX", "OLLAMA_URL", "CONFIG_DIR", "DATA_DIR", "POLL_MINUTES",
    "OSV_MIN_SEVERITY", "MAX_FILE_KB", "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_BEARER_TOKEN",
    "ALERT_WEBHOOK_URL",
}


def _read_yaml(path: Path):
    if not path.exists():
        raise ConfigError(f"{path}: file not found")
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None


def _check_keys(where: str, data: dict, allowed: set[str], required: set[str] = frozenset()) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    missing = sorted(required - set(data))
    if missing:
        raise ConfigError(f"{where}: missing required key(s) {missing}")


def _str_list(where: str, value) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(value)


_TOP_KEYS = {"version", "criticality_levels", "stale_critical", "jira_base_url", "confluence_base_url",
             "monorepo_folders", "exclude_folders", "repos"}
_REPO_KEYS = {"repo", "criticality", "owner", "jira_projects", "confluence_space", "confluence_url",
              "exclude_from_llm"}
_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def load_repos(path: Path | str) -> ReposConfig:
    path = Path(path)
    data = _read_yaml(path)
    name = path.name
    _check_keys(name, data, _TOP_KEYS, {"version", "criticality_levels", "repos"})
    if data["version"] != 1:
        raise ConfigError(f"{name}: unsupported version {data['version']!r}; expected 1")

    levels = _str_list(f"{name}: criticality_levels", data["criticality_levels"])
    if not levels:
        raise ConfigError(f"{name}: criticality_levels must not be empty")
    if len(set(levels)) != len(levels):
        raise ConfigError(f"{name}: criticality_levels has duplicates: {list(levels)}")

    stale = data.get("stale_critical") or {}
    _check_keys(f"{name}: stale_critical", stale, {"months", "levels"})
    months = stale.get("months", 6)
    if not isinstance(months, int) or isinstance(months, bool) or months <= 0:
        raise ConfigError(f"{name}: stale_critical.months must be a positive integer, got {months!r}")
    stale_levels = _str_list(f"{name}: stale_critical.levels", stale.get("levels"))
    for level in stale_levels:
        if level not in levels:
            raise ConfigError(
                f"{name}: stale_critical.levels entry {level!r} is not in criticality_levels {list(levels)}")

    if not isinstance(data["repos"], list):
        raise ConfigError(f"{name}: repos must be a list")
    repos: list[RepoConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(data["repos"]):
        where = f"{name}: repos[{i}]"
        _check_keys(where, entry, _REPO_KEYS, {"repo"})
        repo_id = entry["repo"]
        if not isinstance(repo_id, str) or not _REPO_ID.match(repo_id):
            raise ConfigError(f"{where}: repo must look like owner/name, got {repo_id!r}")
        if repo_id in seen:
            raise ConfigError(f"{name}: duplicate repo {repo_id!r}")
        seen.add(repo_id)
        criticality = entry.get("criticality")
        if criticality is not None and criticality not in levels:
            raise ConfigError(
                f"{where} ({repo_id}): criticality {criticality!r} is not in criticality_levels {list(levels)}")
        exclude = entry.get("exclude_from_llm", False)
        if not isinstance(exclude, bool):
            raise ConfigError(f"{where} ({repo_id}): exclude_from_llm must be true or false")
        repos.append(RepoConfig(
            repo=repo_id,
            criticality=criticality,
            owner=entry.get("owner"),
            jira_projects=_str_list(f"{where}: jira_projects", entry.get("jira_projects")),
            confluence_space=entry.get("confluence_space"),
            confluence_url=entry.get("confluence_url"),
            exclude_from_llm=exclude,
        ))

    return ReposConfig(
        criticality_levels=levels,
        stale_critical_months=months,
        stale_critical_levels=stale_levels,
        jira_base_url=data.get("jira_base_url"),
        confluence_base_url=data.get("confluence_base_url"),
        monorepo_folders=_str_list(f"{name}: monorepo_folders", data.get("monorepo_folders")),
        exclude_folders=_str_list(f"{name}: exclude_folders", data.get("exclude_folders")),
        repos=tuple(repos),
    )


def _patterns(where: str, value) -> tuple[re.Pattern, ...]:
    compiled = []
    for pattern in _str_list(where, value):
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigError(f"{where}: bad regex {pattern!r}: {exc}") from None
    return tuple(compiled)


def load_integrations(path: Path | str) -> dict[str, Integration]:
    path = Path(path)
    data = _read_yaml(path) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: expected a mapping of integration name to rules")
    integrations = {}
    for name, rules in data.items():
        where = f"{path.name}: {name}"
        _check_keys(where, rules, {"sdks", "hosts", "env_vars"})
        sdks = []
        for sdk in rules.get("sdks") or []:
            _check_keys(f"{where}.sdks", sdk, {"ecosystem", "name"}, {"ecosystem", "name"})
            if sdk["ecosystem"] not in OSV_ECOSYSTEMS:
                raise ConfigError(
                    f"{where}.sdks: ecosystem {sdk['ecosystem']!r} must be one of {sorted(OSV_ECOSYSTEMS)}")
            package = sdk["name"]
            if sdk["ecosystem"] == "PyPI":  # library rows store PEP 503 names, so slack_sdk == slack-sdk
                package = re.sub(r"[-_.]+", "-", package).lower()
            sdks.append((sdk["ecosystem"], package))
        integrations[name] = Integration(
            name=name,
            sdks=tuple(sdks),
            hosts=_patterns(f"{where}.hosts", rules.get("hosts")),
            env_vars=_patterns(f"{where}.env_vars", rules.get("env_vars")),
        )
    return integrations


def load_acceptance(path: Path | str) -> list[dict]:
    path = Path(path)
    data = _read_yaml(path) or []
    if not isinstance(data, list):
        raise ConfigError(f"{path.name}: expected a list of acceptance questions")
    ids: set[str] = set()
    for i, item in enumerate(data):
        where = f"{path.name}[{i}]"
        _check_keys(where, item, {"id", "ask", "tool", "args", "expect"}, {"id", "ask", "tool", "expect"})
        if item["id"] in ids:
            raise ConfigError(f"{path.name}: duplicate id {item['id']!r}")
        ids.add(item["id"])
        _check_keys(f"{where} ({item['id']}).expect", item["expect"], EXPECT_KEYS)
        if not item["expect"]:
            raise ConfigError(f"{where} ({item['id']}): expect must name at least one check")
    return data


def load_all(settings: Settings) -> tuple[ReposConfig, dict[str, Integration], list[dict]]:
    config_dir = settings.config_dir
    return (
        load_repos(config_dir / "repos.yaml"),
        load_integrations(config_dir / "integrations.yaml"),
        load_acceptance(config_dir / "acceptance.yaml"),
    )
