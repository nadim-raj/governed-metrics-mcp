"""Governed rules: layered, effective-dated, served only when verified (ADR 0009).

Resolution for (domain, key, unit, as_of):

  1. the most specific layer that has an entry in force on `as_of` wins: the unit's
     override if it has one by then, otherwise the group default;
  2. within that layer, the latest entry effective on or before `as_of`;
  3. that entry is served only if it is marked verified. A newer unverified draft is not
     skipped in favour of an older verified value: if the entry in force is a draft, the
     honest answer is that no verified value exists, not an older one dressed as current.

Asking about the past gives the past's answer, and the effective date travels with the value.
"""
from __future__ import annotations

import difflib
from collections.abc import Sequence
from datetime import date
from typing import Any

from .errors import InvalidRequest
from .knowledge.model import Knowledge, RuleEntry


def _in_force(entries: Sequence[RuleEntry], as_of: date) -> RuleEntry | None:
    candidates = [e for e in entries if e.effective <= as_of]
    return max(candidates, key=lambda e: e.effective) if candidates else None


def _suggest(word: str, options) -> str:
    close = difflib.get_close_matches(word, list(options), n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def list_rules(kb: Knowledge) -> dict[str, Any]:
    """The catalogue without values: a value is only ever served with its provenance."""
    return {
        "kind": "rules",
        "rules": {
            domain: {
                key: {"description": rule.description,
                      "unit_overrides": sorted(rule.overrides),
                      "has_verified_entries": any(e.verified for e in rule.group_default)
                      or any(e.verified for es in rule.overrides.values() for e in es)}
                for key, rule in sorted(rules.items())
            }
            for domain, rules in sorted(kb.rules.items())
        },
        "knowledge_version": kb.version,
    }


def resolve_rule(kb: Knowledge, domain: str, key: str, unit: str, as_of: date) -> dict[str, Any]:
    rules = kb.rules.get(domain)
    if rules is None:
        raise InvalidRequest(f"Unknown rule domain '{domain}'.{_suggest(domain, kb.rules)} "
                             "Call list_rules for the catalogue.", reason="unknown_rule")
    rule = rules.get(key)
    if rule is None:
        raise InvalidRequest(f"Unknown rule '{key}' in '{domain}'.{_suggest(key, rules)}",
                             reason="unknown_rule")
    unit_entry = _in_force(rule.overrides.get(unit, ()), as_of)
    entry, layer = (unit_entry, "unit_override") if unit_entry else \
        (_in_force(rule.group_default, as_of), "group_default")
    if entry is None:
        raise InvalidRequest(f"Rule '{domain}/{key}' had no value in force on {as_of.isoformat()}.",
                             reason="rule_not_in_force")
    if not entry.verified:
        raise InvalidRequest(
            f"Rule '{domain}/{key}' has an entry in force on {as_of.isoformat()} that is not verified, "
            "so no value is served. Say the rule is not published; do not state one from memory.",
            reason="rule_unverified")
    out = {"kind": "rule", "domain": domain, "key": key, "unit": unit, "as_of": as_of.isoformat(),
           "value": entry.value, "effective": entry.effective.isoformat(), "layer": layer,
           "description": rule.description, "provenance": {"knowledge_version": kb.version}}
    if entry.note:
        out["note"] = entry.note
    return out
