from __future__ import annotations

import re
from dataclasses import replace

import anyio
import pytest
from starlette.testclient import TestClient

from governed_metrics import overlay
from governed_metrics.admin import Console
from governed_metrics.runtime import Runtime
from governed_metrics.server import build_server, deep_check

from .fakes import TODAY

PROXY_SECRET = "p" * 40
CSRF_SECRET = "c" * 40
ORIGIN = "https://metrics.example.com"
EXPECTED_TOOLS = {"list_metrics", "get_metric", "get_metric_breakdown", "get_composite_metric",
                  "compare_periods", "list_rules", "resolve_rule", "get_glossary", "list_sources",
                  "describe_source", "run_query", "reload_knowledge"}


def test_tool_surface_and_annotations(rt):
    mcp = build_server(rt)
    tools = {t.name: t for t in anyio.run(mcp.list_tools)}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["get_metric"].annotations.readOnlyHint is True
    assert tools["reload_knowledge"].annotations.readOnlyHint is False
    assert "start" in tools["get_metric"].inputSchema["properties"]


def test_tools_run_through_the_governed_path(rt):
    mcp = build_server(rt)

    async def call():
        return await mcp.call_tool("get_metric", {"metric": "orders", "unit": "unit_a",
                                                  "start": "2026-01-01", "end": "2026-02-01"})
    result = anyio.run(call)
    structured = result[1] if isinstance(result, tuple) else result.structuredContent
    assert structured["kind"] == "metric" and structured["value"] == 100
    assert list(rt.audit.read())[-1]["tool"] == "get_metric"


def test_health_and_readiness(rt, warehouse):
    client = TestClient(build_server(rt).streamable_http_app())
    assert client.get("/healthz").json() == {"status": "ok", "knowledge_version": "test"}
    assert client.get("/readyz").status_code == 200
    warehouse.pings["unit_b"] = False
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["warehouse"] == {"unit_a": True, "unit_b": False}
    assert deep_check(rt)["policy_ok"] is True


def test_console_is_absent_without_its_secrets(rt):
    client = TestClient(build_server(rt).streamable_http_app())
    assert client.get("/admin").status_code == 404


# ---- console -------------------------------------------------------------------------------

@pytest.fixture
def console_rt(settings, warehouse, kb):
    s = replace(settings, proxy_secret=PROXY_SECRET, csrf_secret=CSRF_SECRET, public_url=ORIGIN)
    return Runtime(s, warehouse, knowledge=kb, today=lambda: TODAY)


@pytest.fixture
def client(console_rt):
    return TestClient(build_server(console_rt).streamable_http_app(), follow_redirects=False)


def as_admin(user="admin@example.com", **extra):
    return {"x-proxy-secret": PROXY_SECRET, "x-forwarded-email": user, **extra}


