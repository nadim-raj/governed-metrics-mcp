"""Who is asking: the verified identity every authorisation decision starts from.

The server trusts only an identity it verified itself, never one a caller supplies. Request
text is untrusted even when the caller is trusted (docs/security.md), so nothing in a tool's
arguments can change who the caller is.

* HTTP transport: the MCP endpoint is an OAuth 2.0 resource server. Clients obtain a token
  from the organisation's identity provider; this module verifies its signature against the
  provider's published keys (JWKS), its issuer, audience and expiry, and reads the identity
  claim. Asymmetric algorithms only. `none` and shared-secret algorithms are never accepted.
* stdio transport: a single local operator, identified by GM_LOCAL_USER. The access policy
  is still enforced, so a local run is exactly as narrow as that user's grant.
"""
from __future__ import annotations

import logging

import anyio
import jwt
from mcp.server.auth.provider import AccessToken

from .config import JwtSettings, Settings

log = logging.getLogger("governed_metrics.identity")

ASYMMETRIC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                                   "ES256", "ES384", "ES512", "EDDSA"})
CLOCK_SKEW_S = 30

try:
    from mcp.server.auth.middleware.auth_context import get_access_token
except ImportError:  # pragma: no cover - depends on the installed SDK
    def get_access_token():
        return None


class JwtVerifier:
    """mcp.server.auth.provider.TokenVerifier backed by the identity provider's JWKS."""

    def __init__(self, settings: JwtSettings, jwks_client: jwt.PyJWKClient | None = None):
        bad = {a for a in settings.algorithms if a.upper() not in ASYMMETRIC_ALGORITHMS}
        if bad or not settings.algorithms:
            raise ValueError(f"unsupported JWT algorithms {sorted(bad)}; asymmetric algorithms only")
        self.settings = settings
        self._jwks = jwks_client or jwt.PyJWKClient(settings.jwks_url, cache_keys=True,
                                                    lifespan=300, timeout=5)

    async def verify_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self.verify, token)

    def verify(self, token: str) -> AccessToken | None:
        s = self.settings
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=list(s.algorithms), audience=s.audience,
                                issuer=s.issuer, leeway=CLOCK_SKEW_S,
                                options={"require": ["exp", "iss", "aud"]})
        except (jwt.PyJWTError, ValueError) as e:
            log.info("token rejected: %s", e.__class__.__name__)
            return None
        identity = claims.get(s.identity_claim)
        if not isinstance(identity, str) or not identity.strip():
            log.info("token rejected: no %s claim", s.identity_claim)
            return None
        identity = identity.strip().lower()
        if s.identity_claim == "email" and claims.get("email_verified") is False:
            log.info("token rejected: email not verified")
            return None
        if s.allowed_domains and identity.rpartition("@")[2] not in s.allowed_domains:
            log.info("token rejected: identity domain not allowed")
            return None
        scope = claims.get("scope") or claims.get("scp") or []
        scopes = scope.split() if isinstance(scope, str) else [str(x) for x in scope]
        return AccessToken(token=token, client_id=str(claims.get("azp") or claims.get("client_id") or "unknown"),
                           scopes=scopes, expires_at=int(claims["exp"]), subject=identity,
                           claims={"iss": claims.get("iss")})


def current_caller(settings: Settings) -> str:
    """The verified identity of the current request, or '' when there is none. An empty
    identity matches no policy entry, so it is denied everything."""
    if settings.http:
        try:
            token = get_access_token()
        except Exception:   # no auth context means no identity, never a guessed one
            return ""
        return (getattr(token, "subject", None) or "").strip().lower()
    return settings.local_user
