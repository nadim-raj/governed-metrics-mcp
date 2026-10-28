"""The ad-hoc guard: decide whether a caller-supplied statement may run, before it runs.

Order matters and every step fails closed:

  1. lex      — the statement must tokenise exactly as the warehouse would read it;
  2. analyse  — one read-only SELECT, every source classified, every function allow-listed;
  3. sources  — each source must belong to the unit the query runs under (positive list),
                and to the caller's own source allow-list;
  4. personal — for callers without unmasked access, no construction that would carry a
                personal value out under another name.

The guard narrows what is attempted. The unit's read-only database role bounds what is
possible (ADR 0006): even an approved statement runs with a credential that cannot see any
other unit's sources.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from ..errors import AccessDenied, InvalidRequest
from ..knowledge.model import Unit
from .lexer import LexError
from .personal import personal_problems
from .references import Problem, analyze

MAX_STATEMENT_CHARS = 20_000
ALL_SOURCES = "*"


@dataclass(frozen=True)
class Approved:
    sql: str                       # exactly the text that was analysed, minus trailing ';'
    sources: tuple[str, ...]


def _refuse(problems: list[Problem]) -> InvalidRequest:
    head = problems[0]
    detail = "; ".join(dict.fromkeys(p.message for p in problems[:3]))
    return InvalidRequest(f"Statement refused: {detail}.", reason=head.reason,
                          hint="Ad-hoc statements must be one read-only SELECT over schema-qualified "
                               "sources. Prefer a canonical metric where one exists.")


def check(sql: str, *, unit: Unit, caller_sources: Collection[str], unmasked: bool) -> Approved:
    """Approve `sql` for execution under `unit`, or raise InvalidRequest / AccessDenied.

    `caller_sources` is the caller's ad-hoc allow-list for this unit: source names, or
    ALL_SOURCES for every source the unit declares."""
    if not isinstance(sql, str) or not sql.strip():
        raise InvalidRequest("Statement is empty.", reason="not_a_select")
    if len(sql) > MAX_STATEMENT_CHARS:
        raise InvalidRequest(f"Statement exceeds {MAX_STATEMENT_CHARS} characters.",
                             reason="statement_too_long")
    try:
        analysis = analyze(sql)
    except LexError as e:
        raise InvalidRequest(f"Statement refused: {e}.", reason="lexical_error") from e
    if analysis.problems:
        raise _refuse(analysis.problems)

    names = tuple(dict.fromkeys(ref.name for ref in analysis.sources))
    for name in names:
        if name not in unit.sources:
            raise AccessDenied(f"'{name}' is not a readable source for unit '{unit.name}'.",
                               reason="source_not_in_unit",
                               hint="Use list_sources to see what this unit exposes.")
        if ALL_SOURCES not in caller_sources and name not in caller_sources:
            raise AccessDenied(f"Your ad-hoc access does not include '{name}'.",
                               reason="source_not_permitted")

    if not unmasked:
        personal = {name: unit.sources[name].personal_columns for name in names}
        if problems := personal_problems(analysis, personal):
            raise _refuse(problems)

    tokens = analysis.tokens
    return Approved(sql=sql[tokens[0].start:tokens[-1].end], sources=names)
