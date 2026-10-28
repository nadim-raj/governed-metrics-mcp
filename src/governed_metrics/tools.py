"""Tool bodies, independent of the MCP transport (server.py only wires them up).

Every tool is wrapped by `governed`, which is the single path from a request to a response:

  1. one knowledge snapshot and one policy for the whole call;
  2. the tool body: authorisation first, then the engine;
  3. every exception mapped to one of the four error kinds; anything unexpected becomes a
     generic failure whose detail stays in the server log;
  4. the masking chokepoint, applied to successes and errors alike;
  5. exactly one audit line, whatever happened.

A new tool written with `@governed` gets all five by construction.
"""
from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import masking, overlay
from .adhoc import guard
from .audit import rows_band
from .engine.compiler import Statement, did_you_mean, parse_date, parse_window
from .engine.executor import retry_once
from .errors import AccessDenied, Failure, GovernedError, InvalidRequest, KnowledgeError
from .knowledge.model import DerivedDefinition, Knowledge, Unit
from .policy import AccessPolicy
from .rules import list_rules as _list_rules
from .rules import resolve_rule as _resolve_rule
from .runtime import Runtime, jsonify

log = logging.getLogger("governed_metrics.tools")

NON_CANONICAL_LABEL = ("NON-CANONICAL: an ad-hoc result from a caller-supplied statement, not a "
                       "governed metric. Say so wherever this number is used.")
_DESCRIBE_SQL = ("SELECT column_name, data_type FROM information_schema.columns "
                 "WHERE table_schema = %(schema)s AND table_name = %(table)s ORDER BY ordinal_position")


