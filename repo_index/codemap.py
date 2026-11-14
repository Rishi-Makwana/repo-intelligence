"""Code map: tree-sitter symbols, route rules per framework, OpenAPI routes and the feature items that
semantic_search ranks. No LLM anywhere in this module."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tree_sitter_language_pack as tslp
import yaml

from .facts import LANGUAGES, _read
from .manifests import is_excluded

PARSED_LANGUAGES = {
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".cs": "csharp", ".py": "python", ".go": "go", ".java": "java",
}
CLASS_NODES = {"class_declaration", "abstract_class_declaration", "class_definition", "record_declaration",
               "struct_declaration", "interface_declaration", "enum_declaration"}
METHOD_NODES = {"method_definition", "method_declaration", "constructor_declaration"}
FUNCTION_NODES = {"function_declaration", "generator_function_declaration", "function_definition"}
FUNCTION_VALUES = {"arrow_function", "function_expression", "generator_function"}
SIGNATURE_MAX = 300
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")
OPENAPI_FILE = re.compile(r"^(openapi|swagger)[^/]*\.(ya?ml|json)$", re.IGNORECASE)
README_FILE = re.compile(r"^readme(\.[a-z]+)?\.md$", re.IGNORECASE)
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


@dataclass
class Symbol:
    path: str
    kind: str
    name: str
    signature: str | None
    line: int
    end_line: int

    def row(self, commit_sha: str) -> dict:
        return {"path": self.path, "kind": self.kind, "name": self.name, "signature": self.signature,
                "line": self.line, "commit_sha": commit_sha}


# --- symbols -----------------------------------------------------------------------------------

def _signature(node, source: bytes, name_node) -> str:
    """From the start of the name's line (skipping attributes/decorators above it) to the body."""
    line_start = source.rfind(b"\n", 0, name_node.start_byte) + 1
    start = max(node.start_byte, line_start)
    value = node.child_by_field_name("value")
    body = node.child_by_field_name("body") or (value.child_by_field_name("body") if value else None)
    end = body.start_byte if body else node.end_byte
    text = " ".join(source[start:end].decode("utf-8", "replace").split()).rstrip("{:= ")
    return text[:SIGNATURE_MAX]


def _symbol_for(node, inside_class: bool) -> tuple[str, object] | None:
    """(kind, node holding the name) for a declaration node, else None."""
    kind = node.type
    if kind in CLASS_NODES:
        return "class", node
    if kind == "type_spec" and (t := node.child_by_field_name("type")) and t.type in ("struct_type", "interface_type"):
        return "class", node
    if kind in METHOD_NODES:
        return "method", node
    if kind in FUNCTION_NODES:
        return ("method" if inside_class else "function"), node
    if kind == "variable_declarator" and (v := node.child_by_field_name("value")) and v.type in FUNCTION_VALUES:
        return "function", node
    return None


def extract_symbols(path: str, source: bytes, language: str) -> list[Symbol]:
    """Classes, functions and methods with their 1-based name line. Function bodies are not entered:
    local helpers inside a function are not part of the map."""
    tree = tslp.get_parser(language).parse(source)
    symbols: list[Symbol] = []
    stack = [(tree.root_node, False)]
    while stack:
        node, inside_class = stack.pop()
        found = _symbol_for(node, inside_class)
        if found:
            kind, holder = found
            name_node = holder.child_by_field_name("name")
            if name_node is not None:
                symbols.append(Symbol(path, kind, name_node.text.decode("utf-8", "replace"),
                                      _signature(node, source, name_node), name_node.start_point[0] + 1,
                                      node.end_point[0] + 1))
            if kind != "class":
                continue
        children_in_class = inside_class or (found is not None and found[0] == "class")
        stack.extend((child, children_in_class) for child in reversed(node.children))
    symbols.sort(key=lambda s: s.line)
    return symbols


# --- routes in code: one small table per framework ---------------------------------------------

