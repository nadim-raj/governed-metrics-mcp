"""Runtime settings, read once from the environment into an immutable object.

Secrets never have defaults and never appear in this file: warehouse credentials are one
DSN per unit (`GM_DSN_<UNIT>`), each for that unit's own read-only role (ADR 0006), and
are read only when that unit is first queried. A unit with no DSN cannot be queried at all.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

_LOOPBACK_NAMES = frozenset({"localhost"})


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _csv(name: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in _str(name).split(",") if p.strip())


def _state_path(name: str, filename: str) -> str:
    """State files live outside the deployed tree, so a deploy can never overwrite them."""
    return os.path.expanduser(_str(name) or os.path.join("~", ".governed-metrics", filename))


def is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class JwtSettings:
    issuer: str
    audience: str
    jwks_url: str
    identity_claim: str = "email"
    allowed_domains: tuple[str, ...] = ()
    algorithms: tuple[str, ...] = ("RS256", "ES256")


@dataclass(frozen=True)
class Settings:
    knowledge_path: str
    policy_path: str
    overlay_path: str
    audit_path: str
    transport: str
    host: str
    port: int
    public_url: str
    local_user: str
    statement_timeout_ms: int
    adhoc_row_cap_default: int
    adhoc_row_cap_max: int
    breakdown_row_cap: int
    cache_ttl_s: int
    freshness_ttl_s: int
    adhoc_rate_per_minute: int
    max_parallel_legs: int
    auth_mode: str
    jwt: JwtSettings | None
    proxy_secret: str = field(repr=False, default="")
    proxy_user_header: str = "x-forwarded-email"
    csrf_secret: str = field(repr=False, default="")
    allow_non_loopback_bind: bool = False

    def dsn_for(self, unit: str) -> str:
        """The read-only DSN for one unit's role. Read on demand, never cached on the object,
        so a credential is never part of a repr, a log line or a pickled settings object."""
        dsn = os.environ.get(f"GM_DSN_{unit.upper()}", "").strip()
        if not dsn:
            raise LookupError(f"no warehouse credential configured for unit '{unit}' "
                              f"(set GM_DSN_{unit.upper()})")
        return dsn

    @property
    def http(self) -> bool:
        return self.transport != "stdio"

    def problems(self) -> list[str]:
        """Configurations that must stop the server from starting. Fail closed: an HTTP
        server without authentication would expose the data tools to anyone who can reach it."""
        out = []
        if self.transport not in ("stdio", "streamable-http"):
            out.append(f"GM_TRANSPORT must be 'stdio' or 'streamable-http', got {self.transport!r}")
        if self.http and self.auth_mode != "jwt":
            out.append("HTTP transport requires GM_AUTH_MODE=jwt; an unauthenticated HTTP "
                       "listener is refused")
        if self.auth_mode not in ("none", "jwt"):
            out.append(f"GM_AUTH_MODE must be 'none' or 'jwt', got {self.auth_mode!r}")
        if self.auth_mode == "jwt" and self.jwt is None:
            out.append("GM_AUTH_MODE=jwt needs GM_JWT_ISSUER, GM_JWT_AUDIENCE and GM_JWT_JWKS_URL")
        if self.jwt is not None and not self.jwt.jwks_url.startswith("https://"):
            out.append("GM_JWT_JWKS_URL must be https")
        if self.http and not is_loopback(self.host) and not self.allow_non_loopback_bind:
            out.append(f"GM_HOST={self.host} is not loopback; the reverse proxy is the only public "
                       "listener. Set GM_ALLOW_NON_LOOPBACK_BIND=1 only inside a container network.")
        if self.http and not self.public_url.startswith("https://") and \
                not is_loopback(urlparse(self.public_url).hostname or ""):
            out.append("GM_PUBLIC_URL must be https outside local development")
        if self.adhoc_row_cap_default > self.adhoc_row_cap_max:
            out.append("GM_ADHOC_ROW_CAP_DEFAULT must not exceed GM_ADHOC_ROW_CAP_MAX")
        if bool(self.proxy_secret) != bool(self.csrf_secret):
            out.append("set both GM_PROXY_SECRET and GM_CSRF_SECRET to enable the admin console, "
                       "or neither")
        for name, secret in (("GM_PROXY_SECRET", self.proxy_secret),
                             ("GM_CSRF_SECRET", self.csrf_secret)):
            if secret and len(secret) < 32:
                out.append(f"{name} must be at least 32 characters")
        return out


def _jwt_from_env() -> JwtSettings | None:
    issuer, audience, jwks = _str("GM_JWT_ISSUER"), _str("GM_JWT_AUDIENCE"), _str("GM_JWT_JWKS_URL")
    if not (issuer and audience and jwks):
        return None
    algorithms = tuple(a.strip() for a in _str("GM_JWT_ALGORITHMS").split(",") if a.strip())
    return JwtSettings(issuer=issuer, audience=audience, jwks_url=jwks,
                       identity_claim=_str("GM_JWT_IDENTITY_CLAIM", "email"),
                       allowed_domains=_csv("GM_ALLOWED_EMAIL_DOMAINS"),
                       algorithms=algorithms or ("RS256", "ES256"))


def load_settings() -> Settings:
    port = _int("GM_PORT", 8080, minimum=1)
    host = _str("GM_HOST", "127.0.0.1")
    return Settings(
        knowledge_path=os.path.abspath(_str("GM_KNOWLEDGE_PATH", "examples/knowledge")),
        policy_path=os.path.abspath(_str("GM_POLICY_PATH", "examples/access.yaml")),
        overlay_path=_state_path("GM_OVERLAY_PATH", "overlay.jsonl"),
        audit_path=_state_path("GM_AUDIT_LOG", "audit.jsonl"),
        transport=_str("GM_TRANSPORT", "stdio"),
        host=host,
        port=port,
        public_url=_str("GM_PUBLIC_URL", f"http://{host}:{port}").rstrip("/"),
        local_user=_str("GM_LOCAL_USER").lower(),
        statement_timeout_ms=_int("GM_STATEMENT_TIMEOUT_MS", 30_000, minimum=100),
        adhoc_row_cap_default=_int("GM_ADHOC_ROW_CAP_DEFAULT", 200, minimum=1),
        adhoc_row_cap_max=_int("GM_ADHOC_ROW_CAP_MAX", 2_000, minimum=1),
        breakdown_row_cap=_int("GM_BREAKDOWN_ROW_CAP", 1_000, minimum=1),
        cache_ttl_s=_int("GM_CACHE_TTL_S", 300),
        freshness_ttl_s=_int("GM_FRESHNESS_TTL_S", 60),
        adhoc_rate_per_minute=_int("GM_ADHOC_RATE_PER_MINUTE", 60),
        max_parallel_legs=_int("GM_MAX_PARALLEL_LEGS", 8, minimum=1),
        auth_mode=_str("GM_AUTH_MODE", "none").lower(),
        jwt=_jwt_from_env(),
        proxy_secret=_str("GM_PROXY_SECRET"),
        proxy_user_header=_str("GM_PROXY_USER_HEADER", "x-forwarded-email").lower(),
        csrf_secret=_str("GM_CSRF_SECRET"),
        allow_non_loopback_bind=_str("GM_ALLOW_NON_LOOPBACK_BIND") == "1",
    )
