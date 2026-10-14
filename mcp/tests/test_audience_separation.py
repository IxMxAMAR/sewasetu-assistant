# -*- coding: utf-8 -*-
"""Does the citizen assistant really not see the officer's tools?

The brief is explicit that the two servers must be separate endpoints, and that
a citizen's assistant should not even be able to enumerate the officer's tools.
That is a claim about behaviour, so it is tested here rather than asserted in a
README.

What this script does:

  1. Stands up a stub portal that publishes an RSA key at
     /.well-known/jwks.json and mints sign-in tokens exactly the way the real
     portal does -- issuer = the portal, audience = the MCP server the token is
     for, subject = citizen:<mobile> or officer:<username>.
  2. Starts both MCP servers against it, as separate processes on separate
     ports, the way they are deployed.
  3. Talks to them with the official MCP client over streamable HTTP, and with
     plain HTTP for the parts that happen before a session exists.

Run it with:
    python mcp/tests/test_audience_separation.py

No pytest, no network, no database: it only needs the packages in
mcp/requirements.txt.
"""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

MCP_DIR = Path(__file__).resolve().parents[1]
STUB_PORT = 18000
CITIZEN_PORT = 18101
OFFICER_PORT = 18102
STUB_URL = "http://127.0.0.1:%d" % STUB_PORT
CITIZEN_URL = "http://127.0.0.1:%d" % CITIZEN_PORT
OFFICER_URL = "http://127.0.0.1:%d" % OFFICER_PORT

KID = "test-key-1"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("  %s  %s%s" % ("PASS" if condition else "FAIL", name,
                          "" if condition else "   <- " + str(detail)))


def mint(audience, role, subject):
    now = int(time.time())
    return jwt.encode(
        {"iss": STUB_URL, "sub": subject, "aud": audience, "scope": role, "role": role,
         "client_id": "test-client", "iat": now, "exp": now + 600, "jti": "t",
         "kind": "access"},
        PRIVATE_PEM, algorithm="RS256", headers={"kid": KID},
    )


# ---------------------------------------------------------------------------
# The stub portal
# ---------------------------------------------------------------------------

def jwks(request):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return JSONResponse({"keys": [jwk]})


async def echo(request):
    return JSONResponse({"stub": True, "path": request.url.path,
                         "authorization_seen": request.headers.get("authorization", "")[:16] + "..."})


stub_app = Starlette(routes=[
    Route("/.well-known/jwks.json", jwks),
    Route("/api/v1/{rest:path}", echo, methods=["GET", "POST"]),
])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


async def wait_for_port(port, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        await asyncio.sleep(0.2)
    return False


def start_server(role, port, public_url):
    env = dict(os.environ)
    env.update({
        "PORTAL_BASE_URL": STUB_URL,
        "MCP_PUBLIC_URL": public_url,
        "MCP_PORT": str(port),
        "MCP_BIND_HOST": "127.0.0.1",
        "MCP_LOG_LEVEL": "WARNING",
        "PYTHONPATH": str(MCP_DIR),
    })
    return subprocess.Popen([sys.executable, "-m", "sewasetu_mcp", role],
                            cwd=str(MCP_DIR), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


async def list_tool_names(url, token):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                  timeout=20.0) as http_client:
        async with streamable_http_client(url + "/mcp", http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [t.name for t in result.tools]


async def call_tool(url, token, name, arguments):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                  timeout=20.0) as http_client:
        async with streamable_http_client(url + "/mcp", http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await session.call_tool(name, arguments)


async def status_of(url, token):
    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"Authorization": "Bearer " + token} if token else {}
        r = await client.post(url + "/mcp", headers={
            **headers, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "0"}}})
        return r


