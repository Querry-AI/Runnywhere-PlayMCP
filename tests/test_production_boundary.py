"""Production transport and immutable release-identity boundaries."""

import asyncio

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from runart import server


def _rpc_headers(**extra):
    return {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **extra,
    }


def test_release_sha_requires_full_git_identity_only_in_production():
    sha = "a" * 40
    assert server._validated_release_sha(sha.upper(), production=True) == sha
    assert server._validated_release_sha("unknown", production=False) == "unknown"
    for invalid in ("unknown", "abc123", "g" * 40, "a" * 39, "a" * 41):
        with pytest.raises(RuntimeError):
            server._validated_release_sha(invalid, production=True)


def test_transport_security_accepts_preview_and_rejects_untrusted_origin_and_host():
    async def check():
        boundary_mcp = FastMCP(
            "boundary-test", stateless_http=True, json_response=True,
            transport_security=server._TRANSPORT_SECURITY)
        app = boundary_mcp.streamable_http_app()
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "boundary-test", "version": "1"}},
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost:8000") as client:
                accepted = await client.post(
                    "/mcp", json=payload,
                    headers=_rpc_headers(Origin="https://preview-chatgpt.kakao.com"))
                assert accepted.status_code == 200

                bad_origin = await client.post(
                    "/mcp", json=payload,
                    headers=_rpc_headers(Origin="https://evil.example"))
                assert bad_origin.status_code == 403

                bad_host = await client.post(
                    "/mcp", json=payload,
                    headers=_rpc_headers(Host="evil.example"))
                assert bad_host.status_code == 421

    asyncio.run(check())
