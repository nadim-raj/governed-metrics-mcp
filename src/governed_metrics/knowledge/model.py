"""The definition model as immutable values (docs/knowledge-model.md).

Everything the loader produces is frozen: a request reads one `Knowledge` snapshot from
start to finish, and a reload swaps in a whole new snapshot rather than editing this one,
so no request ever sees half of an old definition and half of a new one.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any


def frozen_map(d: Mapping | None = None) -> Mapping:
    """A read-only view of a copy of `d`. Frozen dataclasses are only shallowly immutable;
    wrapping their mapping fields closes the gap."""
    return MappingProxyType(dict(d or {}))


@dataclass(frozen=True)
class Freshness:
    """How current a source is expected to be. `lag_days` is the source's normal publication
    lag (a daily feed published the next morning has lag 1); the source is stale once its
    newest record is older than `max_age_days`."""

    column: str
    max_age_days: int
    lag_days: int = 0


@dataclass(frozen=True)
class Source:
    name: str                        # schema.table, as the warehouse names it
    unit: str
    description: str
    freshness: Freshness | None = None
    personal_columns: frozenset[str] = frozenset()
    late_arriving_days: int = 0      # records keep arriving this long after their date

    @property
    def schema(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def table(self) -> str:
        return self.name.split(".", 1)[1]


@dataclass(frozen=True)
class Unit:
    """A business unit: the unit of access control, of database role and of rule override."""

    name: str
    description: str
    sources: Mapping[str, Source] = field(default_factory=frozen_map)


@dataclass(frozen=True)
class Measure:
    name: str
    sql: str
    additive: bool                   # may be summed across units; declared, never inferred


@dataclass(frozen=True)
class Dimension:
    name: str
    sql: str
    personal: bool = False           # values are personal data and are masked on the way out


@dataclass(frozen=True)
class Preset:
    name: str
    sql: str
    description: str


@dataclass(frozen=True)
class Join:
    source: str
    alias: str
    on: str
    kind: str = "inner"              # inner | left


@dataclass(frozen=True)
class Maturity:
    """Values in a window keep moving for `settles_after_days` after it closes."""

    settles_after_days: int
    note: str


@dataclass(frozen=True)
class StructuredDefinition:
    """One metric in one unit, compiled to `SELECT <measure> FROM <source> WHERE ...`."""

    metric: str
    unit: str
    source: str
    alias: str
    measures: Mapping[str, Measure]
    default_measure: str
    period_column: str | None = None
    snapshot: bool = False
    base_filter: str | None = None
    joins: tuple[Join, ...] = ()
    dimensions: Mapping[str, Dimension] = field(default_factory=frozen_map)
    presets: Mapping[str, Preset] = field(default_factory=frozen_map)
    measure_aliases: Mapping[str, str] = field(default_factory=frozen_map)
    maturity: Maturity | None = None

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.source, *(j.source for j in self.joins))


@dataclass(frozen=True)
class Leg:
    metric: str
    measure: str | None = None
    preset: str | None = None        # pins the leg's preset; otherwise it inherits the caller's


@dataclass(frozen=True)
class Variant:
    name: str
    numerator: Leg
    denominator: Leg


@dataclass(frozen=True)
class DerivedDefinition:
    """A ratio or difference of two structured metrics. Each leg is a metric in its own
    right, so a derived value can never disagree with its own components."""

    metric: str
    unit: str
    op: str                          # ratio | difference
    variants: Mapping[str, Variant]
    default_variant: str
    maturity: Maturity | None = None


Definition = StructuredDefinition | DerivedDefinition


@dataclass(frozen=True)
class Metric:
    key: str
    description: str
    owner: str
    version: int
    effective: date
    kind: str                        # structured | derived
    units: Mapping[str, Definition]
    file: str


@dataclass(frozen=True)
class RuleEntry:
    value: Any
    effective: date
    verified: bool
    note: str | None = None


@dataclass(frozen=True)
class Rule:
    domain: str
    key: str
    description: str
    group_default: tuple[RuleEntry, ...]
    overrides: Mapping[str, tuple[RuleEntry, ...]] = field(default_factory=frozen_map)


@dataclass(frozen=True)
class Fragment:
    name: str
    sql: str                          # uses {a} for the alias it is applied to
    description: str


@dataclass(frozen=True)
class Knowledge:
    """One complete, validated snapshot of the knowledge layer."""

    version: str
    units: Mapping[str, Unit]
    metrics: Mapping[str, Metric]
    rules: Mapping[str, Mapping[str, Rule]]
    fragments: Mapping[str, Fragment]
    glossary: Mapping[str, str]
    path: str = ""

    def source(self, name: str) -> Source | None:
        for unit in self.units.values():
            if name in unit.sources:
                return unit.sources[name]
        return None

    def personal_columns(self) -> frozenset[str]:
        """Every column name declared personal anywhere. The masking chokepoint uses the
        union, so a renamed copy of the same column in another source is still covered."""
        out: set[str] = set()
        for unit in self.units.values():
            for src in unit.sources.values():
                out |= src.personal_columns
        return frozenset(out)
