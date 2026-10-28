"""Load the knowledge directory into one validated, immutable `Knowledge` snapshot.

Layout (see examples/knowledge/):

    units.yaml           units and the sources each one may read
    fragments.yaml       shared named rules, referenced as {{ name(alias) }}
    glossary.yaml        term -> meaning
    metrics/<key>.yaml   one metric per file; `units:` maps unit -> definition
    rules/<domain>.yaml  governed, effective-dated rules

Parsing collects every problem it finds rather than stopping at the first, then the
cross-reference validator runs (validate.py). Any problem raises KnowledgeError, so a server
with invalid definitions refuses to start and a reload keeps the snapshot it already had.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess  # nosec B404
from datetime import date
from typing import Any

import yaml

from ..errors import KnowledgeError
from . import fragments as F
from .model import (
    DerivedDefinition,
    Dimension,
    Fragment,
    Freshness,
    Join,
    Knowledge,
    Leg,
    Maturity,
    Measure,
    Metric,
    Preset,
    Rule,
    RuleEntry,
    Source,
    StructuredDefinition,
    Unit,
    Variant,
    frozen_map,
)

NAME = re.compile(r"^[a-z][a-z0-9_]*$")
SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")


class _Problems(list):
    def add(self, where: str, message: str) -> None:
        self.append(f"{where}: {message}")


def _read_yaml(path: str, problems: _Problems) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        problems.add(path, f"cannot be read as YAML ({e.__class__.__name__})")
        return None


def _mapping(value: Any, where: str, problems: _Problems) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.add(where, "must be a mapping")
        return {}
    return value


def _text(value: Any, where: str, problems: _Problems, *, required: bool = True) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            problems.add(where, "is required")
        return ""
    if not isinstance(value, (str, int, float, bool)):
        problems.add(where, "must be a scalar")
        return ""
    return " ".join(str(value).split())


def _date(value: Any, where: str, problems: _Problems) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        problems.add(where, f"must be an ISO date (YYYY-MM-DD), got {value!r}")
        return date.min


def _nonneg_int(value: Any, where: str, problems: _Problems, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.add(where, f"must be a non-negative integer, got {value!r}")
        return default
    return value


# ---- units and sources --------------------------------------------------------------------

def _load_units(root: str, problems: _Problems) -> dict[str, Unit]:
    path = os.path.join(root, "units.yaml")
    doc = _mapping(_read_yaml(path, problems), path, problems)
    out: dict[str, Unit] = {}
    owner: dict[str, str] = {}
    for name, spec in _mapping(doc.get("units"), f"{path}: units", problems).items():
        where = f"{path}: units.{name}"
        if not isinstance(name, str) or not NAME.match(name):
            problems.add(where, "unit names must match [a-z][a-z0-9_]*")
            continue
        spec = _mapping(spec, where, problems)
        sources = {}
        for sname, sspec in _mapping(spec.get("sources"), f"{where}.sources", problems).items():
            sw = f"{where}.sources.{sname}"
            if not isinstance(sname, str) or not SOURCE_NAME.match(sname):
                problems.add(sw, "source names must be schema.table in lower case")
                continue
            if sname in owner:
                problems.add(sw, f"already declared under unit '{owner[sname]}'; a source "
                                 "belongs to exactly one unit")
                continue
            owner[sname] = name
            sspec = _mapping(sspec, sw, problems)
            fresh = None
            if sspec.get("freshness") is not None:
                fs = _mapping(sspec["freshness"], f"{sw}.freshness", problems)
                column = _text(fs.get("column"), f"{sw}.freshness.column", problems)
                if column and not ALIAS.match(column):
                    problems.add(f"{sw}.freshness.column", "must be a plain column name")
                fresh = Freshness(column=column,
                                  max_age_days=_nonneg_int(fs.get("max_age_days"),
                                                           f"{sw}.freshness.max_age_days", problems, 2),
                                  lag_days=_nonneg_int(fs.get("lag_days"),
                                                       f"{sw}.freshness.lag_days", problems))
                if fresh.max_age_days < fresh.lag_days:
                    problems.add(f"{sw}.freshness", "max_age_days must be >= lag_days")
            personal = sspec.get("personal_columns") or []
            if not isinstance(personal, list) or not all(isinstance(c, str) and ALIAS.match(c)
                                                         for c in personal):
                problems.add(f"{sw}.personal_columns", "must be a list of column names")
                personal = []
            sources[sname] = Source(
                name=sname, unit=name,
                description=_text(sspec.get("description"), f"{sw}.description", problems),
                freshness=fresh, personal_columns=frozenset(personal),
                late_arriving_days=_nonneg_int(sspec.get("late_arriving_days"),
                                               f"{sw}.late_arriving_days", problems))
        out[name] = Unit(name=name,
                         description=_text(spec.get("description"), f"{where}.description", problems),
                         sources=frozen_map(sources))
    if not out:
        problems.add(path, "declares no units")
    return out


# ---- fragments and glossary ---------------------------------------------------------------

def _load_fragments(root: str, problems: _Problems) -> dict[str, Fragment]:
    path = os.path.join(root, "fragments.yaml")
    if not os.path.exists(path):
        return {}
    doc = _mapping(_read_yaml(path, problems), path, problems)
    out = {}
    for name, spec in _mapping(doc.get("fragments"), f"{path}: fragments", problems).items():
        where = f"{path}: fragments.{name}"
        if not isinstance(name, str) or not NAME.match(name):
            problems.add(where, "fragment names must match [a-z][a-z0-9_]*")
            continue
        spec = _mapping(spec, where, problems)
        sql = _text(spec.get("sql"), f"{where}.sql", problems)
        if sql and "{a}" not in sql:
            problems.add(f"{where}.sql", "must use {a} for the alias it is applied to")
        if F.REFERENCE.search(sql):
            problems.add(f"{where}.sql", "fragments may not reference other fragments")
        out[name] = Fragment(name=name, sql=sql,
                             description=_text(spec.get("description"), f"{where}.description", problems))
    return out


def _load_glossary(root: str, problems: _Problems) -> dict[str, str]:
    path = os.path.join(root, "glossary.yaml")
    if not os.path.exists(path):
        return {}
    doc = _mapping(_read_yaml(path, problems), path, problems)
    terms = _mapping(doc.get("terms"), f"{path}: terms", problems)
    return {str(k): _text(v, f"{path}: terms.{k}", problems) for k, v in terms.items()}


# ---- metrics ------------------------------------------------------------------------------

class _MetricParser:
    """Parses one metric file. `snippets` keeps every raw SQL fragment (before fragment
    expansion) with its location, for the validator's lexical and inline-copy checks."""

    def __init__(self, path: str, frags: dict[str, Fragment], problems: _Problems):
        self.path = path
        self.frags = frags
        self.problems = problems
        self.snippets: list[tuple[str, str, str]] = []   # (where, raw sql, unit)

    def sql(self, value: Any, where: str, unit: str, *, required: bool = True) -> str:
        raw = _text(value, where, self.problems, required=required)
        if not raw:
            return ""
        self.snippets.append((where, raw, unit))
        unknown = [n for n, _ in F.references(raw) if n not in self.frags]
        if unknown:
            self.problems.add(where, f"references unknown fragment(s) {sorted(set(unknown))}")
            return raw
        return F.expand(raw, self.frags)

    def named(self, items: Any, where: str, unit: str, build) -> dict:
        out = {}
        for name, spec in _mapping(items, where, self.problems).items():
            w = f"{where}.{name}"
            if not isinstance(name, str) or not NAME.match(name):
                self.problems.add(w, "names must match [a-z][a-z0-9_]*")
                continue
            out[name] = build(name, _mapping(spec, w, self.problems), w, unit)
        return out

    def maturity(self, value: Any, where: str) -> Maturity | None:
        if value is None:
            return None
        spec = _mapping(value, where, self.problems)
        days = _nonneg_int(spec.get("settles_after_days"), f"{where}.settles_after_days",
                           self.problems)
        if days == 0:
            self.problems.add(f"{where}.settles_after_days", "must be at least 1")
        return Maturity(settles_after_days=days,
                        note=_text(spec.get("note"), f"{where}.note", self.problems))

    def structured(self, key: str, unit: str, spec: dict, where: str) -> StructuredDefinition:
        p = self.problems
        source = _text(spec.get("source"), f"{where}.source", p)
        alias = _text(spec.get("alias"), f"{where}.alias", p)
        if alias and not ALIAS.match(alias):
            p.add(f"{where}.alias", "must be a lower-case identifier")
        joins = []
        raw_joins = spec.get("joins") or []
        if not isinstance(raw_joins, list):
            p.add(f"{where}.joins", "must be a list")
            raw_joins = []
        for i, j in enumerate(raw_joins):
            jw = f"{where}.joins[{i}]"
            j = _mapping(j, jw, p)
            kind = _text(j.get("kind", "inner"), f"{jw}.kind", p)
            if kind not in ("inner", "left"):
                p.add(f"{jw}.kind", "must be 'inner' or 'left'")
            jalias = _text(j.get("alias"), f"{jw}.alias", p)
            if jalias and not ALIAS.match(jalias):
                p.add(f"{jw}.alias", "must be a lower-case identifier")
            if True in j or "on" in j:
                # YAML 1.1 reads a bare `on:` key as the boolean true.
                p.add(jw, "name the join condition 'condition', not 'on'")
            joins.append(Join(source=_text(j.get("source"), f"{jw}.source", p), alias=jalias,
                              on=self.sql(j.get("condition"), f"{jw}.condition", unit), kind=kind))
        measures = self.named(spec.get("measures"), f"{where}.measures", unit, self._measure)
        if not measures:
            p.add(f"{where}.measures", "at least one measure is required")
        default = _text(spec.get("default_measure") or (sorted(measures)[0] if len(measures) == 1 else None),
                        f"{where}.default_measure", p)
        aliases = {}
        for a, target in _mapping(spec.get("measure_aliases"), f"{where}.measure_aliases", p).items():
            aliases[str(a)] = str(target)
        snapshot = spec.get("snapshot", False)
        if not isinstance(snapshot, bool):
            p.add(f"{where}.snapshot", "must be true or false")
            snapshot = False
        return StructuredDefinition(
            metric=key, unit=unit, source=source, alias=alias,
            measures=frozen_map(measures), default_measure=default,
            period_column=self.sql(spec.get("period_column"), f"{where}.period_column", unit,
                                   required=False) or None,
            snapshot=snapshot,
            base_filter=self.sql(spec.get("base_filter"), f"{where}.base_filter", unit,
                                 required=False) or None,
            joins=tuple(joins),
            dimensions=frozen_map(self.named(spec.get("dimensions"), f"{where}.dimensions", unit,
                                             self._dimension)),
            presets=frozen_map(self.named(spec.get("presets"), f"{where}.presets", unit, self._preset)),
            measure_aliases=frozen_map(aliases),
            maturity=self.maturity(spec.get("maturity"), f"{where}.maturity"),
        )

    def _measure(self, name: str, spec: dict, where: str, unit: str) -> Measure:
        additive = spec.get("additive")
        if not isinstance(additive, bool):
            self.problems.add(f"{where}.additive", "must be declared true or false (never inferred)")
            additive = False
        return Measure(name=name, sql=self.sql(spec.get("sql"), f"{where}.sql", unit), additive=additive)

    def _dimension(self, name: str, spec: dict, where: str, unit: str) -> Dimension:
        personal = spec.get("personal", False)
        if not isinstance(personal, bool):
            self.problems.add(f"{where}.personal", "must be true or false")
            personal = True
        return Dimension(name=name, sql=self.sql(spec.get("sql"), f"{where}.sql", unit), personal=personal)

    def _preset(self, name: str, spec: dict, where: str, unit: str) -> Preset:
        return Preset(name=name, sql=self.sql(spec.get("sql"), f"{where}.sql", unit),
                      description=_text(spec.get("description"), f"{where}.description", self.problems))

    def leg(self, value: Any, where: str) -> Leg:
        spec = _mapping(value, where, self.problems)
        return Leg(metric=_text(spec.get("metric"), f"{where}.metric", self.problems),
                   measure=_text(spec.get("measure"), f"{where}.measure", self.problems,
                                 required=False) or None,
                   preset=_text(spec.get("preset"), f"{where}.preset", self.problems,
                                required=False) or None)

    def derived(self, key: str, unit: str, spec: dict, where: str) -> DerivedDefinition:
        op = _text(spec.get("op"), f"{where}.op", self.problems)
        if op and op not in ("ratio", "difference"):
            self.problems.add(f"{where}.op", "must be 'ratio' or 'difference'")
        variants = {}
        for name, v in _mapping(spec.get("variants"), f"{where}.variants", self.problems).items():
            vw = f"{where}.variants.{name}"
            if not isinstance(name, str) or not NAME.match(name):
                self.problems.add(vw, "names must match [a-z][a-z0-9_]*")
                continue
            v = _mapping(v, vw, self.problems)
            variants[name] = Variant(name=name, numerator=self.leg(v.get("numerator"), f"{vw}.numerator"),
                                     denominator=self.leg(v.get("denominator"), f"{vw}.denominator"))
        if not variants:
            self.problems.add(f"{where}.variants", "at least one variant is required")
        default = _text(spec.get("default_variant") or (next(iter(variants)) if len(variants) == 1 else None),
                        f"{where}.default_variant", self.problems)
        return DerivedDefinition(metric=key, unit=unit, op=op, variants=frozen_map(variants),
                                 default_variant=default,
                                 maturity=self.maturity(spec.get("maturity"), f"{where}.maturity"))

    def parse(self) -> Metric | None:
        p = self.problems
        doc = _mapping(_read_yaml(self.path, p), self.path, p)
        if not doc:
            return None
        key = _text(doc.get("key"), f"{self.path}: key", p)
        stem = os.path.splitext(os.path.basename(self.path))[0]
        if key and (not NAME.match(key) or key != stem):
            p.add(f"{self.path}: key", f"must match [a-z][a-z0-9_]* and equal the file name ('{stem}')")
        kind = _text(doc.get("kind", "structured"), f"{self.path}: kind", p)
        if kind not in ("structured", "derived"):
            p.add(f"{self.path}: kind", "must be 'structured' or 'derived'")
        version = doc.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            p.add(f"{self.path}: version", "must be a positive integer")
            version = 0
        units = {}
        for unit, spec in _mapping(doc.get("units"), f"{self.path}: units", p).items():
            where = f"{self.path}: units.{unit}"
            spec = _mapping(spec, where, p)
            units[unit] = (self.derived(key, unit, spec, where) if kind == "derived"
                           else self.structured(key, unit, spec, where))
        if not units:
            p.add(f"{self.path}: units", "the metric is not defined for any unit")
        return Metric(key=key, description=_text(doc.get("description"), f"{self.path}: description", p),
                      owner=_text(doc.get("owner"), f"{self.path}: owner", p),
                      version=version, effective=_date(doc.get("effective"), f"{self.path}: effective", p),
                      kind=kind, units=frozen_map(units), file=os.path.relpath(self.path))


