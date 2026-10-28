"""Guard regressions, written as attacks (docs/quality.md).

Each attack asserts the *specific* reason it is refused for. A test that only checked "was
refused" could pass against vulnerable code because the statement was refused for an
unrelated reason, which buys confidence it has not earned.
"""
from __future__ import annotations

import pytest

from governed_metrics.adhoc import guard
from governed_metrics.errors import AccessDenied, InvalidRequest


@pytest.fixture
def unit_a(kb):
    return kb.units["unit_a"]


def approve(unit, sql, *, sources=("*",), unmasked=True):
    return guard.check(sql, unit=unit, caller_sources=frozenset(sources), unmasked=unmasked)


def refused(unit, sql, *, sources=("*",), unmasked=True):
    with pytest.raises((InvalidRequest, AccessDenied)) as e:
        approve(unit, sql, sources=sources, unmasked=unmasked)
    return e.value.reason


# ---- legitimate shapes must pass (fail-closed fixes are made here, never by loosening) ----

@pytest.mark.parametrize("sql", [
    "select channel, sum(gross_amount) from unit_a.orders group by 1 order by 2 desc",
    "select o.channel, count(*) from unit_a.orders o join unit_a.refunds r on r.order_id = o.order_id group by 1",
    "select * from unit_a.orders o left join unit_a.refunds r using (order_id)",
    "with paid as (select * from unit_a.orders where status = 'paid') select count(*) from paid",
    "with recursive n(i) as (select 1 union all select i + 1 from n where i < 5) select * from n",
    "select extract(year from ordered_at), count(*) from unit_a.orders group by 1",
    "select substring(channel from 1 for 2), trim(both ' ' from country) from unit_a.orders",
    "select count(*) filter (where status = 'paid') from unit_a.orders",
    "select percentile_cont(0.5) within group (order by gross_amount) from unit_a.orders",
    "select date_trunc('month', ordered_at)::date, round(avg(gross_amount)::numeric(12,2), 2) from unit_a.orders group by 1",
    "select cast(gross_amount as numeric(12,2)) from unit_a.orders",
    "select * from unit_a.orders where status is distinct from 'test' and order_id in (select order_id from unit_a.refunds)",
    "select * from generate_series(1, 3) as g(n)",
    "select 'copy; drop table x -- from unit_b.orders' as note from unit_a.orders",
    "select g.n, (select count(*) from unit_a.orders) from generate_series(1, 2) g(n)",
    "select * from (select order_id from unit_a.orders) t join unit_a.refunds r on r.order_id = t.order_id",
    "select 1 from unit_a.orders fetch first 5 rows only",
    "select rank() over (partition by channel order by gross_amount desc) from unit_a.orders",
    "select 1;;",
])
def test_legitimate_statements_are_approved(unit_a, sql):
    approved = approve(unit_a, sql)
    assert approved.sql and not approved.sql.endswith(";")


def test_approved_text_is_exactly_what_was_analysed(unit_a):
    approved = approve(unit_a, "  select 1 from unit_a.orders ; ")
    assert approved.sql == "select 1 from unit_a.orders"
    assert approved.sources == ("unit_a.orders",)


# ---- cross-unit reads: every spelling of a second source is found -------------------------

@pytest.mark.parametrize("sql", [
    "select * from unit_a.orders a, unit_b.orders b",
    "select * from unit_a.orders /* hidden */ , unit_b.orders",
    'select * from unit_a.orders, "unit_b"."orders"',
    "select * from unit_a.orders a join unit_b.orders b on true",
    "select * from (unit_a.orders a cross join unit_b.orders b)",
    "select (select max(order_id) from unit_b.orders) from unit_a.orders",
    "select * from unit_a.orders where order_id in (select order_id from unit_b.orders)",
    "with x as (select * from unit_b.orders) select * from x",
    "select * from unit_a.orders o, lateral (select * from unit_b.orders) b",
    "select * from only unit_b.orders",
    "select extract(year from (select max(ordered_at) from unit_b.orders))",
    "select * from unit_a.orders where status is not distinct from (select status from unit_b.orders limit 1)",
    "select * from (select * from unit_a.orders) t, unit_b.orders",
    "select * from unit_a.orders a join (unit_a.refunds r join unit_b.orders b on true) on true",
])
def test_another_units_source_is_refused(unit_a, sql):
    assert refused(unit_a, sql) == "source_not_in_unit"


@pytest.mark.parametrize("sql", [
    "select * from pg_catalog.pg_stats",
    "select * from information_schema.columns",
    "select * from pg_catalog.pg_authid",
    "select * from public.anything",
])
def test_catalogues_and_undeclared_schemas_are_refused(unit_a, sql):
    assert refused(unit_a, sql) == "source_not_in_unit"


def test_source_outside_the_callers_own_list_is_refused(unit_a):
    assert refused(unit_a, "select * from unit_a.refunds", sources=["unit_a.orders"]) == "source_not_permitted"


# ---- references the parser cannot classify are refused (ADR 0005) -------------------------

@pytest.mark.parametrize("sql", [
    "select * from orders",                                         # search_path decides
    "with a as (select * from b), b as (select 1) select * from a",  # forward ref = real table b
    "with a as (select * from a) select * from a",                   # non-recursive self ref
    "select * from (with x as (select 1) select * from x) t, x",     # CTE used outside its scope
    "select * from db.unit_a.orders",                                # three-part name
])
def test_unclassifiable_references_are_refused(unit_a, sql):
    assert refused(unit_a, sql) == "unclassifiable_reference"


