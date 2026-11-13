"""Dependency manifests and lockfile resolution, per ecosystem.

A version is `version_resolved` only when a lockfile says so or the manifest pins it exactly.
Anything else (ranges, carets, parent/BOM-managed, central package management) stays NULL = unresolved.
"""

from __future__ import annotations

import json
import re
import tomllib

# stdlib expat (2.4.1+ in uv-managed Pythons) rejects entity-expansion bombs and never fetches external
# entities; manifests come from the team's own repos, so defusedxml is not worth a dependency here.
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

NPM_EXACT = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
PIN = re.compile(r"^===?\s*([A-Za-z0-9._+!-]+)$")


def is_excluded(path: str, exclude_folders) -> bool:
    return any(part in exclude_folders for part in PurePosixPath(path).parts[:-1])


def _row(ecosystem, name, declared, resolved, scope, manifest_path, commit_sha) -> dict:
    return {"ecosystem": ecosystem, "name": name, "version_declared": declared,
            "version_resolved": resolved, "scope": scope, "manifest_path": manifest_path,
            "commit_sha": commit_sha}


def _sibling(root: Path, manifest_path: str, name: str) -> Path | None:
    candidate = root / PurePosixPath(manifest_path).parent / name
    return candidate if candidate.is_file() else None


# --- npm ---------------------------------------------------------------------------------------

def _npm_lock_versions(lock_path: Path | None) -> dict[str, str]:
    if not lock_path:
        return {}
    lock = json.loads(lock_path.read_text())
    versions = {}
    for key, entry in (lock.get("packages") or {}).items():
        if key.startswith("node_modules/") and "/node_modules/" not in key and "version" in entry:
            versions[key.removeprefix("node_modules/")] = entry["version"]
    for name, entry in (lock.get("dependencies") or {}).items():  # lockfileVersion 1
        versions.setdefault(name, entry.get("version"))
    return versions


def parse_npm(root: Path, path: str, sha: str) -> list[dict]:
    manifest = json.loads((root / path).read_text())
    locked = _npm_lock_versions(_sibling(root, path, "package-lock.json"))
    rows = []
    for section, scope in (("dependencies", "runtime"), ("devDependencies", "dev"),
                           ("peerDependencies", "peer"), ("optionalDependencies", "optional")):
        for name, declared in (manifest.get(section) or {}).items():
            resolved = locked.get(name) or (declared.lstrip("v") if NPM_EXACT.match(declared) else None)
            rows.append(_row("npm", name, declared, resolved, scope, path, sha))
    return rows


# --- PyPI --------------------------------------------------------------------------------------

def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pep508(requirement: str) -> tuple[str, str | None, str | None]:
    """Return (normalized name, declared specifier or None, exact pin or None)."""
    requirement = requirement.split(";", 1)[0].split("#", 1)[0].strip()
    match = PEP508_NAME.match(requirement)
    if not match:
        return "", None, None
    spec = match.group(2).strip().strip("()").strip() or None
    pin = PIN.match(spec) if spec else None
    exact = pin.group(1) if pin and "*" not in pin.group(1) else None
    return normalize_pypi(match.group(1)), spec, exact


def _python_lock_versions(root: Path, path: str) -> dict[str, str]:
    for lock_name in ("uv.lock", "poetry.lock"):
        lock = _sibling(root, path, lock_name) or (root / lock_name if (root / lock_name).is_file() else None)
        if lock:
            data = tomllib.loads(lock.read_text())
            return {normalize_pypi(p["name"]): p["version"] for p in data.get("package", []) if "version" in p}
    return {}


