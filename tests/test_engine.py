"""The engine against a fake warehouse: values, provenance, freshness, maturity, legs."""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from governed_metrics.engine.compiler import Window, parse_window
from governed_metrics.engine.engine import MetricEngine, combine
from governed_metrics.errors import InvalidRequest, QueryTimeout, StaleSource
from governed_metrics.warehouse import StatementTimeout

from .fakes import TODAY, FakeWarehouse

JAN = parse_window("2026-01-01", "2026-02-01")


def engine(wh, **kw):
    return MetricEngine(wh, cache_ttl_s=kw.pop("cache_ttl_s", 0), freshness_ttl_s=0, today=lambda: TODAY, **kw)


def test_scalar_carries_value_provenance_and_coverage(kb):
    wh = FakeWarehouse(scalar=lambda u, s: Decimal("1234.50"))
    r = engine(wh).metric(kb, "revenue", "unit_a", JAN)
    assert r["value"] == Decimal("1234.50") and r["measure"] == "net" and r["canonical"] is True
    assert r["provenance"] == {"knowledge_version": "test", "definition_version": 3,
                               "definition_effective": "2026-02-01"}
    assert r["coverage"] == {"through": (TODAY - timedelta(days=1)).isoformat(), "complete": True}
    assert r["period"] == {"start": "2026-01-01", "end": "2026-02-01", "end_exclusive": True}


def test_stale_source_is_refused_not_rendered(kb):
    wh = FakeWarehouse(newest=lambda u, s: TODAY - timedelta(days=10))
    with pytest.raises(StaleSource) as e:
        engine(wh).metric(kb, "revenue", "unit_a", JAN)
    assert e.value.kind == "failure" and e.value.reason == "stale_source"
    assert e.value.extra["covers_through"] == (TODAY - timedelta(days=10)).isoformat()


def test_empty_source_is_stale(kb):
    wh = FakeWarehouse(newest=lambda u, s: None)
    with pytest.raises(StaleSource, match="no data at all"):
        engine(wh).metric(kb, "orders", "unit_a", Window())


def test_healthy_source_behind_the_window_is_served_as_incomplete(kb):
    this_week = parse_window((TODAY - timedelta(days=3)).isoformat(), (TODAY + timedelta(days=1)).isoformat())
    r = engine(FakeWarehouse()).metric(kb, "orders", "unit_a", this_week)
    assert r["coverage"]["complete"] is False


def test_joined_source_freshness_is_checked_too(kb):
    wh = FakeWarehouse(newest=lambda u, s: TODAY - timedelta(days=9) if s == "unit_a.orders" else TODAY)
    with pytest.raises(StaleSource):
        engine(wh).metric(kb, "refunds", "unit_a", JAN)


def test_maturity_is_reported_until_the_window_settles(kb):
    eng = engine(FakeWarehouse())
    recent = parse_window("2026-10-01", "2026-10-20")
    assert eng.metric(kb, "refunds", "unit_a", recent)["maturity"]["settled"] is False
    assert eng.metric(kb, "refunds", "unit_a", JAN)["maturity"]["settled"] is True
    assert "maturity" not in eng.metric(kb, "orders", "unit_a", JAN)


def test_derived_ratio_exposes_its_components(kb):
    values = {"SUM(r.amount)": Decimal("50"), "SUM(o.gross_amount)": Decimal("1000")}
    wh = FakeWarehouse(scalar=lambda u, s: next(v for k, v in values.items() if s.sql.startswith(f"SELECT {k}")))
    r = engine(wh).metric(kb, "refund_rate", "unit_a", JAN)
    assert r["value"] == Decimal("0.05") and r["variant"] == "by_value"
    assert r["components"]["numerator"] == {"metric": "refunds", "measure": "amount", "value": Decimal("50")}
    assert r["maturity"]["settled"] is True


def test_derived_leg_pins_its_own_preset(kb):
    wh = FakeWarehouse()
    engine(wh).metric(kb, "refund_rate", "unit_a", JAN, variant="by_count")
    assert any("(o.status = 'paid')" in s for s in wh.statements())


def test_ratio_with_zero_or_missing_side_is_none_not_zero():
    assert combine("ratio", 5, 0) is None
    assert combine("ratio", None, 5) is None
    assert combine("difference", 5, None) is None
    assert combine("ratio", 1, 3) == Decimal("0.333333")


def test_independent_legs_run_concurrently(kb):
    """A barrier that only opens with both legs in flight: a change that serialised the legs
    would fail here instead of quietly doubling latency."""
    barrier = threading.Barrier(2, timeout=5)

    def scalar(unit, stmt):
        barrier.wait()
        return 10

    engine(FakeWarehouse(scalar=scalar)).metric(kb, "refund_rate", "unit_a", JAN)


def test_first_failing_leg_stops_the_request(kb):
    release = threading.Event()

    def scalar(unit, stmt):
        if "SUM(r.amount)" in stmt.sql:
            raise RuntimeError("leg failed")
        release.wait(5)
        return 1

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="leg failed"):
        engine(FakeWarehouse(scalar=scalar)).metric(kb, "refund_rate", "unit_a", JAN)
    # Without this the test would also pass if the engine waited for the slow sibling:
    # it would raise the same error, five seconds later.
    assert time.monotonic() - started < 2
    release.set()


