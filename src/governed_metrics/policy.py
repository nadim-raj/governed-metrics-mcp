"""The access policy: deny by default, resolved from the verified identity (docs/access-model.md).

A policy governs five things per caller: units, metrics, dimensions, ad-hoc querying, and
the sources an ad-hoc statement may read. Two rules keep it tractable:

* a missing entry is a refusal, not a default: there is no `default_units`, so forgetting
  to add someone denies them rather than granting them something;
* admin rights are separate from data access: an admin can manage grants and reload
  knowledge, and reads data only if they also hold a grant.

The policy file is version-controlled. It may `include:` further files that hold user grants
only (scripts/reconcile_overlay.py maintains one), never admins. A policy that fails to load
denies everyone and says why, rather than serving a partial policy.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import overlay as OV
from .knowledge.model import Knowledge, frozen_map

log = logging.getLogger("governed_metrics.policy")
WILDCARD = "*"


@dataclass(frozen=True)
class Grant:
    units: frozenset[str]
    metrics: Mapping[str, frozenset[str]] | None = None     # unit -> keys; None = unrestricted
    dimensions: Mapping[str, frozenset[str]] = field(default_factory=frozen_map)
    adhoc: Mapping[str, frozenset[str]] | None = None       # unit -> sources; None = no ad-hoc
    unmasked: bool = False
    origin: str = "policy"                                  # policy | include file | overlay


@dataclass(frozen=True)
class AccessPolicy:
    grants: Mapping[str, Grant]
    admins: frozenset[str]
    ok: bool
    error: str | None = None
    warnings: tuple[str, ...] = ()
    revoked: frozenset[str] = frozenset()
    reconcile: tuple[str, ...] = ()       # overlay users now governed by version control

    def grant(self, user: str) -> Grant | None:
        return self.grants.get((user or "").strip().lower()) if self.ok else None

    def units_for(self, user: str) -> frozenset[str]:
        g = self.grant(user)
        return g.units if g else frozenset()

    def can_read_unit(self, user: str, unit: str) -> bool:
        return unit in self.units_for(user)

    def has_metric_allow_list(self, user: str, unit: str) -> bool:
        g = self.grant(user)
        return g is not None and g.metrics is not None and unit in g.units

    def metric_allowed(self, user: str, unit: str, metric: str) -> bool:
        g = self.grant(user)
        if g is None or unit not in g.units:
            return False
        if g.metrics is None:
            return True
        allowed = g.metrics.get(unit, g.metrics.get(WILDCARD, frozenset()))
        return WILDCARD in allowed or metric in allowed

    def allowed_dimensions(self, user: str, metric: str) -> frozenset[str] | None:
        """Dimensions the caller may break `metric` down or filter it by; None = unrestricted.
        An empty set means the total only."""
        g = self.grant(user)
        if g is None:
            return frozenset()
        return g.dimensions.get(metric)

    def adhoc_sources(self, user: str, unit: str) -> frozenset[str] | None:
        """Sources the caller may reference ad hoc in `unit`; None when ad-hoc is not granted."""
        g = self.grant(user)
        if g is None or g.adhoc is None or unit not in g.units:
            return None
        return g.adhoc.get(unit)

    def unmasked(self, user: str) -> bool:
        g = self.grant(user)
        return bool(g and g.unmasked)

    def is_admin(self, user: str) -> bool:
        return self.ok and (user or "").strip().lower() in self.admins


def _deny_all(error: str) -> AccessPolicy:
    log.error("access policy unavailable (%s); denying every caller", error)
    return AccessPolicy(grants=frozen_map(), admins=frozenset(), ok=False, error=error)


class _Parser:
    def __init__(self, kb: Knowledge):
        self.kb = kb
        self.warnings: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def names(self, raw: Any, where: str, known: Iterable[str], *, wildcard: bool = True) -> frozenset[str]:
        known = set(known)
        if not isinstance(raw, list):
            self.warn(f"{where}: must be a list")
            return frozenset()
        out = set()
        for item in raw:
            name = str(item).strip()
            if wildcard and name == WILDCARD:
                out.add(WILDCARD)
            elif name in known:
                out.add(name)
            else:
                # Dropped, never kept: a typo must narrow access, never widen it.
                self.warn(f"{where}: unknown name {name!r} ignored")
        return frozenset(out)

    def units(self, raw: Any, where: str) -> frozenset[str]:
        names = self.names(raw, where, self.kb.units)
        return frozenset(self.kb.units) if WILDCARD in names else names

    def per_unit(self, raw: Any, where: str, units: frozenset[str], known_for, *,
                 unit_wildcard: bool = True) -> dict[str, frozenset[str]]:
        if not isinstance(raw, dict):
            self.warn(f"{where}: must map unit -> list")
            return {}
        out = {}
        for unit, items in raw.items():
            unit = str(unit)
            if unit == WILDCARD and not unit_wildcard:
                self.warn(f"{where}: name each unit explicitly; '*' is not accepted here")
                continue
            if unit != WILDCARD and unit not in self.kb.units:
                self.warn(f"{where}.{unit}: unknown unit ignored")
                continue
            if unit != WILDCARD and unit not in units:
                self.warn(f"{where}.{unit}: unit is not in this user's units; ignored")
                continue
            out[unit] = self.names(items, f"{where}.{unit}", known_for(unit))
        return out

    def user(self, user: str, spec: Any, origin: str) -> Grant | None:
        where = f"users.{user}"
        if not isinstance(spec, dict):
            self.warn(f"{where}: must be a mapping with at least 'units'")
            return None
        units = self.units(spec.get("units", []), f"{where}.units")
        metrics = None
        if "metrics" in spec:
            metrics = frozen_map(self.per_unit(
                spec["metrics"], f"{where}.metrics", units,
                lambda u: self.kb.metrics if u == WILDCARD else
                [k for k, m in self.kb.metrics.items() if u in m.units]))
        dims = {}
        for metric, items in (spec.get("dimensions") or {}).items():
            m = self.kb.metrics.get(metric)
            if m is None:
                self.warn(f"{where}.dimensions.{metric}: unknown metric ignored")
                continue
            known = {d for defn in m.units.values() for d in getattr(defn, "dimensions", {})}
            dims[metric] = self.names(items, f"{where}.dimensions.{metric}", known, wildcard=False)
        adhoc = None
        if "adhoc" in spec:
            # Ad-hoc is the highest-risk surface, so it is granted unit by unit, never by '*'.
            adhoc = frozen_map(self.per_unit(spec["adhoc"], f"{where}.adhoc", units,
                                             lambda u: self.kb.units[u].sources, unit_wildcard=False))
        unmasked = spec.get("unmasked", False)
        if not isinstance(unmasked, bool):
            self.warn(f"{where}.unmasked: must be true or false; treated as false")
            unmasked = False
        return Grant(units=units, metrics=metrics, dimensions=frozen_map(dims), adhoc=adhoc,
                     unmasked=unmasked, origin=origin)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return doc


def load_policy(path: str, kb: Knowledge, *, overlay_path: str | None = None) -> AccessPolicy:
    """Build the effective policy. Never raises: any structural problem denies everyone."""
    try:
        doc = _read(path)
    except FileNotFoundError:
        return _deny_all(f"policy file not found: {path}")
    except (OSError, ValueError, yaml.YAMLError) as e:
        return _deny_all(f"policy file unreadable: {e.__class__.__name__}")

    parser = _Parser(kb)
    admins_raw = doc.get("admins") or []
    if not isinstance(admins_raw, list):
        return _deny_all("'admins' must be a list")
    admins = frozenset(str(a).strip().lower() for a in admins_raw if str(a).strip())

    sources: list[tuple[str, dict]] = [("policy", doc)]
    for inc in doc.get("include") or []:
        inc_path = os.path.join(os.path.dirname(path), str(inc))
        try:
            inc_doc = _read(inc_path) if os.path.exists(inc_path) else {}
        except (OSError, ValueError, yaml.YAMLError) as e:
            return _deny_all(f"included policy file unreadable: {e.__class__.__name__}")
        if set(inc_doc) - {"users"}:
            return _deny_all(f"included file {inc} may only contain 'users'")
        sources.append((os.path.basename(str(inc)), inc_doc))

    grants: dict[str, Grant] = {}
    for origin, d in sources:
        users = d.get("users") or {}
        if not isinstance(users, dict):
            return _deny_all(f"'users' in {origin} must be a mapping")
        for user, spec in users.items():
            key = str(user).strip().lower()
            if key in grants:
                return _deny_all(f"user {key!r} is granted in more than one place")
            g = parser.user(key, spec, origin)
            if g is not None:
                grants[key] = g

    revoked: set[str] = set()
    reconcile: list[str] = []
    if overlay_path:
        for user, event in OV.effective(overlay_path).items():
            if event["action"] == "revoke":
                if user in grants:
                    grants.pop(user)
                    revoked.add(user)
                else:
                    reconcile.append(user)          # already denied by version control
            elif user in grants or user in admins:
                # Version control governs this user. For an admin that is a rule, not a
                # coincidence: whoever can change access must not be able to grant it to
                # themselves, so an admin's data access only ever comes through review.
                reconcile.append(user)
            else:
                units = parser.units(event.get("units") or [], f"overlay.{user}.units")
                if units:
                    grants[user] = Grant(units=units, origin="overlay")

    for w in parser.warnings:
        log.warning("access policy: %s", w)
    return AccessPolicy(grants=frozen_map(grants), admins=admins, ok=True,
                        warnings=tuple(parser.warnings), revoked=frozenset(revoked),
                        reconcile=tuple(sorted(reconcile)))
