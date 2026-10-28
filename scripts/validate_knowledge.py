#!/usr/bin/env python3
"""Validate the knowledge layer and the access policy before anything ships.

    python scripts/validate_knowledge.py                 shape: schema, references, fragments
    python scripts/validate_knowledge.py --live          also: every filter still matches rows

The live check is the guard against label drift (lessons-learned.md): a filter written
against an upstream label that was renamed stops matching anything, and a smaller result is
still a valid result, so nothing else fails. Here a filter that matches nothing does fail.
It needs GM_DSN_<UNIT> for each unit, pointing at that unit's read-only role.

A policy that loads with warnings fails this script: at runtime a typo narrows access
silently (safe), but it should never reach a deploy unnoticed.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from governed_metrics.config import load_settings  # noqa: E402
from governed_metrics.engine.compiler import compile_match_count  # noqa: E402
from governed_metrics.errors import KnowledgeError  # noqa: E402
from governed_metrics.knowledge.loader import load_knowledge  # noqa: E402
from governed_metrics.knowledge.model import StructuredDefinition  # noqa: E402
from governed_metrics.policy import load_policy  # noqa: E402


def live_problems(kb) -> list[str]:
    from governed_metrics.warehouse import PostgresWarehouse

    settings = load_settings()
    wh = PostgresWarehouse(settings.dsn_for, statement_timeout_ms=settings.statement_timeout_ms)
    out = []
    try:
        for metric in kb.metrics.values():
            for unit, d in metric.units.items():
                if not isinstance(d, StructuredDefinition):
                    continue
                for preset in (None, *d.presets):
                    n = wh.scalar(unit, compile_match_count(d, preset))
                    label = f"{metric.key}/{unit}" + (f" preset {preset}" if preset else "")
                    if not n:
                        out.append(f"{label}: filters match no rows (renamed label? stopped feed?)")
    finally:
        wh.close()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    examples = os.path.join(ROOT, "examples")
    ap.add_argument("--knowledge", default=os.environ.get("GM_KNOWLEDGE_PATH", os.path.join(examples, "knowledge")))
    ap.add_argument("--policy", default=os.environ.get("GM_POLICY_PATH", os.path.join(examples, "access.yaml")))
    ap.add_argument("--live", action="store_true", help="also check every filter against the warehouse")
    args = ap.parse_args(argv)

    try:
        kb = load_knowledge(args.knowledge)
    except KnowledgeError as e:
        for p in e.problems:
            print(f"knowledge: {p}", file=sys.stderr)
        return 1
    policy = load_policy(args.policy, kb)
    problems = [f"policy: {policy.error}"] if not policy.ok else [f"policy: {w}" for w in policy.warnings]
    if args.live:
        problems += [f"live: {p}" for p in live_problems(kb)]
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        return 1
    print(f"ok: {len(kb.metrics)} metrics, {len(kb.units)} units, {len(policy.grants)} grants, "
          f"knowledge {kb.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
