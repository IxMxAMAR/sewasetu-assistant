"""A thin HTTP client for the portal's machine-facing API.

The MCP servers do not open a database connection. Everything they know and
everything they change goes through portal/api.py, carrying the caller's own
access token, so the portal applies its own rules and records its own audit
trail. This class exists to make that one sentence true and keep it obvious.

It is async because the tool handlers are async: a tool runs on the server's
event loop, and blocking that loop on a portal round-trip would make one slow
citizen everybody's problem.
"""

import logging

import httpx

log = logging.getLogger("sewasetu.portal")

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class PortalError(RuntimeError):
    """The portal refused the request. `message` is written for a person."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload or {}
        self.message = (self.payload.get("message")
                        or self.payload.get("error_description")
                        or "the portal refused the request")
        super().__init__(self.message)

    def as_result(self) -> dict:
        """The shape every tool returns when it could not do the thing."""
        out = {"ok": False, "message": self.message, "portal_status": self.status}
        for key in ("problems", "existing_application", "assistant_blocks"):
            if key in self.payload:
                out[key] = self.payload[key]
        return out


class Portal:
    def __init__(self, base_url: str, token: str, timeout: httpx.Timeout | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout or DEFAULT_TIMEOUT

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        url = self.base_url + path
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise PortalError(0, {"message": "the portal could not be reached (%s)" % exc})
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:400] or "the portal returned no answer"}
        if response.status_code >= 400:
            raise PortalError(response.status_code, payload)
        return payload

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self._call("GET", path, params=params or {})

    async def post(self, path: str, body: dict | None = None) -> dict:
        return await self._call("POST", path, json=body or {})
