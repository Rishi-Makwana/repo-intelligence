"""Per-repo facts: languages, frameworks, runtimes, integrations, signals, monorepo components and
the repos.yaml facts. Every fact row carries source_path and commit_sha."""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath

from .config import Integration, RepoConfig, ReposConfig
from .manifests import is_excluded

LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".go": "Go", ".rs": "Rust", ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".m": "Objective-C", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".sh": "Shell", ".ps1": "PowerShell", ".sql": "SQL",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".vue": "Vue", ".svelte": "Svelte",
    ".tf": "HCL", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
}
CONFIG_EXTENSIONS = {".yaml", ".yml", ".json", ".toml", ".properties", ".xml", ".ini", ".cfg", ".conf",
                     ".env", ".config", ".csproj", ".gradle"}
CONFIG_NAMES = {"Dockerfile", "docker-compose.yml", "Makefile", ".env.example"}
TEST_OR_DOC_FOLDERS = {"test", "tests", "__tests__", "spec", "specs", "testdata", "docs", "doc"}

# (ecosystem, library name or prefix ending in ':' / '.', framework). Declared libraries only.
# ponytail: declared-library match only; add import confirmation if false positives appear.
FRAMEWORKS = [
    ("Maven", "org.springframework.boot:", "spring-boot"),
    ("npm", "express", "express"), ("npm", "next", "nextjs"), ("npm", "react", "react"),
    ("npm", "@angular/core", "angular"), ("npm", "vue", "vue"), ("npm", "@nestjs/core", "nestjs"),
    ("PyPI", "fastapi", "fastapi"), ("PyPI", "django", "django"), ("PyPI", "flask", "flask"),
    ("Go", "github.com/gin-gonic/gin", "gin"), ("Go", "github.com/labstack/echo/v4", "echo"),
    ("crates.io", "actix-web", "actix-web"), ("crates.io", "axum", "axum"),
    ("NuGet", "Microsoft.AspNetCore.", "aspnetcore"),
]

PAGING = re.compile(r"pagerduty|opsgenie|victorops|incident\.io", re.IGNORECASE)
PROD_DEPLOY = re.compile(r"^\s*(environment|name):.*\bprod(uction)?\b", re.IGNORECASE | re.MULTILINE)
MAX_MATCHES_PER_INTEGRATION = 20


def fact(key: str, value, source_path: str, commit_sha: str) -> dict:
    return {"key": key, "value": json.dumps(value, sort_keys=True), "source_path": source_path,
            "commit_sha": commit_sha}


def _version_of(library: dict) -> str | None:
    return library["version_resolved"] or library["version_declared"]


def languages(files: list[str], commit_sha: str) -> dict | None:
    counts = Counter(LANGUAGES[PurePosixPath(f).suffix.lower()] for f in files
                     if PurePosixPath(f).suffix.lower() in LANGUAGES)
    if not counts:
        return None
    value = [{"language": lang, "files": n} for lang, n in counts.most_common()]
    return fact("languages", value, "git ls-files", commit_sha)


def frameworks(libraries: list[dict], files: list[str], root: Path, commit_sha: str) -> dict | None:
    found = []
    for lib in libraries:
        for ecosystem, pattern, framework in FRAMEWORKS:
            prefix = pattern.endswith((":", "."))
            if lib["ecosystem"] == ecosystem and (lib["name"].startswith(pattern) if prefix else lib["name"] == pattern):
                found.append({"framework": framework, "library": lib["name"], "version": _version_of(lib),
                              "manifest_path": lib["manifest_path"]})
    for path in files:
        if path.endswith(".csproj") and 'Sdk="Microsoft.NET.Sdk.Web"' in _read(root, path, 512):
            found.append({"framework": "aspnetcore", "library": "Microsoft.NET.Sdk.Web", "version": None,
                          "manifest_path": path})
    if not found:
        return None
    return fact("frameworks", found, found[0]["manifest_path"], commit_sha)


def _first_version(text: str | None) -> str | None:
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return match.group(0) if match else None


def runtimes(root: Path, files: list[str], commit_sha: str) -> dict | None:
    """Runtime versions as declared; feeds the end-of-life check.

    ponytail: takes the first number in a constraint (">=3.11" -> 3.11), i.e. the oldest version allowed.
    """
    found = []

    def add(runtime, declared, path):
        version = _first_version(declared)
        if version:
            found.append({"runtime": runtime, "declared": declared, "version": version, "source_path": path})

    for path in files:
        name = PurePosixPath(path).name
        try:
            if name == "package.json":
                add("nodejs", (json.loads(_read(root, path)).get("engines") or {}).get("node"), path)
            elif name in (".nvmrc", ".node-version"):
                add("nodejs", _read(root, path).strip(), path)
            elif name == ".python-version":
                add("python", _read(root, path).strip(), path)
            elif name == "pyproject.toml":
                add("python", (tomllib.loads(_read(root, path)).get("project") or {}).get("requires-python"), path)
            elif name == "go.mod":
                match = re.search(r"^go\s+(\S+)", _read(root, path), re.MULTILINE)
                add("go", match and match.group(1), path)
            elif name == "pom.xml":
                text = _read(root, path)
                match = re.search(r"<(java\.version|maven\.compiler\.(?:source|release))>([^<]+)<", text)
                add("java", match and match.group(2), path)
            elif name.endswith(".csproj"):
                match = re.search(r"<TargetFrameworks?>\s*net(\d+\.\d+)", _read(root, path))
                add("dotnet", match and match.group(1), path)
        except (OSError, ValueError, tomllib.TOMLDecodeError, ET.ParseError):
            continue
    if not found:
        return None
    return fact("runtimes", found, found[0]["source_path"], commit_sha)


