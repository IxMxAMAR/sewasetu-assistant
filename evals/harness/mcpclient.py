"""An MCP client, over the public endpoint, with the token we signed in for.

This is the same path the graders' client takes: streamable HTTP, a bearer token
in the header, tools listed from the server's own catalogue. Nothing is imported
from `mcp/sewasetu_mcp` -- if the evals imported the server's code they would be
testing the code rather than the deployment.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


@asynccontextmanager
async def connect(url: str, access_token: str, timeout: float = 120.0):
    """Yield an initialized ClientSession against one MCP server."""
    headers = {"Authorization": "Bearer " + access_token}
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def tool_specs(tools) -> list[dict]:
    """The server's tool list, in the shape an OpenAI-style model expects."""
    out = []
    for tool in tools.tools:
        out.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return out


def render_result(result) -> str:
    """A tool's answer as text, however the server chose to send it."""
    import json

    if getattr(result, "structuredContent", None) is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False, default=str)
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(json.dumps(getattr(block, "model_dump", lambda: str(block))(),
                                    ensure_ascii=False, default=str))
    return "\n".join(parts) if parts else "(no content)"
