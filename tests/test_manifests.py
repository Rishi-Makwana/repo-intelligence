import shutil

import pytest

from repo_index.manifests import parse_manifests
from tests.conftest import FIXTURES, fixture_files

EXCLUDE = ("node_modules", "vendor", "build", "dist", "target", "generated")


def libraries(fixture, root=None):
    root = root or FIXTURES / fixture
    rows, errors = parse_manifests(root, fixture_files(root), "abc123", EXCLUDE)
    assert errors == []
    assert {r["commit_sha"] for r in rows} == {"abc123"}
    return [(r["ecosystem"], r["name"], r["version_declared"], r["version_resolved"], r["scope"], r["manifest_path"])
            for r in rows]


EXPECTED = {
    "npm": [
        ("npm", "express", "4.18.2", "4.18.2", "runtime", "package.json"),
        ("npm", "lodash", "^4.17.15", "4.17.15", "runtime", "package.json"),
        ("npm", "@slack/web-api", "^6.9.0", "6.9.1", "runtime", "package.json"),
        ("npm", "jest", "^29.0.0", None, "dev", "package.json"),
    ],
    "pypi": [
        ("PyPI", "fastapi", ">=0.100", "0.110.0", "runtime", "pyproject.toml"),
        ("PyPI", "requests", "==2.31.0", "2.31.0", "runtime", "pyproject.toml"),
        ("PyPI", "slack-sdk", ">=3.0", "3.27.1", "runtime", "pyproject.toml"),
        ("PyPI", "pydantic", None, None, "runtime", "pyproject.toml"),
        ("PyPI", "pytest", ">=8", "8.1.1", "group:dev", "pyproject.toml"),
        ("PyPI", "black", "==24.1.0", "24.1.0", "dev", "requirements-dev.txt"),
        ("PyPI", "flake8", ">=6.0", None, "dev", "requirements-dev.txt"),
    ],
    "go": [
        ("Go", "github.com/gin-gonic/gin", "v1.6.0", "v1.6.0", "direct", "go.mod"),
        ("Go", "github.com/google/uuid", "v1.6.0", "v1.6.0", "direct", "go.mod"),
        ("Go", "golang.org/x/net", "v0.17.0", "v0.17.0", "indirect", "go.mod"),
    ],
    "maven": [
        ("Maven", "org.springframework.boot:spring-boot-starter-parent", "2.7.18", "2.7.18", "parent", "pom.xml"),
        ("Maven", "org.springframework.boot:spring-boot-starter-web", None, None, "compile", "pom.xml"),
        ("Maven", "org.apache.logging.log4j:log4j-core", "${log4j.version}", "2.14.1", "compile", "pom.xml"),
        ("Maven", "com.example:payments-model", "${project.version}", "1.4.0", "compile", "pom.xml"),
        ("Maven", "junit:junit", "[4.12,5.0)", None, "test", "pom.xml"),
    ],
    "cargo": [
        ("crates.io", "serde", "1.0", "1.0.197", "runtime", "Cargo.toml"),
        ("crates.io", "time", "0.1", "0.1.43", "runtime", "Cargo.toml"),
        ("crates.io", "tokio", "1", None, "runtime", "Cargo.toml"),
        ("crates.io", "exact-crate", "=0.3.2", "0.3.2", "runtime", "Cargo.toml"),
        ("crates.io", "proptest", "1.4", "1.4.0", "dev", "Cargo.toml"),
    ],
    "nuget": [
        ("NuGet", "Newtonsoft.Json", "12.0.1", "12.0.1", "runtime", "src/Api/Api.csproj"),
        ("NuGet", "Serilog", "3.*", "3.1.1", "runtime", "src/Api/Api.csproj"),
        ("NuGet", "Dapper", "2.1.28", "2.1.28", "runtime", "src/Api/Api.csproj"),
        ("NuGet", "Polly", None, None, "runtime", "src/Api/Api.csproj"),
    ],
}


@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_exact_library_rows(fixture):
    assert libraries(fixture) == EXPECTED[fixture]


def test_every_fixture_has_an_unresolved_version_except_go():
    for fixture, rows in EXPECTED.items():
        assert (fixture == "go") == all(r[3] is not None for r in rows), fixture


def test_uv_lock_resolves_pyproject(tmp_path):
    root = tmp_path / "pypi"
    shutil.copytree(FIXTURES / "pypi", root)
    (root / "poetry.lock").unlink()
    (root / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "FastAPI"\nversion = "0.111.1"\n')
    rows = {r[1]: r[3] for r in libraries("pypi", root)}
    assert rows["fastapi"] == "0.111.1"
    assert rows["requests"] == "2.31.0"   # exact pin still resolves without a lock entry
    assert rows["slack-sdk"] is None      # range, no lock entry: unresolved, not guessed


def test_monorepo_package_lockfile_and_poetry_sections(tmp_path):
    pkg = tmp_path / "packages" / "web"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"dependencies": {"react": "^18.2.0"}}')
    (pkg / "package-lock.json").write_text('{"lockfileVersion": 1, "dependencies": {"react": {"version": "18.2.0"}}}')
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.11"\nDjango = "4.2.11"\ncelery = {version = "^5.3"}\n'
        '[tool.poetry.group.dev.dependencies]\nruff = "^0.4"\n')
    rows, errors = parse_manifests(tmp_path, fixture_files(tmp_path), "s", EXCLUDE)
    assert errors == []
    got = {(r["name"], r["version_resolved"], r["scope"], r["manifest_path"]) for r in rows}
    assert got == {("react", "18.2.0", "runtime", "packages/web/package.json"),
                   ("django", "4.2.11", "runtime", "pyproject.toml"),
                   ("celery", None, "runtime", "pyproject.toml"),
                   ("ruff", None, "group:dev", "pyproject.toml")}


def test_broken_manifest_is_reported_not_skipped_silently(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    rows, errors = parse_manifests(tmp_path, ["package.json"], "s", EXCLUDE)
    assert rows == [] and errors and errors[0].startswith("package.json:")
