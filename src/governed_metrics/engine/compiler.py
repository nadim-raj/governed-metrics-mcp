"""Compile a definition into one parameterised, read-only statement.

Every SQL fragment in a compiled statement comes from a validated definition. Caller input
reaches it in only two ways: names (measure, dimension, preset, order) that are checked
against the definition's own allow-lists, and values (dates, filter values, limits) that
are bound as parameters. Nothing a caller types is ever spliced into query text.

Windows are half-open: `start <= t < end`. A record on a boundary belongs to exactly one
period, so adjacent periods never double-count it and a year equals the sum of its months.
"""
from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..errors import InvalidRequest
from ..knowledge.model import Measure, Source, StructuredDefinition

MAX_FILTER_TEXT = 200
ORDERS = {"value_desc": "2 DESC NULLS LAST, 1", "value_asc": "2 ASC NULLS LAST, 1", "dimension": "1"}


@dataclass(frozen=True)
class Window:
    start: date | None = None
    end: date | None = None            # exclusive

    @property
    def all_time(self) -> bool:
        return self.start is None

    def describe(self) -> dict[str, Any] | str:
        if self.all_time:
            return "all-time"
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "end_exclusive": True}


@dataclass(frozen=True)
class Statement:
    sql: str
    params: Mapping[str, Any]


def did_you_mean(name: str, options: Iterable[str]) -> str:
    close = difflib.get_close_matches(str(name), list(options), n=1, cutoff=0.6)
    return f" Did you mean '{close[0]}'?" if close else ""


def parse_date(value: Any, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as e:
        raise InvalidRequest(f"'{label}' must be an ISO date (YYYY-MM-DD), got {value!r}.",
                             reason="invalid_window") from e


def parse_window(start: Any, end: Any) -> Window:
    """Both bounds or neither. `end` is exclusive: January is start=YYYY-01-01, end=YYYY-02-01."""
    if (start is None) != (end is None):
        raise InvalidRequest("Pass both start and end, or neither for all-time.", reason="invalid_window")
    if start is None:
        return Window()
    s, e = parse_date(start, "start"), parse_date(end, "end")
    if s >= e:
        raise InvalidRequest("'start' must be before 'end' (the window is start <= t < end).",
                             reason="invalid_window")
    return Window(s, e)


def _escape(fragment: str) -> str:
    """Definition SQL is spliced next to %(name)s placeholders; a literal % must be doubled."""
    return fragment.replace("%", "%%")


def resolve_measure(d: StructuredDefinition, name: str | None) -> Measure:
    chosen = name or d.default_measure
    if chosen in d.measures:
        return d.measures[chosen]
    target = d.measure_aliases.get(chosen)
    if target in d.measures:
        return d.measures[target]
    raise InvalidRequest(
        f"Unknown measure '{chosen}' for {d.metric}/{d.unit}."
        f"{did_you_mean(chosen, [*d.measures, *d.measure_aliases])} Available: {sorted(d.measures)}.",
        reason="unknown_measure")


def _from(d: StructuredDefinition) -> str:
    parts = [f"{d.source} AS {d.alias}"]
    for j in d.joins:
        parts.append(f"{'LEFT JOIN' if j.kind == 'left' else 'JOIN'} {j.source} AS {j.alias} "
                     f"ON ({_escape(j.on)})")
    return " ".join(parts)


def _filter_value(name: str, value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_FILTER_TEXT:
            raise InvalidRequest(f"Filter '{name}' is longer than {MAX_FILTER_TEXT} characters.",
                                 reason="invalid_filter")
        return value
    raise InvalidRequest(f"Filter '{name}' must be one scalar value (a filter binds one equality).",
                         reason="invalid_filter")


def _where(d: StructuredDefinition, window: Window, filters: Mapping[str, Any] | None,
           preset: str | None) -> tuple[str, dict[str, Any]]:
    parts, params = [], {}
    if d.base_filter:
        parts.append(f"({_escape(d.base_filter)})")
    if not window.all_time:
        if d.snapshot:
            raise InvalidRequest(f"'{d.metric}' is a snapshot (a current value, not a flow over a "
                                 "period); omit start and end.", reason="snapshot_metric")
        parts.append(f"{_escape(d.period_column)} >= %(start)s AND {_escape(d.period_column)} < %(end)s")
        params.update(start=window.start, end=window.end)
    for i, (name, value) in enumerate(sorted((filters or {}).items())):
        dim = d.dimensions.get(name)
        if dim is None:
            raise InvalidRequest(f"Unknown filter '{name}' for {d.metric}/{d.unit}."
                                 f"{did_you_mean(name, d.dimensions)} Available: {sorted(d.dimensions)}.",
                                 reason="unknown_dimension")
        parts.append(f"({_escape(dim.sql)}) = %(f{i})s")
        params[f"f{i}"] = _filter_value(name, value)
    if preset:
        p = d.presets.get(preset)
        if p is None:
            raise InvalidRequest(f"Unknown preset '{preset}' for {d.metric}/{d.unit}."
                                 f"{did_you_mean(preset, d.presets)} Available: {sorted(d.presets)}.",
                                 reason="unknown_preset")
        parts.append(f"({_escape(p.sql)})")
    return (" AND ".join(parts) or "TRUE"), params


def compile_scalar(d: StructuredDefinition, measure: Measure, window: Window,
                   filters: Mapping[str, Any] | None = None, preset: str | None = None) -> Statement:
    where, params = _where(d, window, filters, preset)
    return Statement(f"SELECT {_escape(measure.sql)} AS value FROM {_from(d)} WHERE {where}", params)


def compile_breakdown(d: StructuredDefinition, measure: Measure, by: str, window: Window, *,
                      filters: Mapping[str, Any] | None = None, preset: str | None = None,
                      order: str | None = None, row_limit: int) -> Statement:
    dim = d.dimensions.get(by)
    if dim is None:
        raise InvalidRequest(f"'{by}' is not a declared breakdown for {d.metric}/{d.unit}."
                             f"{did_you_mean(by, d.dimensions)} Available: {sorted(d.dimensions)}.",
                             reason="unknown_dimension")
    order = order or "value_desc"
    if order not in ORDERS:
        raise InvalidRequest(f"order must be one of {sorted(ORDERS)}.", reason="invalid_order")
    where, params = _where(d, window, filters, preset)
    params["row_limit"] = row_limit
    return Statement(
        f"SELECT {_escape(dim.sql)} AS dimension, {_escape(measure.sql)} AS value FROM {_from(d)} "
        f"WHERE {where} GROUP BY 1 ORDER BY {ORDERS[order]} LIMIT %(row_limit)s", params)


def compile_freshness(source: Source) -> Statement:
    return Statement(f"SELECT MAX({source.freshness.column})::date AS value FROM {source.name}", {})


def compile_match_count(d: StructuredDefinition, preset: str | None = None) -> Statement:
    """Rows a definition's filters match over all time: the live check that a filter written
    against a renamed label now matches nothing (lessons-learned.md, "Label drift")."""
    where, params = _where(d, Window(), None, preset)
    return Statement(f"SELECT COUNT(*) AS value FROM {_from(d)} WHERE {where}", params)
