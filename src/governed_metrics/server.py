"""MCP wiring: the tool surface, health checks and the admin console on one server.

Deterministic by design (ADR 0001): every tool here maps a request to a governed answer
through tools.py. No model runs in the request path, so the same call against the same
knowledge version returns the same number.

Transports:
  * stdio for local use, identified by GM_LOCAL_USER, with the policy still enforced;
  * streamable HTTP behind a TLS-terminating reverse proxy, as an OAuth resource server
    that verifies the identity provider's tokens (identity.py). It binds to loopback; the
    proxy is the only public listener.
"""
from __future__ import annotations

import functools
from typing import Any
from urllib.parse import urlparse

import anyio
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import tools as T
from .admin import register_admin
from .identity import JwtVerifier, current_caller
from .runtime import Runtime

INSTRUCTIONS = """\
Governed Metrics serves canonical business metrics. The same call returns the same number \
for everyone, stamped with the knowledge version that produced it.

- Start with list_metrics: it lists the metrics, measures, dimensions, presets and variants \
you may use. Never write SQL for a number that has a metric.
- Periods are half-open: start is inclusive, end is exclusive (January = start YYYY-01-01, \
end YYYY-02-01). Omit both for all-time.
- Governed rules come from resolve_rule (call list_rules first). Never state a rule value \
from memory; an unverified rule is refused, so say it is not published.
- A refusal is an answer. stale_source means the data is late, not zero: report the \
staleness and do not guess a number. access_denied means a grant is missing.
- run_query results are NON-CANONICAL. Say so wherever you use them.
- Personal fields are masked for most callers. Present masked values as returned and never \
try to reconstruct them.
- A breakdown with truncated=true is partial: do not add its rows up as a total. A value \
with maturity.settled=false is still moving.
"""

# All four hints are stated on every tool. An omitted hint is not the same as false:
# a host cannot distinguish "not destructive" from "unstated", so it has to assume the
# worst and warn about a read. Some directories reject a tool whose hints are incomplete.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
# Ad-hoc reads are still reads, but the caller supplies the statement, so the set of
# things it may touch is bounded by a grant rather than by a declaration.
ADHOC = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                        idempotentHint=True, openWorldHint=True)
# Reloading changes server state without destroying anything: invalid knowledge is
# rejected and the version already serving keeps serving.
ADMIN = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=True, openWorldHint=False)


def _transport_security(rt: Runtime) -> TransportSecuritySettings:
    s = rt.settings
    public = urlparse(s.public_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc, f"{s.host}:{s.port}", f"localhost:{s.port}"],
        allowed_origins=[f"{public.scheme}://{public.netloc}"])


