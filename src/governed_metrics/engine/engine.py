"""The metric engine: compile, execute, and stamp canonical answers.

The engine knows nothing about callers. Authorisation happens before it is called (tools.py
decides which units, metrics and dimensions a caller may reach), so this module can be read
as a pure function from (knowledge snapshot, request) to answer.

Every answer carries:
  * `provenance`: the knowledge version and the definition's version and effective date,
    so a number found later in a document can be traced to the definition that produced it;
  * `coverage` when its sources declare freshness, and a refusal instead when they are stale;
  * `maturity` when its window keeps moving after it closes;
  * `canonical: true`, the counterpart of the ad-hoc path's `canonical: false`.
"""
from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import InvalidRequest
from ..knowledge.model import DerivedDefinition, Knowledge, Maturity, Metric, StructuredDefinition
from ..warehouse import Warehouse
from . import freshness as FR
from .cache import TTLCache
from .compiler import (
    Statement,
    Window,
    compile_breakdown,
    compile_freshness,
    compile_scalar,
    resolve_measure,
)
from .executor import retry_once, run_parallel

RATIO_PLACES = Decimal("0.000001")
PCT_PLACES = Decimal("0.0001")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def combine(op: str, a: Any, b: Any) -> Decimal | None:
    """ratio: a / b, None when b is zero or either side is missing. difference: a - b, None
    when either side is missing. A missing side is never read as zero."""
    da, db = _decimal(a), _decimal(b)
    if da is None or db is None:
        return None
    if op == "ratio":
        return None if db == 0 else (da / db).quantize(RATIO_PLACES)
    return da - db


def _frozen(filters: Mapping[str, Any] | None) -> Hashable:
    return tuple(sorted((filters or {}).items()))


def _statement_view(stmt: Statement) -> dict[str, Any]:
    return {"sql": stmt.sql, "params": dict(stmt.params)}