def _read(root: Path, path: str, limit: int | None = None) -> str:
    with open(root / path, encoding="utf-8", errors="replace") as handle:
        return handle.read(limit) if limit else handle.read()


def _is_code_or_config(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix.lower() == ".md" or any(part.lower() in TEST_OR_DOC_FOLDERS for part in p.parts[:-1]):
        return False
    return p.suffix.lower() in LANGUAGES or p.suffix.lower() in CONFIG_EXTENSIONS or p.name in CONFIG_NAMES


def integrations(root: Path, files: list[str], libraries: list[dict], rules: dict[str, Integration],
                 commit_sha: str, max_file_kb: int) -> dict | None:
    by_name: dict[str, dict] = {}
    for integration in rules.values():
        sdks = [{"library": lib["name"], "manifest_path": lib["manifest_path"]} for lib in libraries
                if (lib["ecosystem"], lib["name"]) in integration.sdks]
        if sdks:
            by_name[integration.name] = {"sdks": sdks, "matches": []}

    scanned = [f for f in files if _is_code_or_config(f) and (root / f).is_file()
               and (root / f).stat().st_size <= max_file_kb * 1024]
    for path in scanned:
        text = _read(root, path)
        if "\0" in text:
            continue
        for integration in rules.values():
            entry = by_name.get(integration.name, {"sdks": [], "matches": []})
            for kind, patterns in (("host", integration.hosts), ("env_var", integration.env_vars)):
                for pattern in patterns:
                    for match in pattern.finditer(text):
                        if len(entry["matches"]) >= MAX_MATCHES_PER_INTEGRATION:
                            break  # ponytail: capped per integration; the live agent finds the rest
                        line = text.count("\n", 0, match.start()) + 1
                        entry["matches"].append({"kind": kind, "text": match.group(0),
                                                 "source_path": path, "line": line})
            if entry["sdks"] or entry["matches"]:
                by_name[integration.name] = entry
    if not by_name:
        return None
    first = next(iter(by_name.values()))
    source = first["sdks"][0]["manifest_path"] if first["sdks"] else first["matches"][0]["source_path"]
    return fact("integrations", by_name, source, commit_sha)


def signals(root: Path, files: list[str], libraries: list[dict], commit_sha: str) -> dict | None:
    """Hints for a human classifying criticality. They never set a level."""
    found = [{"signal": "paging", "detail": lib["name"], "source_path": lib["manifest_path"]}
             for lib in libraries if PAGING.search(lib["name"])]
    for path in files:
        if (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
                and PROD_DEPLOY.search(_read(root, path))):
            found.append({"signal": "prod_deploy_workflow", "detail": PurePosixPath(path).name,
                          "source_path": path})
    if not found:
        return None
    return fact("signals", found, found[0]["source_path"], commit_sha)


def monorepo_components(files: list[str], folders, commit_sha: str) -> dict | None:
    components = sorted({"/".join(PurePosixPath(f).parts[:2]) for f in files
                         if len(PurePosixPath(f).parts) > 2 and PurePosixPath(f).parts[0] in folders})
    if not components:
        return None
    return fact("monorepo_components", components, "git ls-files", commit_sha)


def config_facts(repo: RepoConfig, config_sha: str) -> list[dict]:
    """repos.yaml facts. Missing criticality is `unclassified` and missing owner is `unknown`; never guessed."""
    rows = [fact("criticality", repo.criticality or "unclassified", "repos.yaml", config_sha),
            fact("owner", repo.owner or "unknown", "repos.yaml", config_sha)]
    if repo.jira_projects:
        rows.append(fact("jira_projects", list(repo.jira_projects), "repos.yaml", config_sha))
    if repo.confluence_space:
        rows.append(fact("confluence_space", repo.confluence_space, "repos.yaml", config_sha))
    if repo.confluence_url:
        rows.append(fact("confluence_url", repo.confluence_url, "repos.yaml", config_sha))
    return rows


def build_facts(root: Path, files: list[str], libraries: list[dict], repo: RepoConfig, repos_config: ReposConfig,
                rules: dict[str, Integration], commit_sha: str, config_sha: str, max_file_kb: int) -> list[dict]:
    files = [f for f in files if not is_excluded(f, repos_config.exclude_folders)]
    derived = [
        languages(files, commit_sha),
        frameworks(libraries, files, root, commit_sha),
        runtimes(root, files, commit_sha),
        integrations(root, files, libraries, rules, commit_sha, max_file_kb),
        signals(root, files, libraries, commit_sha),
        monorepo_components(files, repos_config.monorepo_folders, commit_sha),
    ]
    return config_facts(repo, config_sha) + [row for row in derived if row]
