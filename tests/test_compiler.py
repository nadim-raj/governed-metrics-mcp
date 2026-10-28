"""Caller input reaches a compiled statement only as allow-listed names or bound parameters."""
from __future__ import annotations

from datetime import date

import pytest

from governed_metrics.engine.compiler import (
    Window,
    compile_breakdown,
    compile_match_count,
    compile_scalar,
    parse_window,
    resolve_measure,
)
from governed_metrics.errors import InvalidRequest


@pytest.fixture
def revenue_a(kb):
    return kb.metrics["revenue"].units["unit_a"]


def test_window_is_half_open(revenue_a):
    stmt = compile_scalar(revenue_a, resolve_measure(revenue_a, None), parse_window("2026-01-01", "2026-02-01"))
    assert "o.ordered_at >= %(start)s AND o.ordered_at < %(end)s" in stmt.sql
    assert stmt.params == {"start": date(2026, 1, 1), "end": date(2026, 2, 1)}


def test_fragment_is_expanded_into_the_statement(revenue_a):
    stmt = compile_scalar(revenue_a, resolve_measure(revenue_a, None), Window())
    assert "(o.status <> 'test' AND NOT o.is_internal)" in stmt.sql
    assert "{{" not in stmt.sql


@pytest.mark.parametrize("payload", [
    "web' OR 1=1 --",
    "web'); DROP TABLE unit_a.orders; --",
    "%(start)s",
    "web\\' OR true",
])
def test_filter_values_are_bound_never_spliced(revenue_a, payload):
    stmt = compile_scalar(revenue_a, resolve_measure(revenue_a, None), Window(), {"channel": payload})
    assert payload not in stmt.sql
    assert stmt.params["f0"] == payload
    assert "(o.channel) = %(f0)s" in stmt.sql


@pytest.mark.parametrize("name", ["channel; drop", "o.channel", "1=1", "status"])
def test_filter_names_must_be_declared_dimensions(revenue_a, name):
    with pytest.raises(InvalidRequest) as e:
        compile_scalar(revenue_a, resolve_measure(revenue_a, None), Window(), {name: "x"})
    assert e.value.reason == "unknown_dimension"


@pytest.mark.parametrize("value", [["a", "b"], {"x": 1}, None, "x" * 201])
def test_filter_values_must_be_short_scalars(revenue_a, value):
    with pytest.raises(InvalidRequest) as e:
        compile_scalar(revenue_a, resolve_measure(revenue_a, None), Window(), {"channel": value})
    assert e.value.reason == "invalid_filter"


def test_unknown_measure_suggests_the_closest(revenue_a):
    with pytest.raises(InvalidRequest, match="Did you mean 'gross'"):
        resolve_measure(revenue_a, "gros")


def test_measure_alias_resolves_to_the_real_measure(kb):
    d = kb.metrics["revenue"].units["unit_b"]
    assert resolve_measure(d, "net").name == "net_sales"


def test_unknown_preset_is_refused(revenue_a):
    with pytest.raises(InvalidRequest) as e:
        compile_scalar(revenue_a, resolve_measure(revenue_a, None), Window(), None, "everything")
    assert e.value.reason == "unknown_preset"


def test_preset_sql_comes_from_the_definition(revenue_a):
    stmt = compile_scalar(revenue_a, resolve_measure(revenue_a, None), Window(), None, "first_orders")
    assert stmt.sql.endswith("AND (o.is_first_order)")


@pytest.mark.parametrize("start,end,reason", [
    ("2026-01-01", None, "invalid_window"),
    ("2026-02-01", "2026-01-01", "invalid_window"),
    ("2026-01-01", "2026-01-01", "invalid_window"),
    ("January", "2026-02-01", "invalid_window"),
])
def test_bad_windows_are_refused(start, end, reason):
    with pytest.raises(InvalidRequest) as e:
        parse_window(start, end)
    assert e.value.reason == reason


def test_snapshot_metric_refuses_a_window(kb):
    d = kb.metrics["active_products"].units["unit_a"]
    with pytest.raises(InvalidRequest) as e:
        compile_scalar(d, resolve_measure(d, None), parse_window("2026-01-01", "2026-02-01"))
    assert e.value.reason == "snapshot_metric"
    assert "WHERE (p.is_active)" in compile_scalar(d, resolve_measure(d, None), Window()).sql


def test_breakdown_orders_deterministically_and_limits_by_parameter(revenue_a):
    stmt = compile_breakdown(revenue_a, resolve_measure(revenue_a, None), "channel", Window(), row_limit=11)
    assert stmt.sql.endswith("GROUP BY 1 ORDER BY 2 DESC NULLS LAST, 1 LIMIT %(row_limit)s")
    assert stmt.params["row_limit"] == 11
    with pytest.raises(InvalidRequest):
        compile_breakdown(revenue_a, resolve_measure(revenue_a, None), "channel", Window(), order="random()",
                          row_limit=5)
    with pytest.raises(InvalidRequest):
        compile_breakdown(revenue_a, resolve_measure(revenue_a, None), "customer_email", Window(), row_limit=5)


def test_percent_signs_in_definitions_are_escaped(kb):
    from dataclasses import replace

    d = kb.metrics["orders"].units["unit_a"]
    d = replace(d, base_filter="o.channel LIKE 'w%'")
    stmt = compile_scalar(d, resolve_measure(d, None), Window())
    assert "LIKE 'w%%'" in stmt.sql


def test_match_count_covers_base_filter_and_preset(kb):
    d = kb.metrics["orders"].units["unit_a"]
    assert compile_match_count(d, "paid").sql.startswith("SELECT COUNT(*) AS value FROM unit_a.orders AS o")