def test_a_timed_out_statement_is_retried_once_and_only_that_statement(kb):
    attempts = {"num": 0, "den": 0}

    def scalar(unit, stmt):
        side = "num" if "SUM(r.amount)" in stmt.sql else "den"
        attempts[side] += 1
        if side == "num" and attempts["num"] == 1:
            raise StatementTimeout()
        return 4

    r = engine(FakeWarehouse(scalar=scalar)).metric(kb, "refund_rate", "unit_a", JAN)
    assert r["value"] == Decimal("1.000000")
    assert attempts == {"num": 2, "den": 1}


def test_a_second_timeout_is_the_answer(kb):
    def scalar(unit, stmt):
        raise StatementTimeout()

    with pytest.raises(QueryTimeout):
        engine(FakeWarehouse(scalar=scalar)).metric(kb, "orders", "unit_a", JAN)


def test_breakdown_flags_truncation_instead_of_cutting_silently(kb):
    rows = [(f"c{i}", 100 - i) for i in range(10)]
    wh = FakeWarehouse(rows=lambda u, s, n: (["dimension", "value"], rows))
    r = engine(wh, breakdown_row_cap=5).breakdown(kb, "orders", "unit_a", "country", JAN)
    assert r["truncated"] is True and r["row_count"] == 5 and "Partial" in r["note"]
    full = engine(wh).breakdown(kb, "orders", "unit_a", "country", JAN)
    assert full["truncated"] is False and full["row_count"] == 10


def test_breakdown_marks_personal_dimensions(kb):
    r = engine(FakeWarehouse()).breakdown(kb, "orders", "unit_a", "customer_email", JAN)
    assert r["dimension_personal"] is True


def test_breakdown_limits_are_validated(kb):
    with pytest.raises(InvalidRequest):
        engine(FakeWarehouse()).breakdown(kb, "orders", "unit_a", "country", JAN, limit=0)
    with pytest.raises(InvalidRequest):
        engine(FakeWarehouse()).breakdown(kb, "refund_rate", "unit_a", "channel", JAN)


def test_composite_totals_additive_measures_with_shares(kb):
    wh = FakeWarehouse(scalar=lambda u, s: Decimal("300") if u == "unit_a" else Decimal("100"))
    r = engine(wh).composite(kb, "revenue", ["unit_a", "unit_b"], JAN, measure="net")
    assert r["total"] == Decimal("400") and r["additive"] is True
    assert r["units"]["unit_a"]["share"] == Decimal("0.7500")
    assert r["units"]["unit_b"]["measure"] == "net_sales"      # resolved through the alias


def test_composite_does_not_total_a_non_additive_measure(kb):
    r = engine(FakeWarehouse()).composite(kb, "orders", ["unit_a", "unit_b"], JAN, measure="customers")
    assert r["total"] is None and r["additive"] is False
    assert all("share" not in u for u in r["units"].values())


def test_composite_covers_only_the_units_it_was_given(kb):
    r = engine(FakeWarehouse()).composite(kb, "revenue", ["unit_a"], JAN)
    assert r["units_covered"] == ["unit_a"] and set(r["units"]) == {"unit_a"}


def test_composite_refuses_entirely_when_one_unit_is_stale(kb):
    wh = FakeWarehouse(newest=lambda u, s: TODAY - timedelta(days=30) if u == "unit_b" else TODAY)
    with pytest.raises(StaleSource):
        engine(wh).composite(kb, "revenue", ["unit_a", "unit_b"], JAN)


def test_compare_reports_change_and_percentage(kb):
    wh = FakeWarehouse(scalar=lambda u, s: Decimal("120") if s.params["start"].month == 2 else Decimal("100"))
    r = engine(wh).compare(kb, "orders", "unit_a", parse_window("2026-02-01", "2026-03-01"), JAN)
    assert r["change"] == Decimal("20") and r["change_pct"] == Decimal("0.2000")
    with pytest.raises(InvalidRequest):
        engine(wh).compare(kb, "orders", "unit_a", Window(), JAN)


def test_cache_is_keyed_on_knowledge_version(kb):
    calls = []
    wh = FakeWarehouse(scalar=lambda u, s: calls.append(1) or 1)
    eng = engine(wh, cache_ttl_s=60)
    eng.metric(kb, "orders", "unit_a", JAN)
    assert eng.metric(kb, "orders", "unit_a", JAN)["cached"] is True
    eng.metric(replace(kb, version="next"), "orders", "unit_a", JAN)
    assert len(calls) == 2


def test_measure_and_variant_are_not_interchangeable(kb):
    eng = engine(FakeWarehouse())
    with pytest.raises(InvalidRequest):
        eng.metric(kb, "orders", "unit_a", JAN, variant="x")
    with pytest.raises(InvalidRequest):
        eng.metric(kb, "refund_rate", "unit_a", JAN, measure="amount")
    with pytest.raises(InvalidRequest):
        eng.metric(kb, "refund_rate", "unit_a", JAN, variant="nope")
    with pytest.raises(InvalidRequest):
        eng.metric(kb, "nope", "unit_a", JAN)
    with pytest.raises(InvalidRequest):
        eng.metric(kb, "active_products", "unit_b", Window())
