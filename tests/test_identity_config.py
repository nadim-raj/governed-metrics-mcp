from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from governed_metrics.config import JwtSettings, is_loopback, load_settings
from governed_metrics.identity import JwtVerifier, current_caller

from .conftest import make_settings

ISSUER, AUDIENCE = "https://idp.example.com/", "governed-metrics"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    """Stands in for PyJWKClient: always returns our test public key."""

    class _Key:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._Key()


def jwt_settings(**kw):
    return JwtSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url="https://idp.example.com/jwks",
                       allowed_domains=kw.pop("allowed_domains", ("example.com",)), **kw)


def token(**claims):
    body = {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300, "email": "Ana@Example.com",
            "email_verified": True, "azp": "client-1", "scope": "metrics:read"}
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, KEY, algorithm="RS256")


@pytest.fixture
def verifier():
    return JwtVerifier(jwt_settings(), jwks_client=FakeJwks())


def test_a_valid_token_yields_the_normalised_identity(verifier):
    access = verifier.verify(token())
    assert access.subject == "ana@example.com" and access.client_id == "client-1"
    assert access.scopes == ["metrics:read"]


@pytest.mark.parametrize("claims", [
    {"aud": "someone-else"},
    {"iss": "https://evil.example.net/"},
    {"exp": int(time.time()) - 3600},
    {"exp": None},
    {"email": None},
    {"email_verified": False},
    {"email": "ana@elsewhere.example.net"},
])
def test_invalid_tokens_are_rejected(verifier, claims):
    assert verifier.verify(token(**claims)) is None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_key_confusion_forgery_is_rejected(verifier):
    """The classic attack: HMAC-sign a token with the server's own *public* key and claim
    HS256, hoping the verifier uses the public key as a shared secret."""
    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60,
                               "email": "attacker@example.com"}).encode())
    signature = _b64(hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    assert verifier.verify(f"{header}.{payload}.{signature}") is None


def test_unsigned_and_malformed_tokens_are_rejected(verifier):
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60,
                           "email": "x@example.com"}, None, algorithm="none")
    assert verifier.verify(unsigned) is None
    assert verifier.verify("not.a.token") is None


def test_shared_secret_algorithms_cannot_be_configured():
    with pytest.raises(ValueError, match="asymmetric"):
        JwtVerifier(jwt_settings(algorithms=("HS256",)), jwks_client=FakeJwks())


def test_stdio_caller_is_the_configured_local_user(tmp_path):
    assert current_caller(make_settings(tmp_path, local_user="lead@example.com")) == "lead@example.com"
    assert current_caller(make_settings(tmp_path, transport="streamable-http")) == ""


# ---- configuration -------------------------------------------------------------------------

def http(tmp_path, **kw):
    base = make_settings(tmp_path, transport="streamable-http", auth_mode="jwt", jwt=jwt_settings(),
                         public_url="https://metrics.example.com")
    return replace(base, **kw)


def test_a_sound_http_configuration_has_no_problems(tmp_path):
    assert http(tmp_path).problems() == []


@pytest.mark.parametrize("overrides,problem", [
    ({"auth_mode": "none"}, "requires GM_AUTH_MODE=jwt"),
    ({"jwt": None}, "needs GM_JWT_ISSUER"),
    ({"host": "0.0.0.0"}, "is not loopback"),
    ({"public_url": "http://metrics.example.com"}, "must be https"),
    ({"proxy_secret": "x" * 40}, "set both GM_PROXY_SECRET and GM_CSRF_SECRET"),
    ({"proxy_secret": "short", "csrf_secret": "short"}, "at least 32 characters"),
    ({"transport": "sse"}, "must be 'stdio' or 'streamable-http'"),
    ({"adhoc_row_cap_default": 5000}, "must not exceed"),
])
def test_weakening_configurations_refuse_to_start(tmp_path, overrides, problem):
    assert any(problem in p for p in http(tmp_path, **overrides).problems())


def test_jwks_must_be_https(tmp_path):
    s = http(tmp_path, jwt=replace(jwt_settings(), jwks_url="http://idp.example.com/jwks"))
    assert any("must be https" in p for p in s.problems())


def test_container_bind_needs_an_explicit_opt_in(tmp_path):
    assert http(tmp_path, host="0.0.0.0", allow_non_loopback_bind=True).problems() == []


def test_dsn_is_read_on_demand_and_never_defaulted(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    monkeypatch.delenv("GM_DSN_UNIT_A", raising=False)
    with pytest.raises(LookupError, match="GM_DSN_UNIT_A"):
        s.dsn_for("unit_a")
    monkeypatch.setenv("GM_DSN_UNIT_A", "postgresql://role@host/db")
    assert s.dsn_for("unit_a") == "postgresql://role@host/db"
    assert "postgresql" not in repr(s)


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("GM_PORT", "9000")
    monkeypatch.setenv("GM_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("GM_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("GM_JWT_JWKS_URL", "https://idp.example.com/jwks")
    monkeypatch.setenv("GM_JWT_ALGORITHMS", "RS256,EdDSA")
    s = load_settings()
    assert s.port == 9000 and s.jwt.algorithms == ("RS256", "EdDSA")
    monkeypatch.setenv("GM_PORT", "nope")
    with pytest.raises(ValueError, match="GM_PORT"):
        load_settings()


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("::1", True), ("localhost", True),
                                           ("0.0.0.0", False), ("metrics.example.com", False)])
def test_loopback_detection(host, expected):
    assert is_loopback(host) is expected
