"""Deployment settings for the Sewa Setu MCP servers.

Everything here comes from the environment because the two servers are the same
code deployed twice with different names, ports and roles. Nothing is defaulted
for the values that matter: a server that does not know its own public URL
cannot check the audience of a token, and a server that cannot check the
audience is not safe to expose, so it refuses to start.
"""

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    portal_base_url: str    # the department's portal, e.g. https://<host>-8000.proxy.runpod.net
    public_url: str         # this server's own canonical URL, used as the token audience
    role: str               # "citizen" or "officer"
    host: str
    port: int
    path: str               # the streamable-HTTP path, "/mcp"
    log_level: str


def load(role: str, default_port: int) -> Settings:
    portal = (os.environ.get("PORTAL_BASE_URL") or "").rstrip("/")
    public = (os.environ.get("MCP_PUBLIC_URL") or "").rstrip("/")
    if not portal:
        raise ConfigError(
            "PORTAL_BASE_URL is not set. Point it at the portal this server acts for, "
            "for example https://sewasetu.example.in")
    if not public:
        raise ConfigError(
            "MCP_PUBLIC_URL is not set. It must be the address at which assistants "
            "reach this server, because that address is the audience its tokens carry.")
    return Settings(
        portal_base_url=portal,
        public_url=public,
        role=role,
        host=os.environ.get("MCP_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", default_port)),
        path=os.environ.get("MCP_PATH", "/mcp"),
        log_level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
    )
