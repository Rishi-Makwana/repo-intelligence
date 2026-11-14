"""MCP tool registration and the bearer-auth HTTP transport.

Verified against the installed mcp 2.3.0 SDK (mcp.server.mcpserver.MCPServer): `server.tool()` builds a
tool's input schema from `inspect.signature(fn, eval_str=True)`, so each tool below is a real, named,
annotated function (not a functools.partial, which has no __name__ and breaks that introspection) that
closes over the open connection and settings.
"""

from __future__ import annotations

import hmac
import sqlite3

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db, tools
from .config import Settings

SERVER_NAME = "repo-index"


def build_server(conn: sqlite3.Connection, settings: Settings) -> MCPServer:
    server: MCPServer = MCPServer(SERVER_NAME, instructions="See USAGE.md: start here, escalate to the "
                                  "repo-live-search agent for code-level detail or when results are weak.")

    @server.tool(structured_output=False)
    def search_repos(language: str | None = None, library: dict | None = None, framework: str | None = None,
                     integration: str | None = None, criticality: str | None = None, owner: str | None = None,
                     jira_project: str | None = None, confluence_space: str | None = None,
                     vuln_min_severity: str | None = None, eol_only: bool = False, limit: int = 20) -> dict:
        """Repos matching every given filter. search_aid is ranking help, never a fact."""
        return tools.search_repos(conn, settings, language, library, framework, integration, criticality,
                                  owner, jira_project, confluence_space, vuln_min_severity, eol_only, limit)

    @server.tool(structured_output=False)
    def find_library_users(name: str, ecosystem: str | None = None, version: str | None = None) -> dict:
        """Every repo declaring a library, with resolved version counts."""
        return tools.find_library_users(conn, settings, name, ecosystem, version)

    @server.tool(structured_output=False)
    def semantic_search(query: str, language: str | None = None, library: dict | None = None,
                        framework: str | None = None, integration: str | None = None,
                        criticality: str | None = None, owner: str | None = None, jira_project: str | None = None,
                        confluence_space: str | None = None, vuln_min_severity: str | None = None,
                        eol_only: bool = False, limit: int = 10, repo: str | None = None,
                        kind: str | None = None) -> dict:
        """Rank code map and purpose items by meaning. rank is an ordering, not a confidence. Weak or
        empty results mean: escalate to the repo-live-search agent. Results are shared evenly between
        repos, so do not read the repo with more hits as the place a change belongs. Narrow with `repo`
        and `kind`: route for API endpoints, ui_text for screens by the words on them."""
        return tools.semantic_search(conn, settings, query, language, library, framework, integration,
                                     criticality, owner, jira_project, confluence_space, vuln_min_severity,
                                     eol_only, limit, repo=repo, kind=kind)

    @server.tool(structured_output=False)
    def get_repo(repo: str) -> dict:
        """Every fact, library, vulnerability, end-of-life entry and route known for one repo."""
        return tools.get_repo(conn, settings, repo)

    @server.tool(structured_output=False)
    def code_map(repo: str, path_prefix: str | None = None, kind: str | None = None, limit: int = 200) -> dict:
        """Classes, functions, methods, folders and routes with path and line."""
        return tools.code_map(conn, settings, repo, path_prefix, kind, limit)

    @server.tool(structured_output=False)
    def vulnerabilities(repo: str | None = None, min_severity: str | None = None,
                        criticality: str | None = None) -> dict:
        """Known vulnerabilities at or above min_severity, plus libraries not checked (unresolved version)."""
        return tools.vulnerabilities(conn, settings, repo, min_severity, criticality)

    @server.tool(structured_output=False)
    def criticality_report() -> dict:
        """How many repos are classified, which are not, and which critical repos look stale."""
        return tools.criticality_report(conn, settings)

    @server.tool(structured_output=False)
    def health() -> dict:
        """Per-repo freshness and the last update, vulns and selfcheck runs."""
        return tools.health(conn, settings)

    @server.tool(structured_output=False)
    def handoff(question: str, repos: list[str] | None = None, paths: list[str] | None = None) -> dict:
        """The brief for the repo-live-search sub-agent: clone paths, start paths, keywords, token budget."""
        return tools.handoff(conn, settings, question, repos, paths)

    return server


class BearerAuthMiddleware:
    """Rejects every HTTP request without `Authorization: Bearer <token>`, compared with hmac.compare_digest
    so a timing side channel cannot leak the token a byte at a time."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("MCP_BEARER_TOKEN must be set to run the HTTP transport")
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        given = headers.get(b"authorization", b"").decode("latin-1")
        prefix = "Bearer "
        if not (given.startswith(prefix) and hmac.compare_digest(given[len(prefix):], self.token)):
            await JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def http_app(conn: sqlite3.Connection, settings: Settings) -> ASGIApp:
    """The streamable-HTTP ASGI app, wrapped with bearer auth and bound to MCP_HOST."""
    server = build_server(conn, settings)
    inner = server.streamable_http_app(host=settings.mcp_host)
    return BearerAuthMiddleware(inner, settings.mcp_bearer_token)


def serve(settings: Settings) -> None:
    conn = db.open_index(settings)
    server = build_server(conn, settings)
    if settings.mcp_transport == "stdio":
        server.run("stdio")
    else:
        import uvicorn

        uvicorn.run(http_app(conn, settings), host=settings.mcp_host, port=settings.mcp_port)
