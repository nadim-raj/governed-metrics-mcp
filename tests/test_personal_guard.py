"""A personal column may leave only under its own name, untransformed (ADR 0007)."""
from __future__ import annotations

import pytest

from governed_metrics.adhoc import guard
from governed_metrics.errors import InvalidRequest


def check(kb, sql, *, unmasked=False):
    return guard.check(sql, unit=kb.units["unit_a"], caller_sources=frozenset({"*"}), unmasked=unmasked)


@pytest.mark.parametrize("sql", [
    "select customer_email from unit_a.orders",
    "select o.customer_email, count(*) from unit_a.orders o group by o.customer_email",
    "select distinct customer_email from unit_a.orders order by customer_email desc",
    "select count(distinct customer_email) from unit_a.orders",
    "select count(customer_email) from unit_a.orders",
    "select * from unit_a.orders where customer_email = 'someone@example.com'",
    "select * from unit_a.orders where customer_email is not null and channel = 'web'",
    "select * from unit_a.orders where (customer_email <> 'a@example.com' or channel = 'app')",
    "select * from unit_a.orders where customer_email in ('a@example.com', 'b@example.com')",
    "select * from unit_a.orders a join unit_a.orders b on a.customer_email = b.customer_email",
    "select * from unit_a.orders a join unit_a.orders b using (customer_email)",
    "select * from unit_a.orders",
    "select o.* from unit_a.orders o",
    "with c as (select customer_email, channel from unit_a.orders) select * from c",
    "select t.customer_email from (select customer_email from unit_a.orders) t",
    "select order_id from unit_a.orders where customer_email in (select customer_email from unit_a.orders where channel = 'app')",
    "select rank() over (partition by customer_email order by ordered_at) from unit_a.orders",
    "select channel as customer_email from unit_a.orders",
])
def test_positions_that_keep_the_value_under_its_own_name_are_allowed(kb, sql):
    check(kb, sql)


@pytest.mark.parametrize("sql", [
    "select customer_email as contact from unit_a.orders",
    "select customer_email contact from unit_a.orders",
    "select lower(customer_email) from unit_a.orders",
    "select split_part(customer_email, '@', 1) from unit_a.orders",
    "select left(customer_email, 3) from unit_a.orders",
    "select customer_email || '' from unit_a.orders",
    "select '' || customer_email from unit_a.orders",
    "select customer_email::text from unit_a.orders",
    "select (customer_email) from unit_a.orders",
    "select case when true then customer_email end from unit_a.orders",
    "select coalesce(customer_email, 'x') from unit_a.orders",
    "select min(customer_email) from unit_a.orders",
    "select string_agg(customer_email, ',') from unit_a.orders",
    "select array[customer_email] from unit_a.orders",
    "select * from unit_a.orders where customer_email like 'a%'",
    "select * from unit_a.orders where lower(customer_email) = 'a@example.com'",
    "select (select customer_email from unit_a.orders limit 1) as c",
    "select array(select customer_email from unit_a.orders)",
    "select count(distinct lower(customer_email)) from unit_a.orders",
    "select * from unit_a.orders order by length(customer_email)",
    'select "customer_email" as x from unit_a.orders',
])
def test_transformations_and_renames_are_refused(kb, sql):
    with pytest.raises(InvalidRequest) as e:
        check(kb, sql)
    assert e.value.reason == "personal_data_reconstruction"


@pytest.mark.parametrize("sql", [
    "select o from unit_a.orders o",
    "select orders from unit_a.orders",
    "select (o.*)::text from unit_a.orders o",
    "select t from (select customer_email from unit_a.orders) t",
    "select c from (select 1) x, lateral (select customer_email from unit_a.orders) c",
    "with c as (select customer_email from unit_a.orders) select c from c",
])
def test_whole_row_references_are_refused(kb, sql):
    with pytest.raises(InvalidRequest) as e:
        check(kb, sql)
    assert e.value.reason == "personal_data_reconstruction"


@pytest.mark.parametrize("sql", [
    "select customer_email from unit_a.orders union select channel from unit_a.orders",
    "select channel from unit_a.orders union all select customer_email from unit_a.orders",
    "with c(x) as (select customer_email from unit_a.orders) select x from c",
    "select x from (select customer_email from unit_a.orders) t(x)",
])
def test_relabelling_constructs_are_refused(kb, sql):
    with pytest.raises(InvalidRequest) as e:
        check(kb, sql)
    assert e.value.reason == "personal_data_reconstruction"


def test_statements_without_personal_sources_are_not_restricted(kb):
    check(kb, "select reason from unit_a.refunds union select reason from unit_a.refunds")
    check(kb, "select x from (select reason from unit_a.refunds) t(x)")


def test_unmasked_callers_are_not_restricted(kb):
    check(kb, "select lower(customer_email) as e from unit_a.orders", unmasked=True)


# ---- regressions found in the adversarial review --------------------------------------------

@pytest.mark.parametrize("sql", [
    # Unreserved keywords are valid bare aliases in PostgreSQL; a whole-row use must still be seen.
    "select first from unit_a.orders first",
    "select preceding from unit_a.orders preceding",
    "select zone from unit_a.orders as zone",
    # `table *` (legacy inheritance syntax) must not hide the alias that follows it.
    "select o from unit_a.orders * as o",
    "select o from only unit_a.orders * o",
])
def test_whole_row_through_unusual_alias_spellings_is_refused(kb, sql):
    with pytest.raises(InvalidRequest) as e:
        check(kb, sql)
    assert e.value.reason == "personal_data_reconstruction"