def _route(method, route, handler_path, handler_name, line, source_path) -> dict:
    return {"method": method.upper() if method else None, "route": route, "handler_path": handler_path,
            "handler_name": handler_name, "line": line, "source": "code", "source_path": source_path}


def _join(prefix: str, template: str, rooted: tuple[str, ...]) -> str:
    if rooted and template.startswith(rooted):
        return "/" + template.lstrip("~/")
    return "/" + "/".join(p.strip("/") for p in (prefix, template) if p.strip("/"))


def _target(symbols: list[Symbol], line: int) -> Symbol | None:
    """The declaration an attribute/annotation/decorator on `line` belongs to: the next symbol."""
    return next((s for s in symbols if s.line >= line), None)


def _enclosing_class(symbols: list[Symbol], method: Symbol) -> Symbol | None:
    classes = [s for s in symbols if s.kind == "class" and s.line < method.line <= s.end_line]
    return classes[-1] if classes else None


ASPNET = re.compile(r'\[\s*(?:(Http(Get|Post|Put|Delete|Patch))|Route)\s*(?:\(\s*@?"([^"]*)")?')
SPRING = re.compile(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\b(?:\s*\(([^)]*)\))?')
SPRING_PATH = re.compile(r'"([^"]*)"')
SPRING_METHOD = re.compile(r"RequestMethod\.(\w+)")


def _annotated_routes(path: str, text: str, symbols: list[Symbol], scan, rooted: tuple[str, ...]) -> list[dict]:
    """Shared by ASP.NET and Spring: attributes on a class set a prefix, on a method a route.
    `scan(line_text)` yields (http_method | None, template | None, is_route_only). A method template
    starting with one of `rooted` ignores the class prefix (ASP.NET "/" and "~/"; never in Spring)."""
    by_target: dict[int, dict] = {}
    for number, line_text in enumerate(text.splitlines(), 1):
        for method, template, route_only in scan(line_text):
            target = _target(symbols, number)
            if target is None:
                continue
            entry = by_target.setdefault(id(target), {"symbol": target, "line": number, "methods": [],
                                                       "template": None})
            if template is not None and (entry["template"] is None or not route_only):
                entry["template"] = template
            if not route_only:
                entry["methods"].append(method)
    prefixes = {id(e["symbol"]): e["template"] or "" for e in by_target.values() if e["symbol"].kind == "class"}
    routes = []
    for entry in by_target.values():
        symbol = entry["symbol"]
        if symbol.kind == "class":
            continue
        owner = _enclosing_class(symbols, symbol)
        prefix = prefixes.get(id(owner), "") if owner else ""
        controller = re.sub(r"Controller$", "", owner.name) if owner else ""
        route = _join(prefix, entry["template"] or "", rooted)
        route = route.replace("[controller]", controller).replace("[action]", symbol.name)
        handler = f"{owner.name}.{symbol.name}" if owner else symbol.name
        for method in entry["methods"] or [None]:
            routes.append(_route(method, route, path, handler, entry["line"], path))
    return routes


def _scan_aspnet(line_text: str):
    for match in ASPNET.finditer(line_text):
        yield match.group(2), match.group(3), match.group(1) is None


def _scan_spring(line_text: str):
    for match in SPRING.finditer(line_text):
        args = match.group(2) or ""
        template = SPRING_PATH.search(args)
        if match.group(1) == "Request":
            method = SPRING_METHOD.search(args)
            yield (method.group(1) if method else None), (template.group(1) if template else None), not method
        else:
            yield match.group(1), (template.group(1) if template else ""), False


FASTAPI = re.compile(r"^\s*@\w+\.(get|post|put|delete|patch)\(\s*[\"']([^\"']*)[\"']")
EXPRESS = re.compile(r"\b(?:app|router|\w+Router)\.(get|post|put|delete|patch|all)\(\s*[\"'`]([^\"'`]+)[\"'`]\s*,(.*)")
EXPRESS_HANDLER = re.compile(r"([A-Za-z_$][\w$.]*)\s*\)?\s*;?\s*$")
GO_HANDLEFUNC = re.compile(r'\.HandleFunc\(\s*"([^"]+)"\s*,\s*([\w.]+)')
GO_GIN_ECHO = re.compile(r'\.(GET|POST|PUT|DELETE|PATCH)\(\s*"([^"]+)"\s*,\s*([\w.]+)')