def csrf_from(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_console_requires_the_proxy_secret_and_an_admin(client):
    assert client.get("/admin", headers={"x-forwarded-email": "admin@example.com"}).status_code == 404
    assert client.get("/admin", headers=as_admin(**{"x-proxy-secret": "wrong" * 8})).status_code == 404
    assert client.get("/admin", headers=as_admin("lead@example.com")).status_code == 403


def test_console_page_is_locked_down(client):
    r = client.get("/admin", headers=as_admin())
    assert r.status_code == 200
    assert "script-src" not in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert "<script" not in r.text


def test_grant_round_trip(client, console_rt):
    token = csrf_from(client.get("/admin", headers=as_admin()).text)
    r = client.post("/admin/grant", headers=as_admin(origin=ORIGIN),
                    data={"csrf": token, "user": "New@Example.com", "units": ["unit_b"], "reason": "joined"})
    assert r.status_code == 303 and r.headers["location"] == "/admin?r=granted"
    assert console_rt.current_policy().units_for("new@example.com") == {"unit_b"}
    assert overlay.effective(console_rt.settings.overlay_path)["new@example.com"]["by"] == "admin@example.com"
    assert list(console_rt.audit.read())[-1]["tool"] == "admin.grant"


@pytest.mark.parametrize("headers,data", [
    ({}, {"user": "x@example.com", "units": "unit_a", "reason": "r"}),                      # no Origin
    ({"origin": "https://evil.example.net"}, {"user": "x@example.com", "units": "unit_a", "reason": "r"}),
    ({"origin": ORIGIN}, {"csrf": "123.abc", "user": "x@example.com", "units": "unit_a", "reason": "r"}),
])
def test_forged_posts_are_refused(client, console_rt, headers, data):
    if "csrf" not in data:
        data = {**data, "csrf": csrf_from(client.get("/admin", headers=as_admin()).text)}
    r = client.post("/admin/grant", headers=as_admin(**headers), data=data)
    assert r.status_code == 403
    assert overlay.effective(console_rt.settings.overlay_path) == {}


def test_a_token_is_bound_to_the_admin_it_was_issued_to(console_rt):
    console = Console(console_rt)
    token = console.csrf_token("admin@example.com", now=1_000_000)
    assert console.csrf_ok("admin@example.com", token, now=1_000_100)
    assert not console.csrf_ok("other@example.com", token, now=1_000_100)
    assert not console.csrf_ok("admin@example.com", token, now=1_000_000 + 7200)
    assert not console.csrf_ok("admin@example.com", "garbage", now=1_000_000)


def test_invalid_form_input_changes_nothing(client, console_rt):
    token = csrf_from(client.get("/admin", headers=as_admin()).text)
    for data, outcome in (({"user": "not-an-email", "units": "unit_a", "reason": "r"}, "invalid_user"),
                          ({"user": "a@example.com", "units": "unit_z", "reason": "r"}, "invalid_units"),
                          ({"user": "a@example.com", "units": "unit_a", "reason": ""}, "invalid_reason")):
        r = client.post("/admin/grant", headers=as_admin(origin=ORIGIN), data={"csrf": token, **data})
        assert r.headers["location"] == f"/admin?r={outcome}"
    assert overlay.effective(console_rt.settings.overlay_path) == {}


def test_oversized_and_wrong_type_bodies_are_refused(client):
    token = csrf_from(client.get("/admin", headers=as_admin()).text)
    big = client.post("/admin/grant", headers=as_admin(origin=ORIGIN),
                      data={"csrf": token, "user": "a@example.com", "reason": "x" * 10_000})
    assert big.status_code == 413
    as_json = client.post("/admin/grant", headers=as_admin(origin=ORIGIN), json={"csrf": token})
    assert as_json.status_code == 403


def test_console_output_is_escaped_and_messages_are_not_reflected(client, console_rt):
    overlay.grant(console_rt.settings.overlay_path, "x@example.com", ["unit_a"], by="admin@example.com",
                  reason="<script>alert(1)</script>")
    page = client.get("/admin?r=<b>pwned</b>", headers=as_admin()).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page
    assert "pwned" not in page


def test_revoke_round_trip(client, console_rt):
    token = csrf_from(client.get("/admin", headers=as_admin()).text)
    r = client.post("/admin/revoke", headers=as_admin(origin=ORIGIN),
                    data={"csrf": token, "user": "lead@example.com", "reason": "left the team"})
    assert r.headers["location"] == "/admin?r=revoked"
    assert console_rt.current_policy().units_for("lead@example.com") == frozenset()


def test_an_admin_cannot_grant_themselves_access(client, console_rt):
    token = csrf_from(client.get("/admin", headers=as_admin()).text)
    r = client.post("/admin/grant", headers=as_admin(origin=ORIGIN),
                    data={"csrf": token, "user": "admin@example.com", "units": "unit_a", "reason": "self"})
    assert r.headers["location"] == "/admin?r=self_grant"
    assert overlay.effective(console_rt.settings.overlay_path) == {}


def test_chunked_bodies_are_size_limited_too(client):
    token = csrf_from(client.get("/admin", headers=as_admin()).text)

    def body():
        yield f"csrf={token}&user=a%40example.com&reason=".encode()
        for _ in range(20):
            yield b"x" * 1024

    r = client.post("/admin/grant", headers=as_admin(origin=ORIGIN, **{"content-type": "application/x-www-form-urlencoded"}),
                    content=body())
    assert r.status_code == 413
