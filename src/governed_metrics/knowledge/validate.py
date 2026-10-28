"""Cross-reference checks over a parsed knowledge snapshot (docs/quality.md, "Validators").

These catch the definitions that parse cleanly and are still wrong in shape:

* every source a definition reads is declared for its own unit, so a definition cannot be
  written against another unit's data;
* every metric declares its period anchoring (or says it is a snapshot);
* every derived leg resolves to a structured metric, measure and preset in the same unit;
* no shared fragment is orphaned, and no definition inlines a fragment's rule (ADR 0008);
* a definition over a late-arriving source declares maturity (lessons-learned.md);
* a dimension that reads a personal column is marked personal, so it is masked;
* SQL snippets tokenise and carry no comments or statement separators, which would change
  the compiled statement around them.

Validators check shape, not meaning. Review is still the control for meaning.
"""
from __future__ import annotations

from ..adhoc.lexer import QIDENT, WORD, LexError, tokenize
from . import fragments as F
from .model import DerivedDefinition, Knowledge, StructuredDefinition


def _snippet_problems(kb: Knowledge, snippets: list[tuple[str, str, str]]) -> tuple[list[str], set[str]]:
    problems, used = [], set()
    for where, raw, _unit in snippets:
        refs = F.references(raw)
        used |= {name for name, _ in refs}
        inlined = F.find_inlined(raw, kb.fragments.values())
        if inlined:
            problems.append(f"{where}: inlines the rule of fragment(s) {sorted(inlined)}; "
                            f"reference it as {{{{ {inlined[0]}(alias) }}}} instead")
        if "--" in raw or "/*" in raw:
            problems.append(f"{where}: comments are not allowed in definition SQL")
            continue
        if any(name not in kb.fragments for name, _ in refs):
            continue                       # already reported by the parser
        try:
            tokens = tokenize(F.expand(raw, kb.fragments))
        except LexError as e:
            problems.append(f"{where}: does not tokenise ({e})")
            continue
        if any(tk.is_punct(";") for tk in tokens):
            problems.append(f"{where}: statement separators are not allowed")
    return problems, used


def _words(sql: str) -> set[str]:
    try:
        return {tk.value for tk in tokenize(sql) if tk.kind in (WORD, QIDENT)}
    except LexError:
        return set()


def _structured_problems(kb: Knowledge, where: str, d: StructuredDefinition) -> list[str]:
    out = []
    unit = kb.units[d.unit]
    for src in d.sources:
        if src not in unit.sources:
            out.append(f"{where}: source '{src}' is not declared for unit '{d.unit}'")
    aliases = [d.alias, *(j.alias for j in d.joins)]
    if len(set(aliases)) != len(aliases):
        out.append(f"{where}: table aliases must be unique")
    if d.snapshot and d.period_column:
        out.append(f"{where}: a snapshot metric must not declare period_column")
    if not d.snapshot and not d.period_column:
        out.append(f"{where}: declare period_column, or snapshot: true for a point-in-time value")
    if d.default_measure and d.default_measure not in d.measures:
        out.append(f"{where}: default_measure '{d.default_measure}' is not a declared measure")
    for alias, target in d.measure_aliases.items():
        if alias in d.measures:
            out.append(f"{where}: measure alias '{alias}' shadows a real measure")
        if target not in d.measures:
            out.append(f"{where}: measure alias '{alias}' points at unknown measure '{target}'")
    declared = [unit.sources[s] for s in d.sources if s in unit.sources]
    personal = set().union(*(s.personal_columns for s in declared)) if declared else set()
    for dim in d.dimensions.values():
        hit = _words(dim.sql) & personal
        if hit and not dim.personal:
            out.append(f"{where}: dimension '{dim.name}' reads personal column(s) {sorted(hit)}; "
                       "mark it personal: true")
    late = max((s.late_arriving_days for s in declared), default=0)
    if late:
        if d.maturity is None:
            out.append(f"{where}: reads a late-arriving source ({late} days); declare maturity")
        elif d.maturity.settles_after_days < late:
            out.append(f"{where}: maturity.settles_after_days is shorter than the source's "
                       f"late_arriving_days ({late})")
    return out


def _derived_problems(kb: Knowledge, key: str, where: str, d: DerivedDefinition) -> list[str]:
    out = []
    if d.default_variant and d.default_variant not in d.variants:
        out.append(f"{where}: default_variant '{d.default_variant}' is not a declared variant")
    leg_matures = False
    for variant in d.variants.values():
        for side, leg in (("numerator", variant.numerator), ("denominator", variant.denominator)):
            lw = f"{where}.variants.{variant.name}.{side}"
            target = kb.metrics.get(leg.metric)
            if leg.metric == key:
                out.append(f"{lw}: a derived metric cannot reference itself")
                continue
            if target is None or target.kind != "structured":
                out.append(f"{lw}: '{leg.metric}' is not a structured metric")
                continue
            ld = target.units.get(d.unit)
            if not isinstance(ld, StructuredDefinition):
                out.append(f"{lw}: '{leg.metric}' is not defined for unit '{d.unit}'")
                continue
            measure = leg.measure or ld.default_measure
            if measure not in ld.measures and measure not in ld.measure_aliases:
                out.append(f"{lw}: '{leg.metric}' has no measure '{measure}'")
            if leg.preset and leg.preset not in ld.presets:
                out.append(f"{lw}: '{leg.metric}' has no preset '{leg.preset}'")
            leg_matures = leg_matures or ld.maturity is not None
    if leg_matures and d.maturity is None:
        out.append(f"{where}: a leg declares maturity, so the derived metric must declare it too")
    return out


def validate(kb: Knowledge, snippets: list[tuple[str, str, str]]) -> list[str]:
    problems, used = _snippet_problems(kb, snippets)
    for name in sorted(set(kb.fragments) - used):
        problems.append(f"fragments.yaml: fragment '{name}' is not referenced by any definition")
    for metric in kb.metrics.values():
        for unit_name, d in metric.units.items():
            where = f"{metric.file}: units.{unit_name}"
            if unit_name not in kb.units:
                problems.append(f"{where}: unknown unit '{unit_name}'")
                continue
            if isinstance(d, StructuredDefinition):
                problems += _structured_problems(kb, where, d)
            else:
                problems += _derived_problems(kb, metric.key, where, d)
    return problems
