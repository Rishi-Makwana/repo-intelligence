from repo_index.codemap import build_code_map, extract_symbols, openapi_routes
from tests.conftest import FIXTURES, fixture_files

ROOT = FIXTURES / "codemap"
NEXT = [{"ecosystem": "npm", "name": "next"}]


def code_map(libraries=NEXT, max_file_kb=500):
    rows, warnings = build_code_map(ROOT, fixture_files(ROOT), libraries, ["node_modules"], ["apps"], "c0ffee",
                                    max_file_kb)
    assert warnings == []
    return rows


def symbols_in(rows, path):
    return [(s["kind"], s["name"], s["line"]) for s in rows["symbols"] if s["path"] == path]


def route(rows, method, path):
    return next((r["handler_path"], r["handler_name"], r["line"]) for r in rows["routes"]
                if r["method"] == method and r["route"] == path)


def test_symbols_have_kind_name_and_name_line_per_language():
    rows = code_map()
    assert symbols_in(rows, "src/Api/Controllers/OrdersController.cs") == [
        ("class", "OrdersController", 7), ("method", "OrdersController", 9), ("method", "GetById", 12),
        ("method", "Create", 18)]
    assert symbols_in(rows, "server/app.js") == [
        ("function", "listAddresses", 4), ("function", "suggestAddress", 8), ("class", "AddressCache", 16),
        ("method", "lookup", 17)]
    assert symbols_in(rows, "apps/web/app/api/orders/route.ts") == [("function", "GET", 3), ("function", "POST", 7)]
    assert symbols_in(rows, "py/api.py") == [("class", "Quote", 6), ("method", "total", 7),
                                             ("function", "read_quote", 12)]
    assert symbols_in(rows, "svc/main.go") == [("class", "Server", 5), ("method", "Health", 7), ("function", "main", 9)]
    assert symbols_in(rows, "java/com/example/InvoiceController.java") == [
        ("class", "InvoiceController", 5), ("method", "get", 8)]
    signature = next(s["signature"] for s in rows["symbols"] if s["name"] == "GetById")
    assert signature == "public IActionResult GetById(int id)"


def test_tsx_and_signatures_skip_decorators():
    source = b"@Component()\nexport class Panel {\n  render(): JSX.Element { return <div/>; }\n}\n"
    symbols = extract_symbols("p.tsx", source, "tsx")
    assert [(s.kind, s.name, s.line, s.signature) for s in symbols] == [
        ("class", "Panel", 2, "class Panel"), ("method", "render", 3, "render(): JSX.Element")]


def test_routes_map_to_their_handler_file_and_line():
    rows = code_map()
    assert route(rows, "GET", "/api/Orders/{id}") == ("src/Api/Controllers/OrdersController.cs",
                                                     "OrdersController.GetById", 11)
    assert route(rows, "POST", "/api/Orders") == ("src/Api/Controllers/OrdersController.cs",
                                                 "OrdersController.Create", 17)
    assert route(rows, "GET", "/invoices/{id}") == ("java/com/example/InvoiceController.java",
                                                   "InvoiceController.get", 7)
    assert route(rows, "GET", "/addresses") == ("server/app.js", "listAddresses", 12)
    assert route(rows, "POST", "/addresses/suggest") == ("server/app.js", "suggestAddress", 13)
    assert route(rows, "DELETE", "/addresses/:id") == ("server/app.js", None, 14)
    assert route(rows, "GET", "/quotes/{quote_id}") == ("py/api.py", "read_quote", 11)
    assert route(rows, "GET", "/health") == ("svc/main.go", "s.Health", 11)


def test_aspnet_minimal_api_routes_map_to_their_handler():
    rows = code_map()
    assert route(rows, "GET", "/resources/{resourceId:guid}") == (
        "src/Api/Endpoints/GetResource.cs", "GetResource.Handle", 6)
    assert route(rows, "POST", "/ping") == ("src/Api/Endpoints/GetResource.cs", None, 19)


def test_nextjs_file_routes_only_when_the_repo_uses_next():
    rows = code_map()
    assert route(rows, "GET", "/orders/[id]")[0] == "apps/web/app/orders/[id]/page.tsx"
    assert route(rows, "GET", "/cart")[0] == "apps/web/app/(shop)/cart/page.tsx"
    assert route(rows, "POST", "/api/orders") == ("apps/web/app/api/orders/route.ts", "POST", 7)
    assert route(rows, "GET", "/about")[0] == "apps/web/pages/about.tsx"
    assert not [r for r in code_map(libraries=[])["routes"] if r["route"] == "/cart"]


def test_openapi_operations_become_routes_and_features():
    rows = code_map()
    op = [r for r in rows["routes"] if r["source"] == "openapi"]
    assert op == [{"method": "GET", "route": "/addresses/autocomplete", "handler_path": None,
                   "handler_name": "autocompleteAddress", "line": 7, "source": "openapi",
                   "source_path": "openapi.yaml", "commit_sha": "c0ffee"}]
    feature = next(f for f in rows["features"] if f["kind"] == "openapi_op")
    assert feature["text"] == "GET /addresses/autocomplete: Suggest addresses as the user types (autocompleteAddress)"
    json_spec = '{"openapi": "3.0.0",\n "paths": {"/a": {\n  "post": {"operationId": "makeA"}}}}'
    assert [(r["method"], r["route"], r["line"]) for r in openapi_routes("swagger.json", json_spec)] == [
        ("POST", "/a", 3)]


def test_features_cover_readme_headings_h1_to_h3_and_module_folders():
    rows = code_map()
    headings = [(f["text"], f["line"]) for f in rows["features"] if f["kind"] == "readme_heading"]
    assert headings == [("Shop", 1), ("Address autocomplete", 5)]
    modules = [f["text"] for f in rows["features"] if f["kind"] == "module"]
    assert "apps/web" in modules and "node_modules" not in modules
    assert all(f["commit_sha"] == "c0ffee" for f in rows["features"])


def test_unsupported_languages_get_folder_rows_and_excluded_or_large_files_are_skipped():
    rows = code_map()
    assert [s for s in rows["symbols"] if s["kind"] == "module"] == [
        {"path": "scripts", "kind": "module", "name": "scripts", "signature": None, "line": 0, "commit_sha": "c0ffee"}]
    assert not [s for s in rows["symbols"] if s["path"].startswith("node_modules")]
    assert not code_map(max_file_kb=0)["symbols"]
