"""Against a real PostgreSQL warehouse built from examples/warehouse/init.

Skipped unless GM_TEST_DSN_UNIT_A and GM_TEST_DSN_UNIT_B point at the two read-only roles.
CI runs these against a service container (.github/workflows/ci.yml).

The first tests bypass every application guard on purpose and talk to the warehouse
directly: they prove the database role holds on its own (ADR 0006).
"""
from __future__ import annotations

import os
from datetime import date

import psycopg
import pytest

from governed_metrics.engine.compiler import Statement, compile_match_count, parse_window
from governed_metrics.engine.engine import MetricEngine
from governed_metrics.errors import StaleSource
from governed_metrics.knowledge.model import StructuredDefinition
from governed_metrics.warehouse import PostgresWarehouse

pytestmark = pytest.mark.integration
DSNS = {"unit_a": os.environ.get("GM_TEST_DSN_UNIT_A"), "unit_b": os.environ.get("GM_TEST_DSN_UNIT_B")}
if not all(DSNS.values()):
    pytest.skip("GM_TEST_DSN_UNIT_A / GM_TEST_DSN_UNIT_B not set", allow_module_level=True)


@pytest.fixture(scope="module")
def wh():
    w = PostgresWarehouse(lambda unit: DSNS[unit], statement_timeout_ms=10_000, pool_max_size=1)
    yield w
    w.close()


@pytest.fixture(scope="module")
def engine(wh):
    return MetricEngine(wh, cache_ttl_s=0, freshness_ttl_s=0)


def last_full_months(n: int):
    first = date.today().replace(day=1)
    months = []
    for _ in range(n + 1):
        months.insert(0, first)
        first = (first.replace(day=1) - date.resolution).replace(day=1)
    return months


# ---- the database role is the boundary -----------------------------------------------------

def test_one_units_role_cannot_read_another_unit(wh):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        wh.adhoc("unit_a", "select count(*) from unit_b.orders", 1)


def test_roles_are_read_only_even_without_the_guard(wh):
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        wh.scalar("unit_a", Statement("CREATE TABLE unit_a.x (id int)", {}))


def test_session_changes_do_not_survive_the_pool(wh):
    wh.scalar("unit_a", Statement("SELECT set_config('statement_timeout', '0', false)", {}))
    assert wh.scalar("unit_a", Statement("SELECT current_setting('statement_timeout')", {})) == "10s"
    assert wh.scalar("unit_a", Statement("SELECT current_setting('search_path')", {})) == "pg_catalog"


def test_adhoc_fetches_only_the_capped_rows(wh):
    cols, rows = wh.adhoc("unit_a", "select order_id from unit_a.orders order by 1", 5)
    assert cols == ["order_id"] and len(rows) == 5


# ---- canonical numbers are internally consistent (docs/quality.md) -------------------------

def test_every_filter_still_matches_rows(kb, wh):
    for metric in kb.metrics.values():
        for unit, d in metric.units.items():
            if isinstance(d, StructuredDefinition):
                for preset in (None, *d.presets):
                    assert wh.scalar(unit, compile_match_count(d, preset)) > 0, (metric.key, unit, preset)


def test_adjacent_periods_add_up_to_the_whole(kb, engine):
    a, b, c = last_full_months(2)[-3:]
    first = engine.metric(kb, "orders", "unit_a", parse_window(a, b))["value"]
    second = engine.metric(kb, "orders", "unit_a", parse_window(b, c))["value"]
    both = engine.metric(kb, "orders", "unit_a", parse_window(a, c))["value"]
    assert first + second == both


def test_a_breakdown_sums_to_its_total(kb, engine):
    a, b = last_full_months(1)[-2:]
    window = parse_window(a, b)
    total = engine.metric(kb, "revenue", "unit_a", window, measure="gross")["value"]
    parts = engine.breakdown(kb, "revenue", "unit_a", "channel", window, measure="gross")
    assert parts["truncated"] is False
    assert sum(r["value"] for r in parts["rows"]) == total


def test_the_stopped_feed_is_refused(kb, engine):
    a, b = last_full_months(1)[-2:]
    with pytest.raises(StaleSource):
        engine.metric(kb, "refund_rate", "unit_b", parse_window(a, b))
    assert engine.metric(kb, "refund_rate", "unit_a", parse_window(a, b))["value"] is not None