# ---- rules --------------------------------------------------------------------------------

def _entries(value: Any, where: str, problems: _Problems) -> tuple[RuleEntry, ...]:
    if not isinstance(value, list) or not value:
        problems.add(where, "must be a non-empty list of dated entries")
        return ()
    out, seen = [], set()
    for i, e in enumerate(value):
        ew = f"{where}[{i}]"
        e = _mapping(e, ew, problems)
        if "value" not in e:
            problems.add(ew, "value is required")
        if not isinstance(e.get("verified"), bool):
            problems.add(f"{ew}.verified", "must be declared true or false")
        eff = _date(e.get("effective"), f"{ew}.effective", problems)
        if eff in seen:
            problems.add(f"{ew}.effective", "duplicate effective date in this layer")
        seen.add(eff)
        out.append(RuleEntry(value=e.get("value"), effective=eff, verified=e.get("verified") is True,
                             note=_text(e.get("note"), f"{ew}.note", problems, required=False) or None))
    return tuple(sorted(out, key=lambda x: x.effective))


def _load_rules(root: str, units: dict[str, Unit], problems: _Problems) -> dict[str, dict[str, Rule]]:
    out: dict[str, dict[str, Rule]] = {}
    for path in sorted(glob.glob(os.path.join(root, "rules", "*.yaml"))):
        doc = _mapping(_read_yaml(path, problems), path, problems)
        domain = _text(doc.get("domain"), f"{path}: domain", problems)
        stem = os.path.splitext(os.path.basename(path))[0]
        if domain and domain != stem:
            problems.add(f"{path}: domain", f"must equal the file name ('{stem}')")
        rules = {}
        for key, spec in _mapping(doc.get("rules"), f"{path}: rules", problems).items():
            where = f"{path}: rules.{key}"
            spec = _mapping(spec, where, problems)
            overrides = {}
            for unit, entries in _mapping(spec.get("overrides"), f"{where}.overrides", problems).items():
                if unit not in units:
                    problems.add(f"{where}.overrides.{unit}", "unknown unit")
                    continue
                overrides[unit] = _entries(entries, f"{where}.overrides.{unit}", problems)
            rules[str(key)] = Rule(domain=domain, key=str(key),
                                   description=_text(spec.get("description"), f"{where}.description", problems),
                                   group_default=_entries(spec.get("group_default"), f"{where}.group_default",
                                                          problems),
                                   overrides=frozen_map(overrides))
        out[domain] = frozen_map(rules)
    return out