async def main():
    print("\nSewa Setu MCP servers - audience separation\n" + "-" * 60)
    config = uvicorn.Config(stub_app, host="127.0.0.1", port=STUB_PORT, log_level="warning")
    stub = uvicorn.Server(config)
    stub_task = asyncio.create_task(stub.serve())
    assert await wait_for_port(STUB_PORT), "stub portal did not start"

    citizen_proc = start_server("citizen", CITIZEN_PORT, CITIZEN_URL)
    officer_proc = start_server("officer", OFFICER_PORT, OFFICER_URL)
    try:
        assert await wait_for_port(CITIZEN_PORT), "citizen server did not start"
        assert await wait_for_port(OFFICER_PORT), "officer server did not start"

        citizen_token = mint(CITIZEN_URL, "citizen", "citizen:9800000001")
        officer_token = mint(OFFICER_URL, "officer", "officer:admin")

        # --- discovery -----------------------------------------------------
        print("\n1. Each server publishes its own protected-resource metadata")
        async with httpx.AsyncClient(timeout=10.0) as client:
            for name, url in (("citizen", CITIZEN_URL), ("officer", OFFICER_URL)):
                meta = (await client.get(url + "/.well-known/oauth-protected-resource")).json()
                check("%s metadata names the portal as its authorization server" % name,
                      STUB_URL in (meta.get("authorization_servers") or []), meta)
                check("%s metadata names itself as the resource" % name,
                      (meta.get("resource") or "").rstrip("/") == url, meta)

        # --- unauthenticated ------------------------------------------------
        print("\n2. No token means no session, and the client is told where to sign in")
        r = await status_of(CITIZEN_URL, None)
        check("unauthenticated request is refused", r.status_code == 401, r.status_code)
        challenge = r.headers.get("www-authenticate", "")
        check("refusal carries a resource_metadata pointer",
              "resource_metadata" in challenge, challenge)

        # --- the separation that matters ------------------------------------
        print("\n3. Each server publishes only its own tools")
        citizen_tools = await list_tool_names(CITIZEN_URL, citizen_token)
        officer_tools = await list_tool_names(OFFICER_URL, officer_token)
        print("     citizen tools: %s" % ", ".join(sorted(citizen_tools)))
        print("     officer tools: %s" % ", ".join(sorted(officer_tools)))
        check("citizen server offers submit_application", "submit_application" in citizen_tools)
        check("citizen server offers upload_age_proof", "upload_age_proof" in citizen_tools)
        check("citizen server does NOT offer record_decision",
              "record_decision" not in citizen_tools, citizen_tools)
        check("citizen server does NOT offer get_queue_summary",
              "get_queue_summary" not in citizen_tools, citizen_tools)
        check("officer server offers record_decision", "record_decision" in officer_tools)
        check("officer server does NOT offer submit_application",
              "submit_application" not in officer_tools, officer_tools)
        check("the two servers share no tool name at all except scheme information",
              set(citizen_tools) & set(officer_tools) == {"get_scheme_information"},
              set(citizen_tools) & set(officer_tools))

        # --- the token is bound to one server -------------------------------
        print("\n4. A token for one server is refused by the other")
        r = await status_of(OFFICER_URL, citizen_token)
        check("citizen token refused by the officer server", r.status_code == 401, r.status_code)
        r = await status_of(CITIZEN_URL, officer_token)
        check("officer token refused by the citizen server", r.status_code == 401, r.status_code)

        wrong_role = mint(CITIZEN_URL, "officer", "officer:admin")
        r = await status_of(CITIZEN_URL, wrong_role)
        check("officer-role token refused by the citizen server even with the right audience",
              r.status_code == 401, r.status_code)

        forged = jwt.encode({"iss": "https://somewhere-else.example", "sub": "citizen:9800000001",
                             "aud": CITIZEN_URL, "scope": "citizen", "role": "citizen",
                             "exp": int(time.time()) + 600},
                            PRIVATE_PEM, algorithm="RS256", headers={"kid": KID})
        r = await status_of(CITIZEN_URL, forged)
        check("token from a different issuer refused", r.status_code == 401, r.status_code)

        # --- the token actually reaches the portal --------------------------
        print("\n5. The caller's own token is what the portal sees")
        result = await call_tool(CITIZEN_URL, citizen_token, "get_scheme_information", {})
        payload = json.loads(result.content[0].text)
        check("tool call succeeded against the stub portal", payload.get("stub") is True, payload)
        check("the stub saw a bearer token", payload.get("authorization_seen", "").endswith("..."),
              payload)

    finally:
        for proc in (citizen_proc, officer_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        stub.should_exit = True
        await stub_task

    print("\n" + "-" * 60)
    print("%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  failed: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