def build_server(rt: Runtime) -> FastMCP:
    s = rt.settings
    extra: dict[str, Any] = {}
    if s.http:
        extra = {"token_verifier": JwtVerifier(s.jwt),
                 "auth": AuthSettings(issuer_url=s.jwt.issuer, resource_server_url=f"{s.public_url}/mcp"),
                 "transport_security": _transport_security(rt)}
    mcp = FastMCP("governed-metrics", instructions=INSTRUCTIONS, host=s.host, port=s.port, **extra)

    def tool(annotations: ToolAnnotations):
        """Register a tool whose body runs in a worker thread. FastMCP calls a synchronous
        tool on the event loop, so one slow warehouse query would stall every other request,
        health checks included. anyio copies context variables into the thread, so the
        verified identity is still visible to current_caller()."""
        def deco(fn):
            @functools.wraps(fn)
            async def offloaded(*args, **kwargs):
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
            mcp.tool(annotations=annotations, structured_output=True)(offloaded)
            return fn
        return deco

    def who() -> str:
        return current_caller(s)

    @tool(READ_ONLY)
    def list_metrics(unit: str | None = None) -> dict[str, Any]:
        """List the canonical metrics you can read, with their measures, dimensions, presets,
        variants and anchoring. Optionally narrow to one unit. Call this first."""
        return T.list_metrics(rt, who(), unit=unit)

    @tool(READ_ONLY)
    def get_metric(metric: str, unit: str, start: str | None = None, end: str | None = None,
                   measure: str | None = None, variant: str | None = None,
                   filters: dict[str, Any] | None = None, preset: str | None = None,
                   verbose: bool = False) -> dict[str, Any]:
        """The canonical value of one metric in one unit. start inclusive, end exclusive
        (YYYY-MM-DD); omit both for all-time. `measure` picks a quantity of a structured
        metric, `variant` a form of a derived one. `filters` binds dimension = value.
        verbose=true adds the compiled statement for audit."""
        return T.get_metric(rt, who(), metric=metric, unit=unit, start=start, end=end, measure=measure,
                            variant=variant, filters=filters, preset=preset, verbose=verbose)

    @tool(READ_ONLY)
    def get_metric_breakdown(metric: str, unit: str, by: str, start: str | None = None,
                             end: str | None = None, measure: str | None = None,
                             filters: dict[str, Any] | None = None, preset: str | None = None,
                             order: str | None = None, limit: int | None = None,
                             verbose: bool = False) -> dict[str, Any]:
        """A metric grouped by one declared dimension. order: value_desc (default), value_asc
        or dimension; limit for top-N. truncated=true means more groups exist."""
        return T.get_metric_breakdown(rt, who(), metric=metric, unit=unit, by=by, start=start,
                                      end=end, measure=measure, filters=filters, preset=preset,
                                      order=order, limit=limit, verbose=verbose)

    @tool(READ_ONLY)
    def get_composite_metric(metric: str, start: str | None = None, end: str | None = None,
                             measure: str | None = None, variant: str | None = None,
                             filters: dict[str, Any] | None = None,
                             preset: str | None = None) -> dict[str, Any]:
        """One metric across every unit you can read: each unit's value, its share, and the
        total when the measure is additive. units_covered says what the total covers."""
        return T.get_composite_metric(rt, who(), metric=metric, start=start, end=end, measure=measure,
                                      variant=variant, filters=filters, preset=preset)

    @tool(READ_ONLY)
    def compare_periods(metric: str, unit: str, start: str, end: str, prior_start: str,
                        prior_end: str, measure: str | None = None, variant: str | None = None,
                        filters: dict[str, Any] | None = None,
                        preset: str | None = None) -> dict[str, Any]:
        """A metric over two periods with the absolute and relative change."""
        return T.compare_periods(rt, who(), metric=metric, unit=unit, start=start, end=end,
                                 prior_start=prior_start, prior_end=prior_end, measure=measure,
                                 variant=variant, filters=filters, preset=preset)

    @tool(READ_ONLY)
    def list_rules() -> dict[str, Any]:
        """The governed-rule catalogue (domains, keys, overrides). Values come from resolve_rule."""
        return T.list_rules(rt, who())

    @tool(READ_ONLY)
    def resolve_rule(domain: str, key: str, unit: str, as_of: str | None = None) -> dict[str, Any]:
        """The verified value of a governed rule for a unit as of a date (default today), with
        the layer that won and its effective date. An unverified rule is refused."""
        return T.resolve_rule(rt, who(), domain=domain, key=key, unit=unit, as_of=as_of)

    @tool(READ_ONLY)
    def get_glossary(term: str | None = None) -> dict[str, Any]:
        """What a term means, as the organisation defines it. Omit term for the full glossary."""
        return T.get_glossary(rt, who(), term=term)

    @tool(READ_ONLY)
    def list_sources(unit: str) -> dict[str, Any]:
        """The sources your ad-hoc access covers in a unit (requires an ad-hoc grant)."""
        return T.list_sources(rt, who(), unit=unit)

    @tool(READ_ONLY)
    def describe_source(unit: str, source: str) -> dict[str, Any]:
        """Columns and types of one ad-hoc source, with personal columns flagged."""
        return T.describe_source(rt, who(), unit=unit, source=source)

    @tool(ADHOC)
    def run_query(unit: str, sql: str, max_rows: int | None = None) -> dict[str, Any]:
        """Guarded, NON-CANONICAL ad-hoc SELECT for exploration where no metric exists. One
        read-only statement over schema-qualified sources in your grant; row-capped and timed
        out. Aggregate rather than paging through rows. Prefer get_metric whenever possible."""
        return T.run_query(rt, who(), unit=unit, sql=sql, max_rows=max_rows)

    @tool(ADMIN)
    def reload_knowledge() -> dict[str, Any]:
        """Administrators only: re-read definitions and policy. Invalid knowledge is rejected
        and the current version keeps serving."""
        return T.reload_knowledge(rt, who())

    register_health(mcp, rt)
    register_admin(mcp, rt)
    return mcp


def deep_check(rt: Runtime) -> dict[str, Any]:
    """Can this process load its knowledge, enforce its policy and reach every unit? The
    deploy pipeline waits on this, not on liveness: a server that is up but cannot reach
    the warehouse would pass a liveness check and then refuse every request."""
    warehouse = {u: rt.warehouse.ping(u) for u in sorted(rt.knowledge.units)}
    policy_ok = rt.current_policy().ok
    return {"ready": policy_ok and all(warehouse.values()), "knowledge_version": rt.knowledge.version,
            "policy_ok": policy_ok, "warehouse": warehouse,
            "audit_write_failures": rt.audit.write_failures}


def register_health(mcp: FastMCP, rt: Runtime) -> None:
    from starlette.responses import JSONResponse

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        return JSONResponse({"status": "ok", "knowledge_version": rt.knowledge.version})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(request):
        report = await anyio.to_thread.run_sync(deep_check, rt)
        return JSONResponse(report, status_code=200 if report["ready"] else 503)
