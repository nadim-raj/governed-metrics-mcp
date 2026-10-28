"""Validators catch definitions that parse cleanly and are still wrong in shape."""
from __future__ import annotations

import shutil
import textwrap

import pytest

from governed_metrics.errors import KnowledgeError
from governed_metrics.knowledge import fragments as F
from governed_metrics.knowledge.loader import knowledge_version, load_knowledge

from .conftest import EXAMPLES


@pytest.fixture
def kdir(tmp_path):
    target = tmp_path / "knowledge"
    shutil.copytree(EXAMPLES / "knowledge", target)
    return target


def problems(kdir) -> str:
    with pytest.raises(KnowledgeError) as e:
        load_knowledge(str(kdir))
    return "\n".join(e.value.problems)


def edit(path, old, new):
    text = path.read_text()
    assert old in text, f"fixture text not found in {path.name}"
    path.write_text(text.replace(old, new))


def test_example_knowledge_loads(kb):
    assert set(kb.metrics) == {"orders", "revenue", "refunds", "refund_rate", "active_products"}


def test_inlining_a_fragment_is_refused(kdir):
    edit(kdir / "metrics" / "orders.yaml", '"{{ countable_order(o) }}"', "\"o.status <> 'test'   AND NOT  o.is_internal\"")
    assert "inlines the rule of fragment(s) ['countable_order']" in problems(kdir)


def test_unreferenced_fragment_is_an_orphan(kdir):
    (kdir / "fragments.yaml").write_text(textwrap.dedent("""
        fragments:
          countable_order: {description: x, sql: "{a}.status <> 'test' AND NOT {a}.is_internal"}
          unused_rule: {description: y, sql: "{a}.channel = 'web'"}
    """))
    assert "fragment 'unused_rule' is not referenced" in problems(kdir)


def test_unknown_fragment_reference_is_refused(kdir):
    edit(kdir / "metrics" / "orders.yaml", "{{ countable_order(o) }}", "{{ countable_ordr(o) }}")
    assert "unknown fragment(s) ['countable_ordr']" in problems(kdir)


def test_every_metric_declares_its_anchoring(kdir):
    edit(kdir / "metrics" / "orders.yaml", "    period_column: o.ordered_at\n", "", )
    assert "declare period_column, or snapshot: true" in problems(kdir)


def test_late_arriving_source_requires_maturity(kdir):
    text = (kdir / "metrics" / "refunds.yaml").read_text()
    head, _, rest = text.partition("    maturity:\n")
    (kdir / "metrics" / "refunds.yaml").write_text(head + rest.split("\n", 2)[2])
    assert "reads a late-arriving source (30 days); declare maturity" in problems(kdir)


def test_derived_metric_must_declare_maturity_when_a_leg_does(kdir):
    text = (kdir / "metrics" / "refund_rate.yaml").read_text()
    (kdir / "metrics" / "refund_rate.yaml").write_text(
        text.replace("    maturity:\n      settles_after_days: 30\n      note: The numerator keeps rising for 30 days after the period closes.\n", "", 1))
    assert "a leg declares maturity" in problems(kdir)


def test_derived_legs_must_resolve(kdir):
    edit(kdir / "metrics" / "refund_rate.yaml", "{metric: refunds, measure: amount}", "{metric: refundz, measure: amount}")
    edit(kdir / "metrics" / "refund_rate.yaml", "{metric: orders, measure: count, preset: paid}",
         "{metric: orders, measure: count, preset: unpaid}")
    found = problems(kdir)
    assert "'refundz' is not a structured metric" in found and "has no preset 'unpaid'" in found


def test_personal_dimension_must_be_marked(kdir):
    edit(kdir / "metrics" / "orders.yaml", "customer_email: {sql: o.customer_email, personal: true}",
         "customer_email: {sql: o.customer_email}")
    assert "reads personal column(s) ['customer_email']; mark it personal: true" in problems(kdir)


def test_definitions_may_not_read_another_units_source(kdir):
    edit(kdir / "metrics" / "orders.yaml", "    source: unit_b.orders", "    source: unit_a.orders")
    assert "source 'unit_a.orders' is not declared for unit 'unit_b'" in problems(kdir)


@pytest.mark.parametrize("snippet,expected", [
    ('"{{ countable_order(o) }} -- note"', "comments are not allowed"),
    ('"{{ countable_order(o) }}; select 1"', "statement separators are not allowed"),
    ("\"{{ countable_order(o) }} AND o.channel = 'open\"", "does not tokenise"),
])
def test_sql_snippets_are_lexically_safe(kdir, snippet, expected):
    edit(kdir / "metrics" / "orders.yaml", '"{{ countable_order(o) }}"', snippet)
    assert expected in problems(kdir)


def test_yaml_on_key_trap_is_explained(kdir):
    edit(kdir / "metrics" / "refunds.yaml", "condition: \"o.order_id = r.order_id\"}", "on: \"o.order_id = r.order_id\"}")
    assert "name the join condition 'condition', not 'on'" in problems(kdir)


def test_measures_declare_additivity(kdir):
    edit(kdir / "metrics" / "orders.yaml", 'count: {sql: "COUNT(*)", additive: true}', 'count: {sql: "COUNT(*)"}')
    assert "must be declared true or false (never inferred)" in problems(kdir)


def test_a_source_belongs_to_one_unit(kdir):
    edit(kdir / "units.yaml", "      unit_b.refunds:", "      unit_a.refunds:")
    assert "already declared under unit 'unit_a'" in problems(kdir)


def test_rules_need_dated_verified_entries(kdir):
    edit(kdir / "rules" / "shipping.yaml", "{value: 50, effective: 2025-01-01, verified: true}",
         "{value: 50, effective: 2025-01-01}")
    edit(kdir / "rules" / "shipping.yaml", "{value: 60, effective: 2026-03-01, verified: true,",
         "{value: 60, effective: 2025-01-01, verified: true,")
    found = problems(kdir)
    assert "verified: must be declared true or false" in found
    assert "duplicate effective date" in found


def test_metric_key_must_match_its_file(kdir):
    edit(kdir / "metrics" / "orders.yaml", "key: orders", "key: order_count")
    assert "equal the file name ('orders')" in problems(kdir)


def test_fragment_detection_ignores_correct_references():
    frag = F.Fragment("countable_order", "{a}.status <> 'test' AND NOT {a}.is_internal", "")
    assert F.find_inlined("{{ countable_order(o) }} AND o.x = 1", [frag]) == []
    assert F.find_inlined("O.STATUS <> 'test' and not O.IS_INTERNAL", [frag]) == ["countable_order"]


def test_version_stamp_is_honest(kdir, monkeypatch):
    monkeypatch.delenv("GM_KNOWLEDGE_VERSION", raising=False)
    before = knowledge_version(str(kdir))
    assert before.startswith("c:")                  # tmp copy is not a clean git tree
    edit(kdir / "metrics" / "orders.yaml", "version: 2", "version: 3")
    assert knowledge_version(str(kdir)) != before
    monkeypatch.setenv("GM_KNOWLEDGE_VERSION", "build-42")
    assert knowledge_version(str(kdir)) == "build-42"


def test_missing_directory_is_refused(tmp_path):
    with pytest.raises(KnowledgeError, match="not found"):
        load_knowledge(str(tmp_path / "nope"))