def _line_routes(path: str, text: str, symbols: list[Symbol], language: str) -> list[dict]:
    routes = []
    for number, line_text in enumerate(text.splitlines(), 1):
        if language == "python" and (m := FASTAPI.match(line_text)):
            # ponytail: APIRouter(prefix=...) is not applied; the live agent confirms full paths.
            target = _target(symbols, number)
            routes.append(_route(m.group(1), m.group(2), path, target.name if target else None, number, path))
        elif language in ("javascript", "typescript", "tsx") and (m := EXPRESS.search(line_text)):
            rest = m.group(3)
            handler = None if ("=>" in rest or "function" in rest) else EXPRESS_HANDLER.search(rest)
            routes.append(_route(m.group(1), m.group(2), path, handler.group(1) if handler else None, number, path))
        elif language == "go":
            if m := GO_HANDLEFUNC.search(line_text):
                method, _, route = m.group(1).rpartition(" ")
                routes.append(_route(method or None, route, path, m.group(2), number, path))
            elif m := GO_GIN_ECHO.search(line_text):
                routes.append(_route(m.group(1), m.group(2), path, m.group(3), number, path))
    return routes


MINIMAL_API = re.compile(r'\.Map(Get|Post|Put|Delete|Patch)\(\s*@?"([^"]*)"\s*,\s*([A-Za-z_][\w.]*)?')
MAP_GROUP = re.compile(r'\.MapGroup\(\s*@?"([^"]*)"')


def _minimal_api_routes(path: str, text: str, symbols: list[Symbol]) -> list[dict]:
    routes = []
    for number, line_text in enumerate(text.splitlines(), 1):
        for match in MINIMAL_API.finditer(line_text):
            handler = match.group(3)
            owners = [s for s in symbols if s.kind == "class" and s.line < number <= s.end_line]
            if handler and owners and "." not in handler:
                handler = f"{owners[-1].name}.{handler}"
            route = _route(match.group(1), match.group(2) or "/", path, handler, number, path)
            routes.append({**route, "minimal_api": True})
    return routes


def apply_group_prefix(routes: list[dict], group_prefixes: set[str]) -> None:
    """Prefix minimal-API routes with the repo's MapGroup path, and drop the marker key."""
    # ponytail: only the one-group case ("/api" around everything) is resolved, and it is applied to every
    # minimal-API route. With several groups the routes stay unprefixed and the live agent confirms the
    # path. Trace which group each endpoint is mapped on if a repo mixes grouped and ungrouped routes.
    prefix = next(iter(group_prefixes)) if len(group_prefixes) == 1 else ""
    for route in routes:
        if route.pop("minimal_api", False) and prefix:
            route["route"] = _join(prefix, route["route"], ())


def code_routes(path: str, text: str, symbols: list[Symbol], language: str) -> list[dict]:
    if language == "csharp":
        return (_annotated_routes(path, text, symbols, _scan_aspnet, ("/", "~/"))
                + _minimal_api_routes(path, text, symbols))
    if language == "java":
        return _annotated_routes(path, text, symbols, _scan_spring, ())
    return _line_routes(path, text, symbols, language)


NEXT_PAGE = re.compile(r"^(?:.*?/)?app/(.*/)?page\.(tsx|jsx|ts|js)$")
NEXT_ROUTE = re.compile(r"^(?:.*?/)?app/(.*/)?route\.(ts|js)$")
# ponytail: any app/ or pages/ folder counts once the repo depends on `next`; tighten if a non-route folder
# of that name causes false routes.
NEXT_PAGES = re.compile(r"^(?:.*?/)?pages/(.+)\.(tsx|jsx|ts|js)$")
NEXT_ROUTE_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def _app_route(folder: str | None) -> str:
    parts = [p for p in (folder or "").strip("/").split("/") if p and not (p.startswith("(") and p.endswith(")"))]
    return "/" + "/".join(parts)