class MetricEngine:
    def __init__(self, warehouse: Warehouse, *, cache_ttl_s: int = 300, freshness_ttl_s: int = 60,
                 breakdown_row_cap: int = 1000, max_parallel_legs: int = 8,
                 today: Callable[[], date] | None = None):
        self.warehouse = warehouse
        self._results = TTLCache(cache_ttl_s)
        self._coverage = TTLCache(freshness_ttl_s)
        self.breakdown_row_cap = breakdown_row_cap
        self.max_parallel = max_parallel_legs
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    def clear_cache(self) -> None:
        self._results.clear()
        self._coverage.clear()

    # ---- lookup ----------------------------------------------------------------------
    @staticmethod
    def definition(kb: Knowledge, key: str, unit: str):
        metric = kb.metrics.get(key)
        if metric is None:
            raise InvalidRequest(f"Unknown metric '{key}'. Call list_metrics for the catalogue.",
                                 reason="unknown_metric")
        d = metric.units.get(unit)
        if d is None:
            raise InvalidRequest(f"'{key}' is not defined for unit '{unit}'.", reason="metric_not_in_unit")
        return metric, d

    def leg_metrics(self, kb: Knowledge, key: str, unit: str, variant: str | None) -> list[str]:
        """The structured metrics a derived answer exposes as its components."""
        _, d = self.definition(kb, key, unit)
        if not isinstance(d, DerivedDefinition):
            return []
        v = d.variants.get(variant or d.default_variant)
        return [] if v is None else [v.numerator.metric, v.denominator.metric]

    # ---- blocks ----------------------------------------------------------------------
    @staticmethod
    def provenance(kb: Knowledge, metric: Metric) -> dict[str, Any]:
        return {"knowledge_version": kb.version, "definition_version": metric.version,
                "definition_effective": metric.effective.isoformat()}

    def maturity(self, m: Maturity | None, window: Window) -> dict[str, Any] | None:
        if m is None:
            return None
        if window.all_time:
            return {"settled": False, "note": m.note}
        settles_on = window.end + timedelta(days=m.settles_after_days)
        return {"settled": self._today() >= settles_on, "settles_on": settles_on.isoformat(),
                "note": m.note}

    def _coverage_of(self, kb: Knowledge, unit: str, source_name: str) -> FR.Coverage:
        key = (kb.version, source_name)
        cached = self._coverage.get(key)
        if cached is not None:
            return cached
        source = kb.units[unit].sources[source_name]
        newest = retry_once(lambda: self.warehouse.scalar(unit, compile_freshness(source)),
                            what=f"freshness {source_name}")
        coverage = FR.assess(source, newest, self._today())
        self._coverage.set(key, coverage)
        return coverage

    def _fresh_jobs(self, kb: Knowledge, d: StructuredDefinition) -> list[Callable[[], FR.Coverage]]:
        unit = kb.units[d.unit]
        names = [s for s in d.sources if unit.sources[s].freshness is not None]
        return [lambda s=s: self._coverage_of(kb, d.unit, s) for s in names]

    # ---- scalar ----------------------------------------------------------------------
    def metric(self, kb: Knowledge, key: str, unit: str, window: Window, *, measure: str | None = None,
               variant: str | None = None, filters: Mapping[str, Any] | None = None,
               preset: str | None = None) -> dict[str, Any]:
        cache_key = ("metric", kb.version, key, unit, window, measure, variant, _frozen(filters), preset)
        if (hit := self._results.get(cache_key)) is not None:
            return {**hit, "cached": True}
        metric, d = self.definition(kb, key, unit)
        if isinstance(d, StructuredDefinition):
            if variant:
                raise InvalidRequest(f"'{key}' is a structured metric; pass measure, not variant.",
                                     reason="unknown_variant")
            result = self._structured(kb, metric, d, window, measure, filters, preset)
        else:
            if measure:
                raise InvalidRequest(f"'{key}' is a derived metric; pass variant, not measure.",
                                     reason="unknown_measure")
            result = self._derived(kb, metric, d, window, variant, filters, preset)
        self._results.set(cache_key, result)
        return result

    def _structured(self, kb: Knowledge, metric: Metric, d: StructuredDefinition, window: Window,
                    measure: str | None, filters: Mapping[str, Any] | None,
                    preset: str | None) -> dict[str, Any]:
        m = resolve_measure(d, measure)
        stmt = compile_scalar(d, m, window, filters, preset)
        value_job = lambda: retry_once(lambda: self.warehouse.scalar(d.unit, stmt),  # noqa: E731
                                       what=f"{metric.key}/{d.unit}")
        value, *coverages = run_parallel([value_job, *self._fresh_jobs(kb, d)], self.max_parallel)
        for c in coverages:
            FR.refuse_if_stale(c)
        result: dict[str, Any] = {"kind": "metric", "metric": metric.key, "unit": d.unit,
                                  "value": value, "measure": m.name, "period": window.describe()}
        if filters:
            result["filters"] = dict(sorted(filters.items()))
        if preset:
            result["preset"] = preset
        if (cov := FR.coverage_block(coverages, window)) is not None:
            result["coverage"] = cov
        if (mat := self.maturity(d.maturity, window)) is not None:
            result["maturity"] = mat
        result.update(provenance=self.provenance(kb, metric), canonical=True,
                      statement=_statement_view(stmt))
        return result

    def _derived(self, kb: Knowledge, metric: Metric, d: DerivedDefinition, window: Window,
                 variant: str | None, filters: Mapping[str, Any] | None,
                 preset: str | None) -> dict[str, Any]:
        name = variant or d.default_variant
        v = d.variants.get(name)
        if v is None:
            raise InvalidRequest(f"Unknown variant '{name}' for {metric.key}/{d.unit}. "
                                 f"Available: {sorted(d.variants)}.", reason="unknown_variant")

        def leg_job(leg):
            lm = kb.metrics[leg.metric]
            return lambda: self._structured(kb, lm, lm.units[d.unit], window, leg.measure, filters,
                                            leg.preset or preset)

        num, den = run_parallel([leg_job(v.numerator), leg_job(v.denominator)], self.max_parallel)
        result: dict[str, Any] = {
            "kind": "metric", "metric": metric.key, "unit": d.unit,
            "value": combine(d.op, num["value"], den["value"]), "variant": name, "op": d.op,
            "period": window.describe(),
            "components": {side: {"metric": leg["metric"], "measure": leg["measure"], "value": leg["value"]}
                           for side, leg in (("numerator", num), ("denominator", den))},
        }
        if filters:
            result["filters"] = dict(sorted(filters.items()))
        if preset:
            result["preset"] = preset
        if (cov := FR.merge_blocks([num.get("coverage"), den.get("coverage")])) is not None:
            result["coverage"] = cov
        if (mat := self.maturity(d.maturity, window)) is not None:
            result["maturity"] = mat
        result.update(provenance=self.provenance(kb, metric), canonical=True,
                      statement={"numerator": num["statement"], "denominator": den["statement"]})
        return result

    # ---- breakdown -------------------------------------------------------------------
    def breakdown(self, kb: Knowledge, key: str, unit: str, by: str, window: Window, *,
                  measure: str | None = None, filters: Mapping[str, Any] | None = None,
                  preset: str | None = None, order: str | None = None,
                  limit: int | None = None) -> dict[str, Any]:
        metric, d = self.definition(kb, key, unit)
        if not isinstance(d, StructuredDefinition):
            raise InvalidRequest("Breakdowns are available for structured metrics; break down the "
                                 "derived metric's components instead.", reason="unsupported_breakdown")
        limit = self.breakdown_row_cap if limit is None else limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.breakdown_row_cap:
            raise InvalidRequest(f"limit must be between 1 and {self.breakdown_row_cap}.", reason="invalid_limit")
        cache_key = ("breakdown", kb.version, key, unit, by, window, measure, _frozen(filters),
                     preset, order, limit)
        if (hit := self._results.get(cache_key)) is not None:
            return {**hit, "cached": True}
        m = resolve_measure(d, measure)
        # One extra row tells a complete answer from a cut one.
        stmt = compile_breakdown(d, m, by, window, filters=filters, preset=preset, order=order,
                                 row_limit=limit + 1)
        rows_job = lambda: retry_once(lambda: self.warehouse.rows(unit, stmt, limit + 1),  # noqa: E731
                                      what=f"{key}/{unit} by {by}")
        (_cols, rows), *coverages = run_parallel([rows_job, *self._fresh_jobs(kb, d)], self.max_parallel)
        for c in coverages:
            FR.refuse_if_stale(c)
        truncated = len(rows) > limit
        rows = rows[:limit]
        result: dict[str, Any] = {
            "kind": "breakdown", "metric": key, "unit": unit, "by": by, "measure": m.name,
            "period": window.describe(), "dimension_personal": d.dimensions[by].personal,
            "rows": [{"dimension": r[0], "value": r[1]} for r in rows], "row_count": len(rows),
            "truncated": truncated,
        }
        if truncated:
            # The cap protects the warehouse; saying so protects the reader. Silence would let
            # a partial breakdown pass for a complete one.
            result["note"] = (f"Partial: there are more than {limit} groups and only the first {limit} "
                              "by the requested order are shown. Do not add them up as a total.")
        if filters:
            result["filters"] = dict(sorted(filters.items()))
        if preset:
            result["preset"] = preset
        if (cov := FR.coverage_block(coverages, window)) is not None:
            result["coverage"] = cov
        if (mat := self.maturity(d.maturity, window)) is not None:
            result["maturity"] = mat
        result.update(provenance=self.provenance(kb, metric), canonical=True,
                      statement=_statement_view(stmt))
        self._results.set(cache_key, result)
        return result

    # ---- composite -------------------------------------------------------------------
    def composite(self, kb: Knowledge, key: str, units: Sequence[str], window: Window, *,
                  measure: str | None = None, variant: str | None = None,
                  filters: Mapping[str, Any] | None = None, preset: str | None = None) -> dict[str, Any]:
        """One leg per unit in `units` (the caller's permitted units, decided upstream). A unit
        not in `units` is absent from the answer, never reported as zero."""
        metric = kb.metrics.get(key)
        if metric is None:
            raise InvalidRequest(f"Unknown metric '{key}'.", reason="unknown_metric")
        covered = [u for u in units if u in metric.units]
        if not covered:
            raise InvalidRequest(f"'{key}' is not defined for any unit you can read.",
                                 reason="metric_not_in_unit")
        per = run_parallel([lambda u=u: self.metric(kb, key, u, window, measure=measure, variant=variant,
                                                    filters=filters, preset=preset) for u in covered],
                           self.max_parallel)
        additive = metric.kind == "structured" and all(
            resolve_measure(metric.units[u], measure).additive for u in covered)
        values = [_decimal(r["value"]) for r in per]
        total = sum((v for v in values if v is not None), Decimal(0)) \
            if additive and any(v is not None for v in values) else None
        out_units = {}
        for u, r, v in zip(covered, per, values, strict=True):
            entry: dict[str, Any] = {"value": r["value"], "measure": r.get("measure"),
                                     "variant": r.get("variant")}
            if total not in (None, 0) and v is not None:
                entry["share"] = (v / total).quantize(PCT_PLACES)
            for block in ("coverage", "maturity"):
                if block in r:
                    entry[block] = r[block]
            out_units[u] = {k: val for k, val in entry.items() if val is not None}
        note = "The total covers only the units listed in units_covered."
        if not additive:
            note = ("This measure is not additive across units, so values are shown side by side "
                    "with no total.")
        return {"kind": "composite", "metric": key, "period": window.describe(),
                "units_covered": covered, "units": out_units, "total": total, "additive": additive,
                "note": note, "provenance": self.provenance(kb, metric), "canonical": True}

    # ---- comparison ------------------------------------------------------------------
    def compare(self, kb: Knowledge, key: str, unit: str, current: Window, prior: Window,
                **kwargs: Any) -> dict[str, Any]:
        if current.all_time or prior.all_time:
            raise InvalidRequest("Both periods need a start and an end.", reason="invalid_window")
        cur, pri = run_parallel([lambda: self.metric(kb, key, unit, current, **kwargs),
                                 lambda: self.metric(kb, key, unit, prior, **kwargs)], self.max_parallel)
        change = combine("difference", cur["value"], pri["value"])
        base = _decimal(pri["value"])
        pct = None if change is None or not base else (change / abs(base)).quantize(PCT_PLACES)

        def slim(r: dict[str, Any]) -> dict[str, Any]:
            keep = ("value", "period", "measure", "variant", "coverage", "maturity")
            return {k: r[k] for k in keep if k in r}

        return {"kind": "comparison", "metric": key, "unit": unit, "current": slim(cur),
                "prior": slim(pri), "change": change, "change_pct": pct,
                "provenance": cur["provenance"], "canonical": True}
