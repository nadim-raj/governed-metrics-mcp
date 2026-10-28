"""The masking chokepoint: the one place personal data is masked on the way out (ADR 0007).

Every tool result passes through `apply` (tools.py guarantees it by construction), so a new
tool is covered without anyone remembering to add masking to it. The chokepoint only passes
result shapes it understands: an unknown `kind` is refused, not passed through, because a
shape it cannot read is a shape it cannot mask.

For a caller without unmasked access:
  * every email-shaped string anywhere in the result is masked (`j***@example.com`);
  * a breakdown by a personal dimension has its group labels masked;
  * an ad-hoc result has every column named as a personal column masked. The ad-hoc guard
    (adhoc/personal.py) is what makes the name a reliable signal.
"""
from __future__ import annotations

import re
from typing import Any

from .errors import Failure

KINDS = frozenset({"metric", "breakdown", "composite", "comparison", "tabular", "rules", "rule",
                   "catalog", "sources", "source", "glossary", "reload", "error"})
MASK = "***"
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# The caller's own identity is not personal data being disclosed to them.
_EXEMPT_KEYS = frozenset({"caller"})


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    if not domain or local.endswith(MASK):
        return address
    return f"{local[:1]}{MASK}@{domain}"


def mask_value(value: Any) -> Any:
    """Mask one personal value, keeping just enough shape to read a table by."""
    if value is None:
        return None
    text = str(value)
    if _EMAIL.fullmatch(text):
        return mask_email(text)
    return f"{text[:1]}{MASK}" if len(text) > 3 else MASK


def _mask_strings(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (v if k in _EXEMPT_KEYS else _mask_strings(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_strings(v) for v in obj]
    if isinstance(obj, str) and "@" in obj:
        return _EMAIL.sub(lambda m: mask_email(m.group(0)), obj)
    return obj


def _mask_tabular(result: dict, personal: frozenset[str]) -> dict:
    columns = result.get("columns") or []
    idx = [i for i, c in enumerate(columns) if str(c).lower() in personal]
    if not idx:
        return result
    rows = [[mask_value(v) if i in idx else v for i, v in enumerate(row)] for row in result.get("rows") or []]
    return {**result, "rows": rows, "masked_columns": [columns[i] for i in idx]}


def apply(result: Any, *, unmasked: bool, personal_columns: frozenset[str]) -> dict:
    if not isinstance(result, dict) or result.get("kind") not in KINDS:
        raise Failure("A result reached the masking chokepoint in a shape it does not understand, "
                      "so it was withheld.", reason="unrecognised_result_shape")
    if unmasked:
        return result
    out = result
    if out["kind"] == "breakdown" and out.get("dimension_personal"):
        out = {**out, "rows": [{**row, "dimension": mask_value(row.get("dimension"))}
                               for row in out.get("rows") or []]}
    elif out["kind"] == "tabular":
        out = _mask_tabular(out, personal_columns)
    return _mask_strings(out)