def nextjs_routes(path: str, symbols: list[Symbol]) -> list[dict]:
    """File-based routes: app/**/page.tsx, app/**/route.ts (one row per exported HTTP method), pages/**."""
    if m := NEXT_PAGE.match(path):
        return [_route("GET", _app_route(m.group(1)), path, "page", 1, path)]
    if m := NEXT_ROUTE.match(path):
        return [_route(s.name, _app_route(m.group(1)), path, s.name, s.line, path)
                for s in symbols if s.kind == "function" and s.name in NEXT_ROUTE_METHODS]
    if m := NEXT_PAGES.match(path):
        name = m.group(1)
        if PurePosixPath(name).name.startswith("_"):
            return []
        route = "/" + re.sub(r"(^|/)index$", "", name)
        return [_route(None if name.startswith("api/") else "GET", route.rstrip("/") or "/", path, "default",
                       1, path)]
    return []


# --- OpenAPI -----------------------------------------------------------------------------------

def _mapping(node) -> dict:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {k.value: (k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)}


def _scalar(node) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def openapi_routes(path: str, text: str) -> list[dict]:
    """Operations of an OpenAPI/Swagger file with the line of each method key. JSON parses as YAML."""
    root = _mapping(yaml.compose(text))
    if "paths" not in root:
        return []
    routes = []
    for route, (_, item) in _mapping(root["paths"][1]).items():
        for method, (key, operation) in _mapping(item).items():
            if method.lower() not in HTTP_METHODS:
                continue
            op = _mapping(operation)
            routes.append({"method": method.upper(), "route": route, "handler_path": None,
                           "handler_name": _scalar(op["operationId"][1]) if "operationId" in op else None,
                           "line": key.start_mark.line + 1, "source": "openapi", "source_path": path,
                           "summary": _scalar(op["summary"][1]) if "summary" in op else None})
    return routes


# --- feature items -----------------------------------------------------------------------------

def readme_headings(path: str, text: str) -> list[dict]:
    headings, fenced = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (m := HEADING.match(line)):
            headings.append({"kind": "readme_heading", "text": m.group(2), "path": path, "line": number})
    return headings


UI_TEXT_ATTRIBUTES = {"placeholder", "aria-label", "title", "label", "alt"}
UI_TEXT_MAX = 300


def ui_text(path: str, source: bytes, language: str) -> list[dict]:
    """One feature per JSX file: the words a user sees on that screen (element text and a few labelling
    attributes), so a plain-language description of a screen can find the file that draws it."""
    # ponytail: literal strings only. Text from variables, translation keys or the server is not seen,
    # and styling has no words to match. Follow imports to shared copy files if screens go unmatched.
    words: list[str] = []
    first_line = None
    stack = [tslp.get_parser(language).parse(source).root_node]
    while stack:
        node = stack.pop()
        text = None
        if node.type == "jsx_text":
            text = node.text.decode("utf-8", "replace")
        elif node.type == "jsx_attribute" and node.children and node.children[-1].type == "string" \
                and node.children[0].text.decode("utf-8", "replace") in UI_TEXT_ATTRIBUTES:
            text = node.children[-1].text.decode("utf-8", "replace")[1:-1]
        text = " ".join((text or "").split())
        if len(text) >= 3 and any(c.isalpha() for c in text) and text not in words:
            words.append(text)
            first_line = first_line or node.start_point[0] + 1
        stack.extend(reversed(node.children))
    if not words:
        return []
    return [{"kind": "ui_text", "text": f"{path}: {' · '.join(words)}"[:len(path) + 2 + UI_TEXT_MAX],
             "path": path, "line": first_line}]