def governed(tool: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(rt: Runtime, caller: str, **kwargs: Any) -> dict[str, Any]:
            started = time.perf_counter()
            kb, policy = rt.knowledge, rt.current_policy()
            verbose = bool(kwargs.pop("verbose", False))
            outcome, reason = "ok", None
            try:
                result = fn(rt, kb, policy, caller, **kwargs)
            except GovernedError as e:
                outcome, reason = e.kind, e.reason
                (log.error if e.kind == "failure" else log.info)(
                    "tool=%s caller=%s outcome=%s reason=%s", tool, caller, e.kind, e.reason)
                result = e.envelope()
            except Exception:
                log.exception("tool=%s caller=%s failed", tool, caller)
                outcome, reason = "failure", "internal"
                result = Failure("The request could not be completed. It has been logged.").envelope()
            if not verbose and isinstance(result, dict):
                result = {k: v for k, v in result.items() if k != "statement"}
            try:
                result = masking.apply(jsonify(result), unmasked=policy.unmasked(caller),
                                       personal_columns=kb.personal_columns())
            except GovernedError as e:
                outcome, reason = e.kind, e.reason
                log.error("tool=%s result withheld by the masking chokepoint", tool)
                result = jsonify(e.envelope())
            rt.audit.record(caller=caller or "(none)", tool=tool, unit=kwargs.get("unit"),
                            metric=kwargs.get("metric"), knowledge_version=kb.version,
                            outcome=outcome, reason=reason,
                            duration_ms=round((time.perf_counter() - started) * 1000, 1),
                            rows_band=rows_band(result.get("row_count")) if outcome == "ok" else None,
                            cached=result.get("cached"))
            return result
        return wrapper
    return deco


# ---- authorisation -------------------------------------------------------------------------

def _require_caller(policy: AccessPolicy, caller: str) -> None:
    if not policy.ok:
        raise AccessDenied("The access policy is unavailable, so every request is denied.",
                           reason="policy_unavailable")
    if not policy.units_for(caller):
        raise AccessDenied("You have no access grant.", reason="no_grant",
                           hint="Ask an administrator for access to the units you need.")


def _require_unit(kb: Knowledge, policy: AccessPolicy, caller: str, unit: Any) -> Unit:
    _require_caller(policy, caller)
    if not isinstance(unit, str) or unit not in kb.units:
        raise InvalidRequest(f"Unknown unit {unit!r}. Units: {sorted(kb.units)}.", reason="unknown_unit")
    if not policy.can_read_unit(caller, unit):
        raise AccessDenied(f"You do not have access to unit '{unit}'.", reason="unit_not_permitted")
    return kb.units[unit]


def _visible_metrics(kb: Knowledge, policy: AccessPolicy, caller: str, unit: str) -> list[str]:
    return sorted(k for k, m in kb.metrics.items()
                  if unit in m.units and policy.metric_allowed(caller, unit, k))


def _require_metric(kb: Knowledge, policy: AccessPolicy, caller: str, unit: str, key: Any) -> None:
    if not isinstance(key, str) or not key:
        raise InvalidRequest("metric is required.", reason="unknown_metric")
    if policy.has_metric_allow_list(caller, unit) and not policy.metric_allowed(caller, unit, key):
        # Checked before existence, so a metric-limited caller learns nothing about names
        # outside their list.
        raise AccessDenied(f"You do not have access to metric '{key}' in '{unit}'.",
                           reason="metric_not_permitted")
    metric = kb.metrics.get(key)
    if metric is None or unit not in metric.units:
        visible = _visible_metrics(kb, policy, caller, unit)
        raise InvalidRequest(f"'{key}' is not a metric of unit '{unit}'.{did_you_mean(key, visible)} "
                             "Call list_metrics for the catalogue.", reason="unknown_metric")


def _require_dimensions(policy: AccessPolicy, caller: str, metrics: Iterable[str],
                        dims: Iterable[str]) -> None:
    dims = list(dims)
    if not dims:
        return
    for key in metrics:
        allowed = policy.allowed_dimensions(caller, key)
        if allowed is not None and (bad := [d for d in dims if d not in allowed]):
            raise AccessDenied(f"You may not break down or filter '{key}' by {sorted(bad)}.",
                               reason="dimension_not_permitted")


def _check_filters(filters: Any) -> Mapping[str, Any] | None:
    if filters is None:
        return None
    if not isinstance(filters, dict):
        raise InvalidRequest("filters must be an object of dimension -> value.", reason="invalid_filter")
    return filters


def _authorise_metric(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, unit: str,
                      metric: str, variant: str | None, dims: Iterable[str]) -> None:
    _require_unit(kb, policy, caller, unit)
    _require_metric(kb, policy, caller, unit, metric)
    legs = rt.engine.leg_metrics(kb, metric, unit, variant)
    for leg in legs:
        if not policy.metric_allowed(caller, unit, leg):
            # A derived answer shows its components, so reading it needs its legs too.
            raise AccessDenied(f"'{metric}' is computed from '{leg}', which you do not have access to.",
                               reason="metric_not_permitted")
    _require_dimensions(policy, caller, [metric, *legs], dims)


# ---- catalogue -----------------------------------------------------------------------------

def _unit_detail(d, allowed_dims: frozenset[str] | None) -> dict[str, Any]:
    if isinstance(d, DerivedDefinition):
        out: dict[str, Any] = {"variants": sorted(d.variants), "default_variant": d.default_variant,
                               "op": d.op}
    else:
        dims = [n for n in sorted(d.dimensions) if allowed_dims is None or n in allowed_dims]
        out = {"measures": {n: {"additive": m.additive} for n, m in sorted(d.measures.items())},
               "default_measure": d.default_measure, "dimensions": dims,
               "presets": {n: p.description for n, p in sorted(d.presets.items())},
               "anchoring": "snapshot" if d.snapshot else "period"}
        if d.measure_aliases:
            out["measure_aliases"] = dict(d.measure_aliases)
    if d.maturity is not None:
        out["maturity"] = {"settles_after_days": d.maturity.settles_after_days, "note": d.maturity.note}
    return out


@governed("list_metrics")
def list_metrics(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str,
                 unit: str | None = None) -> dict[str, Any]:
    _require_caller(policy, caller)
    units = [unit] if unit is not None else sorted(policy.units_for(caller) & set(kb.units))
    for u in units:
        _require_unit(kb, policy, caller, u)
    catalog = []
    for key, metric in sorted(kb.metrics.items()):
        detail = {u: _unit_detail(metric.units[u], policy.allowed_dimensions(caller, key))
                  for u in units if u in metric.units and policy.metric_allowed(caller, u, key)}
        if detail:
            catalog.append({"key": key, "description": metric.description, "kind": metric.kind,
                            "owner": metric.owner, "version": metric.version,
                            "effective": metric.effective, "units": detail})
    return {"kind": "catalog", "metrics": catalog, "knowledge_version": kb.version,
            "windows": "start inclusive, end exclusive (YYYY-MM-DD); omit both for all-time"}


@governed("get_glossary")
def get_glossary(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str,
                 term: str | None = None) -> dict[str, Any]:
    _require_caller(policy, caller)
    if term is None:
        return {"kind": "glossary", "terms": dict(sorted(kb.glossary.items()))}
    if term not in kb.glossary:
        raise InvalidRequest(f"No glossary entry for '{term}'.{did_you_mean(term, kb.glossary)}",
                             reason="unknown_term")
    return {"kind": "glossary", "terms": {term: kb.glossary[term]}}


# ---- canonical metrics ---------------------------------------------------------------------

@governed("get_metric")
def get_metric(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *, metric: str,
               unit: str, start: str | None = None, end: str | None = None,
               measure: str | None = None, variant: str | None = None,
               filters: dict | None = None, preset: str | None = None) -> dict[str, Any]:
    filters = _check_filters(filters)
    _authorise_metric(rt, kb, policy, caller, unit, metric, variant, (filters or {}).keys())
    return rt.engine.metric(kb, metric, unit, parse_window(start, end), measure=measure,
                            variant=variant, filters=filters, preset=preset)


@governed("get_metric_breakdown")
def get_metric_breakdown(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *,
                         metric: str, unit: str, by: str, start: str | None = None,
                         end: str | None = None, measure: str | None = None,
                         filters: dict | None = None, preset: str | None = None,
                         order: str | None = None, limit: int | None = None) -> dict[str, Any]:
    filters = _check_filters(filters)
    _authorise_metric(rt, kb, policy, caller, unit, metric, None, [by, *(filters or {})])
    return rt.engine.breakdown(kb, metric, unit, by, parse_window(start, end), measure=measure,
                               filters=filters, preset=preset, order=order, limit=limit)


@governed("get_composite_metric")
def get_composite_metric(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *,
                         metric: str, start: str | None = None, end: str | None = None,
                         measure: str | None = None, variant: str | None = None,
                         filters: dict | None = None, preset: str | None = None) -> dict[str, Any]:
    _require_caller(policy, caller)
    filters = _check_filters(filters)
    m = kb.metrics.get(metric) if isinstance(metric, str) else None
    units = [] if m is None else sorted(
        u for u in m.units if policy.can_read_unit(caller, u) and policy.metric_allowed(caller, u, metric))
    if not units:
        # One answer whether the metric is unknown or exists only where the caller cannot
        # read: a denial must not reveal what lies behind the boundary.
        raise InvalidRequest(f"'{metric}' is not available in any unit you can read.",
                             reason="metric_not_available")
    for u in units:
        _authorise_metric(rt, kb, policy, caller, u, metric, variant, (filters or {}).keys())
    return rt.engine.composite(kb, metric, units, parse_window(start, end), measure=measure,
                               variant=variant, filters=filters, preset=preset)


@governed("compare_periods")
def compare_periods(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *, metric: str,
                    unit: str, start: str, end: str, prior_start: str, prior_end: str,
                    measure: str | None = None, variant: str | None = None,
                    filters: dict | None = None, preset: str | None = None) -> dict[str, Any]:
    filters = _check_filters(filters)
    _authorise_metric(rt, kb, policy, caller, unit, metric, variant, (filters or {}).keys())
    return rt.engine.compare(kb, metric, unit, parse_window(start, end),
                             parse_window(prior_start, prior_end), measure=measure,
                             variant=variant, filters=filters, preset=preset)


# ---- governed rules ------------------------------------------------------------------------

@governed("list_rules")
def list_rules(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str) -> dict[str, Any]:
    _require_caller(policy, caller)
    return _list_rules(kb)


@governed("resolve_rule")
def resolve_rule(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *, domain: str,
                 key: str, unit: str, as_of: str | None = None) -> dict[str, Any]:
    _require_unit(kb, policy, caller, unit)
    when = rt.today() if as_of is None else parse_date(as_of, "as_of")
    return _resolve_rule(kb, domain, key, unit, when)


# ---- guarded ad-hoc path -------------------------------------------------------------------

def _adhoc_sources(kb: Knowledge, policy: AccessPolicy, caller: str, unit: str) -> frozenset[str]:
    _require_unit(kb, policy, caller, unit)
    sources = policy.adhoc_sources(caller, unit)
    if sources is None:
        raise AccessDenied("Ad-hoc querying is a privilege that has not been granted to you for "
                           f"unit '{unit}'.", reason="adhoc_not_permitted",
                           hint="Use the canonical metric tools, or request ad-hoc access.")
    return sources


@governed("list_sources")
def list_sources(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *,
                 unit: str) -> dict[str, Any]:
    allowed = _adhoc_sources(kb, policy, caller, unit)
    out = []
    for name, src in sorted(kb.units[unit].sources.items()):
        if guard.ALL_SOURCES in allowed or name in allowed:
            entry: dict[str, Any] = {"source": name, "description": src.description,
                                     "personal_columns": sorted(src.personal_columns)}
            if src.freshness:
                entry["freshness"] = {"column": src.freshness.column,
                                      "max_age_days": src.freshness.max_age_days}
            out.append(entry)
    return {"kind": "sources", "unit": unit, "sources": out}


@governed("describe_source")
def describe_source(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *, unit: str,
                    source: str) -> dict[str, Any]:
    allowed = _adhoc_sources(kb, policy, caller, unit)
    src = kb.units[unit].sources.get(source) if isinstance(source, str) else None
    if src is None or not (guard.ALL_SOURCES in allowed or source in allowed):
        raise AccessDenied(f"'{source}' is not a source you may read in '{unit}'.",
                           reason="source_not_permitted")
    stmt = Statement(_DESCRIBE_SQL, {"schema": src.schema, "table": src.table})
    _cols, rows = retry_once(lambda: rt.warehouse.rows(unit, stmt, 2000), what=f"describe {source}")
    return {"kind": "source", "unit": unit, "source": source, "description": src.description,
            "columns": [{"name": r[0], "type": r[1], "personal": r[0] in src.personal_columns}
                        for r in rows]}


@governed("run_query")
def run_query(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str, *, unit: str,
              sql: str, max_rows: int | None = None) -> dict[str, Any]:
    allowed = _adhoc_sources(kb, policy, caller, unit)
    s = rt.settings
    if max_rows is not None and (isinstance(max_rows, bool) or not isinstance(max_rows, int)
                                 or not 1 <= max_rows <= s.adhoc_row_cap_max):
        raise InvalidRequest(f"max_rows must be between 1 and {s.adhoc_row_cap_max}.", reason="invalid_limit")
    if not rt.adhoc_rate.allow(caller):
        raise InvalidRequest(f"Ad-hoc queries are limited to {s.adhoc_rate_per_minute} per minute.",
                             reason="rate_limited", hint="Slow down, or use a canonical metric.")
    approved = guard.check(sql, unit=kb.units[unit], caller_sources=allowed,
                           unmasked=policy.unmasked(caller))
    cap = max_rows or s.adhoc_row_cap_default
    columns, rows = _adhoc_execute(rt, unit, approved.sql, cap + 1)
    truncated = len(rows) > cap
    rows = rows[:cap]
    result = {"kind": "tabular", "unit": unit, "columns": columns, "rows": [list(r) for r in rows],
              "row_count": len(rows), "truncated": truncated, "sources": list(approved.sources),
              "canonical": False, "label": NON_CANONICAL_LABEL,
              "provenance": {"knowledge_version": kb.version}}
    if truncated:
        result["note"] = (f"Capped at {cap} rows. Aggregate further (GROUP BY what will be read) "
                          "rather than paging through rows.")
    return result


def _adhoc_sql_error(e: Exception) -> GovernedError:
    """A mistake in the caller's own statement comes back with the warehouse's first line,
    so the caller can fix it. That is safe only on this path: the statement was approved for
    this unit's sources and ran under this unit's role. Everything else is a generic failure."""
    try:
        import psycopg
        caller_mistake = isinstance(e, (psycopg.errors.ProgrammingError, psycopg.errors.DataError))
    except ImportError:  # pragma: no cover
        caller_mistake = False
    if caller_mistake:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0][:300]
        return InvalidRequest(f"The warehouse rejected the statement: {first}", reason="sql_error",
                              hint="Check names with describe_source before querying.")
    log.error("ad-hoc execution failed: %s", type(e).__name__)
    return Failure("The statement could not be run. It has been logged.")


