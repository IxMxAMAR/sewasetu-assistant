"""Everything the two servers share: auth wiring, the caller's portal handle,
and the way the process is started.

Keeping this in one place is what makes the pair a pair. If the officer server
ever grows a different idea of what a valid token is, that is a bug that should
be visible in a diff of one file, not hidden in two copies of the same code.
"""

import asyncio
import logging

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from .auth import PortalTokenVerifier
from .portal import Portal, PortalError
from .settings import Settings

log = logging.getLogger("sewasetu")

# A citizen may send an age proof of up to 5 MB, which is about 6.8 MB once
# base64-encoded. The transport limit has to sit above the scheme's own limit or
# the portal's rule would never get a chance to be the one that applies.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

NOT_SIGNED_IN = ("Nobody is signed in to the portal, so nothing can be read or filed. "
                 "The person has to sign in on the portal first; the assistant cannot "
                 "sign in for them and must never ask for their OTP or password.")


def build_server(settings: Settings, title: str, instructions: str) -> MCPServer:
    verifier = PortalTokenVerifier(settings.portal_base_url, settings.public_url, settings.role)
    return MCPServer(
        name="sewasetu-" + settings.role,
        title=title,
        instructions=instructions,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.portal_base_url,
            resource_server_url=settings.public_url,
            required_scopes=[settings.role],
            # Audience is checked inside the verifier, which accepts the resource
            # URL with or without the transport path; the middleware's own check
            # is exact and would reject the /mcp form that some clients send.
            validate_token_resource=False,
            service_documentation_url=settings.portal_base_url + "/about",
        ),
    )


def caller_portal(settings: Settings) -> Portal:
    """The portal, as the person who is signed in.

    The token comes from the request the transport authenticated, never from a
    tool argument. That is the whole reason an assistant cannot be talked into
    acting for somebody else.
    """
    token = get_access_token()
    if token is None:
        raise PortalError(401, {"message": NOT_SIGNED_IN})
    return Portal(settings.portal_base_url, token.token)


def serve(server: MCPServer, settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("%s listening on %s:%d%s (public URL %s, portal %s)",
             server.name, settings.host, settings.port, settings.path,
             settings.public_url, settings.portal_base_url)
    asyncio.run(server.run_streamable_http_async(
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.path,
        json_response=True,
        # No server-side session state: every request carries its own bearer
        # token and is answered on its own. A citizen on a shared phone or a
        # kiosk should not be able to inherit somebody else's session by
        # reconnecting.
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
    ))
