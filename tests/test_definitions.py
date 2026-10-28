"""Definition tests: pin properties the live knowledge must keep (docs/quality.md).

These run against the real knowledge directory. They fail when somebody edits a definition
in a way that is syntactically fine and semantically broken.
"""
from __future__ import annotations

from governed_metrics.knowledge.model import DerivedDefinition, StructuredDefinition


def structured(kb):
    for metric in kb.metrics.values():
        for d in metric.units.values():
            if isinstance(d, StructuredDefinition):
                yield metric, d


def test_every_structured_definition_is_anchored(kb):
    for metric, d in structured(kb):
        assert d.snapshot or d.period_column, f"{metric.key}/{d.unit} has no period anchoring"


def test_every_derived_leg_is_a_structured_metric_in_the_same_unit(kb):
    for metric in kb.metrics.values():
        for unit, d in metric.units.items():
            if not isinstance(d, DerivedDefinition):
                continue
            for v in d.variants.values():
                for leg in (v.numerator, v.denominator):
                    assert isinstance(kb.metrics[leg.metric].units[unit], StructuredDefinition)


def test_definitions_read_only_their_own_units_sources(kb):
    for metric, d in structured(kb):
        assert set(d.sources) <= set(kb.units[d.unit].sources), f"{metric.key}/{d.unit}"


def test_dimensions_on_personal_columns_are_masked(kb):
    for metric, d in structured(kb):
        personal = set().union(*(kb.units[d.unit].sources[s].personal_columns for s in d.sources))
        for dim in d.dimensions.values():
            if any(col in dim.sql for col in personal):
                assert dim.personal, f"{metric.key}/{d.unit}.{dim.name}"


def test_values_that_keep_moving_declare_maturity(kb):
    for metric, d in structured(kb):
        if any(kb.units[d.unit].sources[s].late_arriving_days for s in d.sources):
            assert d.maturity is not None, f"{metric.key}/{d.unit}"


def test_every_fragment_is_used(kb):
    from pathlib import Path

    text = "".join(p.read_text() for p in Path(kb.path, "metrics").glob("*.yaml"))
    for name in kb.fragments:
        assert f"{{{{ {name}(" in text


def test_cross_unit_metrics_share_measure_names_or_alias_them(kb):
    """A composite asks every unit for the same measure name; each unit must answer it."""
    from governed_metrics.engine.compiler import resolve_measure

    for metric in kb.metrics.values():
        defs = [d for d in metric.units.values() if isinstance(d, StructuredDefinition)]
        if len(defs) < 2:
            continue
        for name in defs[0].measures:
            for d in defs[1:]:
                assert resolve_measure(d, name), f"{metric.key}/{d.unit} lacks '{name}'"