def parse_pyproject(root: Path, path: str, sha: str) -> list[dict]:
    data = tomllib.loads((root / path).read_text())
    locked = _python_lock_versions(root, path)
    entries: list[tuple[str, str]] = []  # (requirement string, scope)
    project = data.get("project") or {}
    entries += [(req, "runtime") for req in project.get("dependencies") or []]
    for group, reqs in (project.get("optional-dependencies") or {}).items():
        entries += [(req, f"optional:{group}") for req in reqs]
    for group, reqs in (data.get("dependency-groups") or {}).items():
        entries += [(req, f"group:{group}") for req in reqs if isinstance(req, str)]

    poetry = (data.get("tool") or {}).get("poetry") or {}
    poetry_sections = [(poetry.get("dependencies") or {}, "runtime"), (poetry.get("dev-dependencies") or {}, "dev")]
    poetry_sections += [((g.get("dependencies") or {}), f"group:{name}")
                        for name, g in (poetry.get("group") or {}).items()]

    rows, seen = [], set()

    def add(name, declared, exact, scope):
        if not name or name in seen:
            return
        seen.add(name)
        rows.append(_row("PyPI", name, declared, locked.get(name) or exact, scope, path, sha))

    for requirement, scope in entries:
        add(*_pep508(requirement), scope)
    for section, scope in poetry_sections:
        for raw_name, value in section.items():
            if raw_name.lower() == "python":
                continue
            declared = value if isinstance(value, str) else (value.get("version") if isinstance(value, dict) else None)
            exact = declared if declared and re.fullmatch(r"=?=?\d[\w.+!-]*", declared) else None
            add(normalize_pypi(raw_name), declared, exact.lstrip("=") if exact else None, scope)
    return rows


def parse_requirements(root: Path, path: str, sha: str) -> list[dict]:
    rows, seen = [], set()
    scope = "dev" if re.search(r"dev|test", PurePosixPath(path).name) else "runtime"
    for line in (root / path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", "git+", "http:", "https:")):
            continue
        name, declared, exact = _pep508(line)
        if name and name not in seen:
            seen.add(name)
            rows.append(_row("PyPI", name, declared, exact, scope, path, sha))
    return rows


# --- Go ----------------------------------------------------------------------------------------

def parse_go_mod(root: Path, path: str, sha: str) -> list[dict]:
    # ponytail: `replace` directives are ignored; resolve them if a repo relies on forks.
    rows, in_block = [], False
    for raw in (root / path).read_text().splitlines():
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if line.startswith("require "):
            line = line.removeprefix("require ").strip()
        elif not in_block:
            continue
        indirect = "// indirect" in line
        parts = line.split("//", 1)[0].split()
        if len(parts) == 2:
            rows.append(_row("Go", parts[0], parts[1], parts[1], "indirect" if indirect else "direct", path, sha))
    return rows


# --- Maven -------------------------------------------------------------------------------------

def _strip_ns(tree: ET.Element) -> ET.Element:
    for element in tree.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return tree


def _maven_substitute(value: str | None, props: dict[str, str]) -> str | None:
    if value is None:
        return None
    for _ in range(5):
        new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def parse_pom(root: Path, path: str, sha: str) -> list[dict]:
    project = _strip_ns(ET.parse(root / path).getroot())
    props = {el.tag: (el.text or "").strip() for el in project.findall("properties/*")}
    version = project.findtext("version") or project.findtext("parent/version")
    if version:
        props.update({"project.version": version.strip(), "version": version.strip()})

    def resolved(declared):
        value = _maven_substitute(declared, props)
        if not value or "${" in value or value[:1] in "[(" or "," in value:
            return None
        return value

    rows = []
    parent = project.find("parent")
    if parent is not None and parent.findtext("artifactId"):
        declared = (parent.findtext("version") or "").strip() or None
        rows.append(_row("Maven", f"{parent.findtext('groupId', '').strip()}:{parent.findtext('artifactId').strip()}",
                         declared, resolved(declared), "parent", path, sha))
    seen = set()
    for dep in project.findall("dependencies/dependency"):
        name = f"{(dep.findtext('groupId') or '').strip()}:{(dep.findtext('artifactId') or '').strip()}"
        if name in seen:
            continue
        seen.add(name)
        declared = (dep.findtext("version") or "").strip() or None
        rows.append(_row("Maven", name, declared, resolved(declared),
                         (dep.findtext("scope") or "compile").strip(), path, sha))
    return rows


# --- Cargo -------------------------------------------------------------------------------------

def _cargo_lock_versions(root: Path, path: str) -> dict[str, list[str]]:
    lock = _sibling(root, path, "Cargo.lock") or (root / "Cargo.lock" if (root / "Cargo.lock").is_file() else None)
    versions: dict[str, list[str]] = {}
    if lock:
        for package in tomllib.loads(lock.read_text()).get("package", []):
            versions.setdefault(package["name"], []).append(package["version"])
    return versions


def parse_cargo(root: Path, path: str, sha: str) -> list[dict]:
    data = tomllib.loads((root / path).read_text())
    locked = _cargo_lock_versions(root, path)
    rows = []
    for section, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev"),
                           ("build-dependencies", "build")):
        for name, value in (data.get(section) or {}).items():
            declared = value if isinstance(value, str) else value.get("version")
            package = value.get("package", name) if isinstance(value, dict) else name
            candidates = locked.get(package, [])
            if len(candidates) == 1:
                resolved = candidates[0]
            elif declared and declared.startswith("="):
                resolved = declared.lstrip("= ")
            else:
                resolved = None  # absent from the lock, or several locked versions we cannot tell apart
            rows.append(_row("crates.io", package, declared, resolved, scope, path, sha))
    return rows


