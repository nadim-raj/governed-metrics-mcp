"""Shared fragments: a business rule written once, referenced everywhere (ADR 0008).

A definition references a fragment as `{{ name(alias) }}`; the fragment's SQL uses `{a}`
for that alias. Expansion is textual and happens at load time, so the compiled statement
and the provenance stamp both reflect the fragment as it stood in that knowledge version.

Copies drift, so a definition may not inline a rule for which a fragment exists:
`find_inlined` recognises a fragment's body written out by hand (any alias, any spacing,
any case) and the validator rejects it.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .model import Fragment

REFERENCE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\}\}")
_ALIAS_SLOT = "{a}"


def references(text: str) -> list[tuple[str, str]]:
    """(fragment name, alias) for every reference in `text`."""
    return [(m.group(1), m.group(2)) for m in REFERENCE.finditer(text or "")]


def expand(text: str, fragments: Mapping[str, Fragment]) -> str:
    """Replace every reference with its fragment, parenthesised so it composes with AND/OR.
    An unknown name is a validation error reported by the loader before this runs."""
    def _sub(m: re.Match) -> str:
        frag = fragments[m.group(1)]
        return "(" + frag.sql.replace(_ALIAS_SLOT, m.group(2)) + ")"
    return REFERENCE.sub(_sub, text or "")


def _body_pattern(sql: str) -> re.Pattern:
    """A pattern for the fragment body as somebody might retype it: any alias in place of
    {a}, any run of whitespace where the fragment has whitespace, any case."""
    parts = []
    for piece in re.split(r"(\{a\}|\s+)", sql.strip()):
        if piece == _ALIAS_SLOT:
            parts.append(r"[A-Za-z_][A-Za-z0-9_]*")
        elif piece and piece.isspace():
            parts.append(r"\s+")
        elif piece:
            parts.append(re.escape(piece))
    return re.compile("".join(parts), re.IGNORECASE)


def find_inlined(text: str, fragments: Iterable[Fragment]) -> list[str]:
    """Names of fragments whose body appears written out in `text` (references removed
    first, so a correct reference never counts as an inline copy)."""
    bare = REFERENCE.sub(" ", text or "")
    return [f.name for f in fragments if _body_pattern(f.sql).search(bare)]