def module_folders(files: list[str], monorepo_folders) -> list[str]:
    """Top-level folders, plus each component folder under a monorepo folder (packages/x)."""
    folders = set()
    for f in files:
        parts = PurePosixPath(f).parts
        if len(parts) > 1 and not parts[0].startswith("."):
            folders.add(parts[0])
            if parts[0] in monorepo_folders and len(parts) > 2:
                folders.add("/".join(parts[:2]))
    return sorted(folders)


def _route_text(route: dict) -> str:
    text = f"{route['method'] or 'ANY'} {route['route']}"
    if route.get("summary"):
        text += f": {route['summary']}"
    if route["handler_name"]:
        text += f" ({route['handler_name']})"
    return text


# --- the stage ---------------------------------------------------------------------------------

def build_code_map(root: Path, files: list[str], libraries: list[dict], exclude_folders, monorepo_folders,
                   commit_sha: str, max_file_kb: int) -> tuple[dict[str, list[dict]], list[str]]:
    """Returns ({symbols, routes, features}, warnings)."""
    warnings: list[str] = []
    failed_languages: set[str] = set()
    uses_next = any(lib["ecosystem"] == "npm" and lib["name"] == "next" for lib in libraries)
    files = [f for f in files if not is_excluded(f, exclude_folders) and (root / f).is_file()]
    symbols: list[dict] = []
    routes: list[dict] = []
    features: list[dict] = []
    unparsed_folders: set[str] = set()
    group_prefixes: set[str] = set()

    for path in files:
        suffix = PurePosixPath(path).suffix.lower()
        name = PurePosixPath(path).name
        too_big = (root / path).stat().st_size > max_file_kb * 1024
        language = PARSED_LANGUAGES.get(suffix)
        if OPENAPI_FILE.match(name) and not too_big:
            try:
                routes += openapi_routes(path, _read(root, path))
            except yaml.YAMLError as exc:
                warnings.append(f"openapi parse failed: {path}: {exc}")
        elif README_FILE.match(name) and not too_big:
            features += readme_headings(path, _read(root, path))
        if language is None or language in failed_languages or too_big:
            if suffix in LANGUAGES and not too_big:
                unparsed_folders.add(str(PurePosixPath(path).parent))
            continue
        source = (root / path).read_bytes()
        try:
            file_symbols = extract_symbols(path, source, language)
        except tslp.Error as exc:
            failed_languages.add(language)
            warnings.append(f"tree-sitter grammar {language!r} unavailable, folder rows only: {exc}")
            unparsed_folders.add(str(PurePosixPath(path).parent))
            continue
        symbols += [s.row(commit_sha) for s in file_symbols]
        text = source.decode("utf-8", "replace")
        routes += code_routes(path, text, file_symbols, language)
        if suffix in (".tsx", ".jsx") and ".test." not in name and ".spec." not in name:
            features += ui_text(path, source, language)
        if language == "csharp":
            group_prefixes.update(p for p in MAP_GROUP.findall(text) if p.strip("/"))
        if uses_next and language in ("typescript", "tsx", "javascript"):
            routes += nextjs_routes(path, file_symbols)

    # ponytail: unsupported languages get one module row per folder; add a grammar to PARSED_LANGUAGES to go deeper.
    symbols += [{"path": folder, "kind": "module", "name": folder, "signature": None, "line": 0,
                 "commit_sha": commit_sha} for folder in sorted(unparsed_folders)]
    apply_group_prefix(routes, group_prefixes)
    for route in routes:
        kind = "openapi_op" if route["source"] == "openapi" else "route"
        features.append({"kind": kind, "text": _route_text(route), "path": route["handler_path"] or route["source_path"],
                         "line": route["line"]})
        route.pop("summary", None)
        route["commit_sha"] = commit_sha
    features += [{"kind": "module", "text": folder, "path": folder, "line": None}
                 for folder in module_folders(files, monorepo_folders)]
    for feature in features:
        feature["commit_sha"] = commit_sha
    return {"symbols": symbols, "routes": routes, "features": features}, warnings