# --- NuGet -------------------------------------------------------------------------------------

def _nuget_lock_versions(root: Path, path: str) -> dict[str, str]:
    lock = _sibling(root, path, "packages.lock.json")
    versions: dict[str, str] = {}
    if lock:
        for framework in (json.loads(lock.read_text()).get("dependencies") or {}).values():
            for name, entry in framework.items():
                if entry.get("resolved"):
                    versions.setdefault(name.lower(), entry["resolved"])
    return versions


def parse_csproj(root: Path, path: str, sha: str) -> list[dict]:
    project = _strip_ns(ET.parse(root / path).getroot())
    locked = _nuget_lock_versions(root, path)
    rows, seen = [], set()
    for ref in project.iter("PackageReference"):
        name = ref.get("Include") or ref.get("Update")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        # ponytail: central package management (Directory.Packages.props) is not read; such refs stay unresolved.
        declared = ref.get("Version") or ref.findtext("Version")
        exact = declared if declared and not re.search(r"[*\[\](),]", declared) else None
        rows.append(_row("NuGet", name, declared, locked.get(name.lower()) or exact, "runtime", path, sha))
    return rows


# --- walk --------------------------------------------------------------------------------------

def _parser_for(path: str):
    name = PurePosixPath(path).name
    if name == "package.json":
        return parse_npm
    if name == "pyproject.toml":
        return parse_pyproject
    if re.fullmatch(r"requirements.*\.txt", name):
        return parse_requirements
    if name == "go.mod":
        return parse_go_mod
    if name == "pom.xml":
        return parse_pom
    if name == "Cargo.toml":
        return parse_cargo
    if name.endswith(".csproj"):
        return parse_csproj
    return None


def parse_manifests(root: Path, files: list[str], commit_sha: str, exclude_folders) -> tuple[list[dict], list[str]]:
    """Parse every manifest in `files` (repo-relative paths). Returns (library rows, parse errors)."""
    rows, errors, keys = [], [], set()
    for path in sorted(files):
        parser = _parser_for(path)
        if not parser or is_excluded(path, exclude_folders):
            continue
        try:
            parsed = parser(root, path, commit_sha)
        except (OSError, ValueError, ET.ParseError, tomllib.TOMLDecodeError, AttributeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        for row in parsed:
            key = (row["ecosystem"], row["name"], row["manifest_path"])
            if key not in keys:
                keys.add(key)
                rows.append(row)
    return rows, errors
