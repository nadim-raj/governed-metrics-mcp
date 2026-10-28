#!/usr/bin/env python3
"""Rebuild the generated sections of docs/reference.md from the source (ADR 0012).

Marked blocks are owned by this script and never edited by hand:

    <!-- gen:NAME -->  ...generated...  <!-- /gen:NAME -->

    tools     every MCP tool in server.py, its annotation and first docstring line
    errors    the error vocabulary in errors.py
    modules   every module under src/governed_metrics with its first docstring line
    metrics   the example knowledge's metric catalogue

    python scripts/gen_docs.py            rewrite the blocks in place
    python scripts/gen_docs.py --check    exit 1 if any block is stale or missing

Imports nothing that needs a database: tools are read from server.py with `ast`.
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src", "governed_metrics")
DOC = os.path.join(ROOT, "docs", "reference.md")
sys.path.insert(0, os.path.join(ROOT, "src"))

from governed_metrics.errors import ERROR_KINDS  # noqa: E402
from governed_metrics.knowledge.loader import load_knowledge  # noqa: E402
from governed_metrics.knowledge.model import DerivedDefinition  # noqa: E402

ANNOTATIONS = {"READ_ONLY": "read-only", "ADHOC": "read-only, open world", "ADMIN": "administrative"}


def first_line(doc: str | None) -> str:
    return " ".join((doc or "").strip().split("\n\n")[0].split()) or "—"


def build_tools() -> str:
    with open(os.path.join(SRC, "server.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "tool":
                ann = getattr(dec.args[0], "id", "?") if dec.args else "?"
                rows.append(f"| `{node.name}` | {ANNOTATIONS.get(ann, ann)} | {first_line(ast.get_docstring(node))} |")
    return "| Tool | Kind | Purpose |\n|---|---|---|\n" + "\n".join(rows)


def build_errors() -> str:
    rows = [f"| `{kind}` | {meaning} |" for kind, meaning in ERROR_KINDS.items()]
    return "| Kind | Caller's next step |\n|---|---|\n" + "\n".join(rows)


def build_modules() -> str:
    rows = []
    for dirpath, _, files in sorted(os.walk(SRC)):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            rel = os.path.relpath(path, os.path.join(ROOT, "src"))
            doc = first_line(ast.get_docstring(ast.parse(text)))
            rows.append(f"| `{rel}` | {len(text.splitlines())} | {doc} |")
    return "| Module | Lines | Responsibility |\n|---|---|---|\n" + "\n".join(rows)


def build_metrics() -> str:
    os.environ.setdefault("GM_KNOWLEDGE_VERSION", "docs")
    kb = load_knowledge(os.path.join(ROOT, "examples", "knowledge"))
    rows = []
    for key, metric in sorted(kb.metrics.items()):
        for unit, d in sorted(metric.units.items()):
            if isinstance(d, DerivedDefinition):
                shape = f"{d.op} · variants: " + ", ".join(
                    f"**{v}**" if v == d.default_variant else v for v in sorted(d.variants))
            else:
                shape = "measures: " + ", ".join(
                    f"**{m}**" if m == d.default_measure else m for m in sorted(d.measures))
                shape += " · " + ("snapshot" if d.snapshot else "period")
            matures = f"{d.maturity.settles_after_days} days" if d.maturity else "—"
            rows.append(f"| `{key}` | {unit} | {metric.kind} | v{metric.version} | {shape} | {matures} |")
    return ("| Metric | Unit | Kind | Version | Shape (default in bold) | Matures |\n"
            "|---|---|---|---|---|---|\n" + "\n".join(rows))


BUILDERS = {"tools": build_tools, "errors": build_errors, "modules": build_modules, "metrics": build_metrics}


def render(text: str) -> str:
    for name, build in BUILDERS.items():
        pattern = re.compile(rf"(<!-- gen:{name} -->\n)(?:.*?\n)?(<!-- /gen:{name} -->)", re.DOTALL)
        if not pattern.search(text):
            raise SystemExit(f"docs/reference.md is missing the gen:{name} block")
        text = pattern.sub(lambda m, b=build: m.group(1) + b() + "\n" + m.group(2), text)
    return text


def main(argv: list[str]) -> int:
    with open(DOC, encoding="utf-8") as f:
        current = f.read()
    fresh = render(current)
    if "--check" in argv:
        if fresh != current:
            print("docs/reference.md is stale: run `python scripts/gen_docs.py` and commit the result.",
                  file=sys.stderr)
            return 1
        return 0
    if fresh != current:
        with open(DOC, "w", encoding="utf-8") as f:
            f.write(fresh)
        print("docs/reference.md updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
