import asyncio
import dataclasses
import json

import pytest
from mcp import Client
from starlette.testclient import TestClient

from repo_index import db, server, tools


@pytest.fixture
def conn(settings, repos_config):
    conn = db.open_index(settings, create=True)
    db.upsert_repos(conn, repos_config)
    return conn


def test_every_tool_in_tools_py_is_registered_with_a_matching_name(settings, conn):
    mcp_server = server.build_server(conn, settings)

    async def list_names():
        async with Client(mcp_server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(list_names()) == set(tools.TOOLS)


def test_calling_a_tool_in_process_returns_the_envelope(settings, conn):
    mcp_server = server.build_server(conn, settings)

    async def call():
        async with Client(mcp_server) as client:
            return await client.call_tool("health", {})

    result = asyncio.run(call())
    assert not result.is_error
    envelope = json.loads(result.content[0].text)
    assert set(envelope) == {"data", "provenance", "stale_repos", "warnings"}


def test_http_request_without_a_bearer_token_gets_401(settings, conn):
    http_settings = dataclasses.replace(settings, mcp_bearer_token="s3cret")
    with TestClient(server.http_app(conn, http_settings)) as client:
        no_auth = client.post("/mcp", json={})
        assert no_auth.status_code == 401
        wrong = client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401
        right = client.post("/mcp", json={}, headers={"Authorization": "Bearer s3cret"})
        assert right.status_code != 401


def test_http_transport_refuses_to_start_with_an_empty_token(settings, conn):
    empty = dataclasses.replace(settings, mcp_bearer_token="")
    with pytest.raises(ValueError, match="MCP_BEARER_TOKEN"):
        server.http_app(conn, empty)
