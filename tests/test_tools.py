"""The tool layer end to end over a fake warehouse: authorisation, error kinds, masking, audit."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from governed_metrics import overlay
from governed_metrics import tools as T
from governed_metrics.errors import QueryTimeout
from governed_metrics.tools import governed

from .fakes import TODAY

JAN = {"start": "2026-01-01", "end": "2026-02-01"}


def audit_lines(rt):
    return list(rt.audit.read())


# ---- authorisation -------------------------------------------------------------------------

def test_a_caller_without_a_grant_is_denied_and_the_denial_is_logged(rt):
    r = T.get_metric(rt, "stranger@example.com", metric="orders", unit="unit_a", **JAN)
    assert (r["error"], r["reason"]) == ("access_denied", "no_grant")
    [line] = audit_lines(rt)
    assert (line["outcome"], line["reason"], line["caller"]) == ("access_denied", "no_grant", "stranger@example.com")


def test_an_empty_identity_is_denied(rt):
    assert T.list_metrics(rt, "")["error"] == "access_denied"


def test_unit_outside_the_grant_is_denied(rt):
    r = T.get_metric(rt, "analyst@example.com", metric="orders", unit="unit_b", **JAN)
    assert (r["error"], r["reason"]) == ("access_denied", "unit_not_permitted")


def test_unknown_unit_is_an_invalid_request(rt):
    assert T.get_metric(rt, "lead@example.com", metric="orders", unit="unit_z", **JAN)["reason"] == "unknown_unit"


def test_metric_allow_list_is_checked_before_existence(rt):
    for name in ("refunds", "does_not_exist"):
        r = T.get_metric(rt, "partner@example.com", metric=name, unit="unit_a", **JAN)
        assert (r["error"], r["reason"]) == ("access_denied", "metric_not_permitted")


def test_unknown_metric_suggests_only_visible_names(rt):
    r = T.get_metric(rt, "lead@example.com", metric="revenu", unit="unit_a", **JAN)
    assert r["reason"] == "unknown_metric" and "Did you mean 'revenue'" in r["detail"]


def test_dimension_restrictions_apply_to_breakdowns_and_filters(rt):
    ok = T.get_metric_breakdown(rt, "partner@example.com", metric="revenue", unit="unit_a", by="channel", **JAN)
    assert ok["kind"] == "breakdown"
    for kwargs in ({"by": "country"}, {"by": "channel", "filters": {"country": "GB"}}):
        r = T.get_metric_breakdown(rt, "partner@example.com", metric="revenue", unit="unit_a", **kwargs, **JAN)
        assert r["reason"] == "dimension_not_permitted"


def test_derived_metric_needs_access_to_its_legs(rt, policy_dir):
    (policy_dir / "access.grants.yaml").write_text(
        "users:\n  ratio@example.com:\n    units: [unit_a]\n    metrics: {unit_a: [refund_rate, revenue]}\n")
    rt.refresh_policy()
    r = T.get_metric(rt, "ratio@example.com", metric="refund_rate", unit="unit_a", **JAN)
    assert r["reason"] == "metric_not_permitted" and "'refunds'" in r["detail"]


def test_composite_covers_only_permitted_units(rt):
    r = T.get_composite_metric(rt, "analyst@example.com", metric="revenue", **JAN)
    assert r["units_covered"] == ["unit_a"] and set(r["units"]) == {"unit_a"}


def test_composite_denial_does_not_reveal_what_exists(rt):
    hidden = T.get_composite_metric(rt, "analyst@example.com", metric="active_products_b", **JAN)
    missing = T.get_composite_metric(rt, "analyst@example.com", metric="nonexistent", **JAN)
    assert hidden["detail"].replace("active_products_b", "X") == missing["detail"].replace("nonexistent", "X")


def test_filters_must_be_an_object(rt):
    r = T.get_metric(rt, "lead@example.com", metric="orders", unit="unit_a", filters=["web"], **JAN)
    assert r["reason"] == "invalid_filter"


# ---- answers -------------------------------------------------------------------------------

def test_canonical_answer_shape_and_verbose_statement(rt):
    r = T.get_metric(rt, "lead@example.com", metric="revenue", unit="unit_a", **JAN)
    assert r["kind"] == "metric" and r["canonical"] is True and "statement" not in r
    assert r["provenance"]["knowledge_version"] == "test"
    v = T.get_metric(rt, "lead@example.com", metric="revenue", unit="unit_a", verbose=True, **JAN)
    assert "%(start)s" in v["statement"]["sql"] and v["statement"]["params"]["start"] == "2026-01-01"


def test_stale_source_is_a_failure_with_its_own_reason(rt, warehouse):
    warehouse.newest_fn = lambda unit, source: TODAY - timedelta(days=20)
    r = T.get_metric(rt, "lead@example.com", metric="orders", unit="unit_b", **JAN)
    assert (r["error"], r["reason"]) == ("failure", "stale_source")
    assert r["covers_through"] == (TODAY - timedelta(days=20)).isoformat()
    assert audit_lines(rt)[-1]["reason"] == "stale_source"


def test_timeout_is_its_own_kind(rt, warehouse):
    def boom(unit, stmt):
        raise QueryTimeout("slow")
    warehouse.scalar_fn = boom
    assert T.get_metric(rt, "lead@example.com", metric="orders", unit="unit_a", **JAN)["error"] == "timeout"


def test_unexpected_errors_become_a_generic_failure(rt, warehouse):
    def boom(unit, stmt):
        raise RuntimeError("password=hunter2 host=db.internal")
    warehouse.scalar_fn = boom
    r = T.get_metric(rt, "lead@example.com", metric="orders", unit="unit_a", **JAN)
    assert (r["error"], r["reason"]) == ("failure", "internal")
    assert "hunter2" not in json.dumps(r) and "db.internal" not in json.dumps(r)


def test_decimals_and_dates_leave_as_json(rt, warehouse):
    from decimal import Decimal
    warehouse.scalar_fn = lambda u, s: Decimal("12.50")
    r = T.get_metric(rt, "lead@example.com", metric="revenue", unit="unit_a", **JAN)
    assert r["value"] == 12.5 and isinstance(json.dumps(r), str)


def test_catalog_lists_only_what_the_caller_may_read(rt):
    r = T.list_metrics(rt, "partner@example.com")
    assert {m["key"] for m in r["metrics"]} == {"orders", "revenue"}
    revenue = next(m for m in r["metrics"] if m["key"] == "revenue")
    assert revenue["units"]["unit_a"]["dimensions"] == ["channel"]
    assert {m["key"] for m in T.list_metrics(rt, "analyst@example.com")["metrics"]} >= {"refund_rate", "active_products"}


def test_rules_and_glossary(rt):
    assert T.resolve_rule(rt, "analyst@example.com", domain="shipping", key="free_shipping_threshold",
                          unit="unit_a")["value"] == 60
    assert T.resolve_rule(rt, "analyst@example.com", domain="shipping", key="returns_window_days",
                          unit="unit_a")["reason"] == "rule_unverified"
    assert T.resolve_rule(rt, "analyst@example.com", domain="shipping", key="free_shipping_threshold",
                          unit="unit_a", as_of="2025-02-01")["value"] == 50
    assert "countable order" in T.get_glossary(rt, "analyst@example.com")["terms"]
    assert T.get_glossary(rt, "analyst@example.com", term="nope")["reason"] == "unknown_term"
    assert T.list_rules(rt, "analyst@example.com")["kind"] == "rules"


# ---- masking -------------------------------------------------------------------------------

def test_personal_breakdown_is_masked_for_most_callers_only(rt, warehouse):
    warehouse.rows_fn = lambda u, s, n: (["dimension", "value"], [("jane@example.com", 3)])
    args = {"metric": "orders", "unit": "unit_a", "by": "customer_email", **JAN}
    assert T.get_metric_breakdown(rt, "lead@example.com", **args)["rows"][0]["dimension"] == "j***@example.com"
    assert T.get_metric_breakdown(rt, "steward@example.com", **args)["rows"][0]["dimension"] == "jane@example.com"


def test_every_governed_tool_passes_the_masking_chokepoint(rt):
    @governed("rogue")
    def rogue(rt, kb, policy, caller):
        return {"value": "a@example.com"}          # no kind: the chokepoint must refuse it

    r = rogue(rt, "lead@example.com")
    assert (r["error"], r["reason"]) == ("failure", "unrecognised_result_shape")
    assert "a@example.com" not in json.dumps(r)


# ---- ad hoc --------------------------------------------------------------------------------

def test_adhoc_is_a_privilege(rt):
    r = T.run_query(rt, "analyst@example.com", unit="unit_a", sql="select 1")
    assert (r["error"], r["reason"]) == ("access_denied", "adhoc_not_permitted")
    assert T.list_sources(rt, "analyst@example.com", unit="unit_a")["reason"] == "adhoc_not_permitted"


def test_adhoc_result_is_labelled_capped_and_masked(rt, warehouse):
    rows = [(i, f"user{i}@example.com") for i in range(5)]
    warehouse.adhoc_fn = lambda u, sql, n: (["order_id", "customer_email"], rows)
    r = T.run_query(rt, "lead@example.com", unit="unit_a",
                    sql="select order_id, customer_email from unit_a.orders", max_rows=3)
    assert r["canonical"] is False and r["label"].startswith("NON-CANONICAL")
    assert r["row_count"] == 3 and r["truncated"] is True
    assert r["rows"][0] == [0, "u***@example.com"] and r["masked_columns"] == ["customer_email"]
    assert audit_lines(rt)[-1]["rows_band"] == "1-10"


def test_adhoc_source_allow_list_is_per_caller(rt):
    r = T.run_query(rt, "lead@example.com", unit="unit_b", sql="select * from unit_b.refunds")
    assert r["reason"] == "source_not_permitted"
    assert [s["source"] for s in T.list_sources(rt, "lead@example.com", unit="unit_b")["sources"]] == ["unit_b.orders"]


def test_adhoc_refusals_never_reach_the_warehouse(rt, warehouse):
    r = T.run_query(rt, "lead@example.com", unit="unit_a", sql="select * from unit_a.orders, unit_b.orders")
    assert r["reason"] == "source_not_in_unit" and warehouse.statements("adhoc") == []


def test_audit_never_records_query_text(rt):
    T.run_query(rt, "lead@example.com", unit="unit_a", sql="select 'secret-literal' from unit_a.orders")
    with open(rt.audit.path) as f:
        raw = f.read()
    assert "secret-literal" not in raw and "select" not in raw


def test_adhoc_rate_limit(rt):
    rt.adhoc_rate.per_minute = 1
    T.run_query(rt, "lead@example.com", unit="unit_a", sql="select 1")
    assert T.run_query(rt, "lead@example.com", unit="unit_a", sql="select 1")["reason"] == "rate_limited"


def test_max_rows_is_bounded(rt):
    assert T.run_query(rt, "lead@example.com", unit="unit_a", sql="select 1", max_rows=10**6)["reason"] == "invalid_limit"


def test_describe_source_flags_personal_columns(rt, warehouse):
    warehouse.rows_fn = lambda u, s, n: (["column_name", "data_type"], [("order_id", "bigint"), ("customer_email", "text")])
    r = T.describe_source(rt, "lead@example.com", unit="unit_a", source="unit_a.orders")
    assert [c["personal"] for c in r["columns"]] == [False, True]
    assert T.describe_source(rt, "lead@example.com", unit="unit_b", source="unit_b.refunds")["reason"] == "source_not_permitted"


def test_caller_sql_errors_come_back_as_invalid_requests(rt, warehouse):
    import psycopg

    def bad(unit, sql, n):
        raise psycopg.errors.UndefinedColumn('column "nope" does not exist')
    warehouse.adhoc_fn = bad
    r = T.run_query(rt, "lead@example.com", unit="unit_a", sql="select nope from unit_a.orders")
    assert r["reason"] == "sql_error" and 'column "nope"' in r["detail"]


# ---- administration ------------------------------------------------------------------------

def test_reload_is_admin_only(rt, policy_dir):
    assert T.reload_knowledge(rt, "lead@example.com")["reason"] == "admin_only"
    (policy_dir / "access.yaml").write_text(
        (policy_dir / "access.yaml").read_text().replace("analyst@example.com:", "admin@example.com:\n    units: [unit_a]\n  analyst@example.com:"))
    rt.refresh_policy()
    r = T.reload_knowledge(rt, "admin@example.com")
    assert r["kind"] == "reload" and r["policy_ok"] is True


def test_console_grant_takes_effect_on_the_next_call(rt):
    before = T.list_metrics(rt, "new@example.com")
    assert before["reason"] == "no_grant"
    assert T.admin_grant(rt, "admin@example.com", "new@example.com", ["unit_a"], "joined")
    assert T.list_metrics(rt, "new@example.com")["kind"] == "catalog"
    assert T.admin_revoke(rt, "admin@example.com", "new@example.com", "left")
    assert T.list_metrics(rt, "new@example.com")["reason"] == "no_grant"


def test_overlay_change_from_outside_is_picked_up_without_a_restart(rt, settings):
    overlay.grant(settings.overlay_path, "late@example.com", ["unit_b"], by="admin@example.com", reason="r")
    assert T.list_metrics(rt, "late@example.com")["kind"] == "catalog"


@pytest.mark.parametrize("caller", ["lead@example.com", "stranger@example.com"])
def test_every_call_writes_exactly_one_audit_line(rt, caller):
    T.get_metric(rt, caller, metric="orders", unit="unit_a", **JAN)
    assert len(audit_lines(rt)) == 1
