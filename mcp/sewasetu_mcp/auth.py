"""Bearer-token verification for the Sewa Setu MCP servers.

The portal is the authorization server. It issues short-lived RS256 access
tokens whose `aud` is the canonical URL of the service they were minted for
(RFC 8707 resource indicators), and it publishes the public half of its signing
key at /.well-known/jwks.json.

So these servers hold no secret that belongs to the portal. They fetch the
public keys and verify the signature themselves. Two consequences the
department asked for:

  * A token minted for the citizen assistant is useless at the officer
    assistant, because the audience does not match. The two servers are
    separate resources, not one resource with a role check inside it.
  * Revoking a sign-in at the portal takes effect here within one access-token
    lifetime (20 minutes for a citizen, 30 for an officer) without these
    servers having to be told anything.
"""

import logging

import jwt
from jwt import PyJWKClient

from mcp.server.auth.provider import AccessToken, TokenVerifier

log = logging.getLogger("sewasetu.auth")

# PyJWKClient fetches the key set with urllib, whose default user agent is
# "Python-urllib/3.11". Anything sitting in front of the portal that filters
# obvious bots -- Cloudflare, which is in front of our own deployment -- answers
# that with 403, and every token is then refused with "HTTP Error 403: Forbidden",
# which reads like a permissions bug and is not one. Say who we are instead.
USER_AGENT = "sewasetu-mcp/1.0 (+https://github.com/IxMxAMAR/sewasetu-assistant)"


class PortalTokenVerifier(TokenVerifier):
    def __init__(self, portal_base_url: str, resource_url: str, role: str,
                 jwks_url: str | None = None):
        self.issuer = portal_base_url.rstrip("/")
        self.resource_url = resource_url.rstrip("/")
        self.role = role
        # The issuer is checked against the portal's public name; the keys are
        # fetched wherever this process can actually reach the portal.
        self._jwks = PyJWKClient(jwks_url or self.issuer + "/.well-known/jwks.json",
                                 cache_keys=True, lifespan=300,
                                 headers={"User-Agent": USER_AGENT})

    def _audience_matches(self, aud) -> bool:
        """Accept the resource URL with or without the transport path.

        MCP clients send `resource` as the endpoint they are talking to, which
        may be `https://host` or `https://host/mcp` depending on the client. Both
        name the same server, so both are honoured; nothing else is.
        """
        candidates = aud if isinstance(aud, list) else [aud]
        accepted = {self.resource_url, self.resource_url + "/mcp"}
        return any(isinstance(a, str) and a.rstrip("/") in accepted for a in candidates)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=["RS256"],
                issuer=self.issuer, options={"verify_aud": False},
                # The portal stamps iat from its own clock, which is a second or
                # two off any given client's; without leeway a token minted a
                # moment ago is rejected as "not yet valid" and the failure looks
                # like an authorization bug rather than a clock.
                leeway=60,
            )
        except Exception as exc:                      # noqa: BLE001 - any failure is a refusal
            log.info("token refused: %s", exc)
            return None

        if not self._audience_matches(claims.get("aud")):
            log.info("token refused: audience %r is not this server", claims.get("aud"))
            return None
        if claims.get("role") != self.role or claims.get("scope") != self.role:
            log.info("token refused: role %r cannot use the %s server",
                     claims.get("role"), self.role)
            return None

        subject = claims.get("sub") or ""
        if not subject.startswith(self.role + ":"):
            log.info("token refused: subject %r does not match role %s", subject, self.role)
            return None

        return AccessToken(
            token=token,
            client_id=claims.get("client_id") or "",
            scopes=[self.role],
            expires_at=claims.get("exp"),
            resource=self.resource_url,
            subject=subject,
            claims=claims,
        )