def test_rows_from_is_refused(unit_a):
    assert refused(unit_a, "select * from rows from (generate_series(1,2))") == "unsupported_syntax"


def test_table_syntax_is_refused_everywhere(unit_a):
    assert refused(unit_a, "table unit_b.orders") == "not_a_select"
    assert refused(unit_a, "select * from unit_a.orders where order_id in (table unit_b.orders)") == \
        "unsupported_syntax"


# ---- statement shape: one read-only SELECT -------------------------------------------------

@pytest.mark.parametrize("sql,reason", [
    ("select 1; select 2", "multiple_statements"),
    ("select 1; drop table unit_a.orders", "multiple_statements"),
    ("delete from unit_a.orders", "not_a_select"),
    ("explain select 1", "not_a_select"),
    ("(select 1)", "not_a_select"),
    ("with d as (delete from unit_a.orders returning *) select * from d", "write_statement"),
    ("with u as (update unit_a.orders set status = 'x' returning 1) select 1", "write_statement"),
    ("with i as (insert into unit_a.orders values (1) returning 1) select 1", "write_statement"),
    ("select * into unit_a.copy from unit_a.orders", "write_statement"),
    ("select * from unit_a.orders for update", "write_statement"),
    ("select * from unit_a.orders for share", "write_statement"),
    ("select * from unit_a.orders for no key update", "write_statement"),
    ("select (1", "unbalanced"),
    ("select 1)", "unbalanced"),
    ("", "not_a_select"),
])
def test_anything_but_one_read_only_select_is_refused(unit_a, sql, reason):
    assert refused(unit_a, sql) == reason


# ---- functions are allow-listed --------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "select pg_sleep(10)",
    "select set_config('search_path', 'unit_b', false)",
    "select current_setting('search_path')",
    "select pg_read_file('/etc/passwd')",
    "select query_to_xml('select * from unit_b.orders', true, true, '')",
    "select dblink('host=x', 'select 1')",
    "select nextval('unit_a.seq')",
    "select unit_a.custom_fn(1)",
    "select pg_catalog.pg_sleep(1)",
    'select "pg_sleep"(1)',
    "select lo_import('/tmp/x')",
    "select pg_terminate_backend(1)",
    "select row_to_json(o) from unit_a.orders o",
    "select operator(pg_catalog.+)(1, 2)",
    "select * from xmltable('/a' passing '<a/>' columns x int)",
    "select * from unit_a.orders tablesample bernoulli (10)",
])
def test_functions_outside_the_allow_list_are_refused(unit_a, sql):
    assert refused(unit_a, sql) == "function_not_permitted"


def test_only_allow_listed_set_returning_functions_in_from(unit_a):
    assert refused(unit_a, "select * from json_each('{}')") == "function_not_permitted"
    assert refused(unit_a, "select * from pg_catalog.pg_ls_dir('.')") == "function_not_permitted"


def test_qualified_allow_listed_function_from_pg_catalog_is_fine(unit_a):
    approve(unit_a, "select pg_catalog.lower(channel) from unit_a.orders")


# ---- lexical refusals surface as their own reason ------------------------------------------

@pytest.mark.parametrize("sql", ["select 'open", "select u&'x'", "select * from unit_b.orders"])
def test_lexical_ambiguity_is_refused(unit_a, sql):
    assert refused(unit_a, sql) == "lexical_error"


def test_overlong_statement_is_refused(unit_a):
    assert refused(unit_a, "select " + "1," * 10_001 + "1") == "statement_too_long"


# ---- regressions found in the adversarial review --------------------------------------------

@pytest.mark.parametrize("sql", [
    # Outside FROM, `word word(args)` is keyword syntax followed by a call, never an alias list.
    "select x at time zone pg_sleep(channel) from unit_a.orders",
    "select gross_amount from unit_a.orders order by gross_amount nulls first, pg_sleep(channel)",
    "select sum(gross_amount) over (order by ordered_at rows pg_sleep(channel) preceding) from unit_a.orders",
])
def test_calls_after_keyword_syntax_are_still_allow_listed(unit_a, sql):
    assert refused(unit_a, sql) == "function_not_permitted"


def test_deep_nesting_is_refused_not_crashed(unit_a):
    sql = "select * from " + "(" * 3000 + "unit_a.orders" + ")" * 3000
    assert refused(unit_a, sql) == "unsupported_syntax"


def test_not_distinct_from_needs_is(unit_a):
    from governed_metrics.adhoc.references import analyze
    a = analyze("select 1 from unit_a.orders where a is not distinct from b")
    assert not a.problems
    assert any(t.value == "from" for i, t in enumerate(a.tokens) if i not in a.table_from)


@pytest.mark.parametrize("fn", ["lpad", "rpad"])
def test_functions_that_build_unbounded_values_are_refused(unit_a, fn):
    assert refused(unit_a, f"select {fn}('x', 1000000000)") == "function_not_permitted"


def test_aliases_that_are_unreserved_keywords_still_parse(unit_a):
    approve(unit_a, "select first.channel from unit_a.orders first")
    approve(unit_a, "select o.channel from unit_a.orders * o")


def test_alias_lists_are_only_recognised_in_from(unit_a):
    """`value pg_sleep(x)` outside FROM must be treated as a call (and refused), never as an
    alias with a column list, which would skip the function allow-list."""
    assert refused(unit_a, "select 1 from unit_a.orders where value pg_sleep(channel)") == "function_not_permitted"