# ---- version stamp ------------------------------------------------------------------------

def _content_hash(root: str) -> str:
    """Hash of every knowledge file by relative path and content: changes if and only if a
    definition changes, independent of git."""
    h = hashlib.sha256()
    for path in sorted(glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True)):
        h.update(os.path.relpath(path, root).encode())
        h.update(b"\0")
        with open(path, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _git_commit(root: str) -> str | None:
    """Short HEAD commit, but only when the knowledge tree is clean against it; a tree that
    has moved past its commit (files copied over a stale checkout) must not claim it. git runs
    with a fixed argv and no shell."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        status = subprocess.run([git, "-C", root, "status", "--porcelain", "--", "."],  # noqa: S603  # nosec B603
                                capture_output=True, text=True, timeout=5, check=True).stdout
        if status.strip():
            return None
        return subprocess.run([git, "-C", root, "rev-parse", "--short=12", "HEAD"],  # noqa: S603  # nosec B603
                              capture_output=True, text=True, timeout=5, check=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def knowledge_version(root: str) -> str:
    """The provenance stamp. An explicit build stamp wins (an image has no .git); then the
    clean git commit; else the content hash, which is always honest."""
    stamp = os.environ.get("GM_KNOWLEDGE_VERSION", "").strip()
    if stamp:
        return stamp
    commit = _git_commit(root)
    return f"g:{commit}" if commit else f"c:{_content_hash(root)}"


# ---- entry point --------------------------------------------------------------------------

def load_knowledge(root: str) -> Knowledge:
    from .validate import validate  # local import: validate depends on the model only

    root = os.path.abspath(root)
    problems = _Problems()
    if not os.path.isdir(root):
        raise KnowledgeError([f"{root}: knowledge directory not found"])
    units = _load_units(root, problems)
    frags = _load_fragments(root, problems)
    metrics, snippets = {}, []
    for path in sorted(glob.glob(os.path.join(root, "metrics", "*.yaml"))):
        parser = _MetricParser(path, frags, problems)
        metric = parser.parse()
        snippets.extend(parser.snippets)
        if metric is None or not metric.key:
            continue
        if metric.key in metrics:
            problems.add(path, f"metric key '{metric.key}' is defined twice")
            continue
        metrics[metric.key] = metric
    kb = Knowledge(version=knowledge_version(root), units=frozen_map(units),
                   metrics=frozen_map(metrics), rules=frozen_map(_load_rules(root, units, problems)),
                   fragments=frozen_map(frags), glossary=frozen_map(_load_glossary(root, problems)),
                   path=root)
    problems.extend(validate(kb, snippets))
    if problems:
        raise KnowledgeError(list(problems))
    return kb