def _adhoc_execute(rt: Runtime, unit: str, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
    try:
        return retry_once(lambda: rt.warehouse.adhoc(unit, sql, max_rows), what=f"ad-hoc {unit}")
    except GovernedError:
        raise
    except Exception as e:
        raise _adhoc_sql_error(e) from e


# ---- administration ------------------------------------------------------------------------

@governed("reload_knowledge")
def reload_knowledge(rt: Runtime, kb: Knowledge, policy: AccessPolicy, caller: str) -> dict[str, Any]:
    if not policy.is_admin(caller):
        raise AccessDenied("reload_knowledge is restricted to administrators.", reason="admin_only")
    try:
        return rt.reload()
    except KnowledgeError as e:
        raise Failure("The new knowledge failed validation; the previous version is still serving.",
                      reason="knowledge_invalid", extra={"problems": e.problems[:20]}) from e


def admin_grant(rt: Runtime, admin: str, user: str, units: list[str], reason: str) -> bool:
    """Console grant: overlay first (durable), then rebuild the policy so it applies now. The
    policy ignores an overlay grant to any admin, the granting admin included."""
    ok = overlay.grant(rt.settings.overlay_path, user, units, by=admin, reason=reason)
    if ok:
        rt.refresh_policy()
    rt.audit.record(caller=admin, tool="admin.grant", subject=user, knowledge_version=rt.knowledge.version,
                    outcome="ok" if ok else "failure", reason=None if ok else "overlay_write_failed")
    return ok


def admin_revoke(rt: Runtime, admin: str, user: str, reason: str) -> bool:
    ok = overlay.revoke(rt.settings.overlay_path, user, by=admin, reason=reason)
    if ok:
        rt.refresh_policy()
    rt.audit.record(caller=admin, tool="admin.revoke", subject=user, knowledge_version=rt.knowledge.version,
                    outcome="ok" if ok else "failure", reason=None if ok else "overlay_write_failed")
    return ok
