"""Guards against rebuilding a masked personal field inside an ad-hoc query (ADR 0007).

The masking chokepoint masks result columns by *name*. That only holds if a personal value
always leaves the warehouse under its own column name, untransformed. So for a caller who
is not entitled to unmasked data, a personal column may appear only in these positions:

  1. selected as-is, with no alias, in a select list whose rows reach the output by name
     (the top level, a CTE body, or a sub-query in FROM; never a scalar sub-query);
  2. counted: COUNT(col) or COUNT(DISTINCT col);
  3. compared for equality in WHERE / ON / HAVING (=, <>, !=, IS, IN);
  4. listed in GROUP BY / ORDER BY / PARTITION BY, or in JOIN … USING (…).

Anything else (a function argument, a concatenation, a cast, CASE, a rename, a LIKE pattern)
is refused. With a personal source in the statement, these are refused too: set operations
(UNION re-labels a column by position), column-alias lists (`AS t(x)` renames), and
whole-row references (`SELECT t`, `row_to_json(t)`, `(t.*)::text`), which carry every
column out under a single name.

Residual risk, stated in docs/security.md: equality comparison is an oracle. A caller can
confirm a guess one comparison at a time. The per-unit database role (and, where it matters,
a column-level grant) is the boundary; this guard narrows what the query path can attempt.
"""
from __future__ import annotations

from collections.abc import Mapping

from .lexer import QIDENT, WORD, Token
from .references import Analysis, Problem

_REASON = "personal_data_reconstruction"
_EQUALITY_OPS = frozenset({"=", "<>", "!="})
_SELECT_ITEM_BEFORE = frozenset({"select", "distinct", "all"})
_EXPR_START = frozenset({"where", "on", "having", "and", "or", "not", "when"})
_EXPR_END = frozenset({"and", "or", "then", "group", "order", "limit", "offset", "having",
                       "window", "join", "inner", "left", "right", "full", "cross", "natural",
                       "where", "fetch", "for", "union", "intersect", "except"})
_LIST_END = frozenset({"asc", "desc", "nulls", "limit", "offset", "having", "window", "fetch",
                       "order", "rows", "range", "groups", "union", "intersect", "except", "for"})


class _Check:
    def __init__(self, a: Analysis):
        self.a = a
        self.t = a.tokens
        self.cte_bodies = {c.body_open for c in a.ctes}

    def tok(self, i: int) -> Token | None:
        return self.t[i] if 0 <= i < len(self.t) else None

    def frame_kind(self, o: int) -> str:
        return "top" if o == -1 else self.a.frames[o].kind

    # ---- where does a sub-query's output go? -------------------------------------------
    def subquery_context(self, o: int) -> str:
        """'relation' (rows reach the outer query by column name), 'filter' (the rows only
        decide a predicate) or 'output' (the value is re-labelled or computed on)."""
        if o in self.cte_bodies:
            return "relation"
        prev = self.tok(o - 1)
        if prev is not None:
            if (prev.is_word("from") and (o - 1) in self.a.table_from) or \
                    prev.is_word("join", "lateral"):
                return "relation"
            if prev.is_punct(",", "(") and self.a.clause_at(o) == "from":
                return "relation"
        if self.a.clause_at(o) in ("where", "on", "having"):
            return "filter"
        return "output"

    def output_path_ok(self, o: int) -> bool:
        while o != -1:
            kind = self.a.frames[o].kind
            if kind == "group":
                o = self.a.open_of[o]
                continue
            if kind != "subquery":
                return False
            ctx = self.subquery_context(o)
            if ctx == "filter":
                return True
            if ctx != "relation":
                return False
            o = self.a.open_of[o]
        return True

    # ---- the four permitted positions --------------------------------------------------
    def select_item(self, start: int, end: int) -> bool:
        o = self.a.open_of[start]
        if self.frame_kind(o) not in ("top", "subquery") or self.a.clause[start] != "select":
            return False
        before, after = self.tok(start - 1), self.tok(end + 1)
        if before is None or not (before.is_word(*_SELECT_ITEM_BEFORE) or before.is_punct(",")):
            return False
        if not (after is None or after.is_punct(",", ")") or after.is_word("from")):
            return False
        return self.output_path_ok(o)

    def counted(self, start: int, end: int) -> bool:
        o = self.a.open_of[start]
        if o == -1:
            return False
        fr = self.a.frames[o]
        before, after = self.tok(start - 1), self.tok(end + 1)
        return fr.kind == "call" and fr.name == "count" and after is not None and \
            after.is_punct(")") and before is not None and \
            (start - 1 == o or (before.is_word("distinct") and start - 2 == o))

    def compared(self, start: int, end: int) -> bool:
        if self.a.clause_at(start) not in ("where", "on", "having"):
            return False
        o = self.a.open_of[start]
        while o != -1 and self.a.frames[o].kind == "group":
            o = self.a.open_of[o]
        if self.frame_kind(o) not in ("top", "subquery"):
            return False
        before, after = self.tok(start - 1), self.tok(end + 1)
        after2 = self.tok(end + 2)
        starts = before is not None and (before.is_word(*_EXPR_START) or before.is_punct("("))
        ends = after is None or after.is_punct(")", ",") or after.is_word(*_EXPR_END)
        compares_right = after is not None and (
            after.is_op(*_EQUALITY_OPS) or after.is_word("is", "in")
            or (after.is_word("not") and after2 is not None and after2.is_word("in")))
        compares_left = before is not None and before.is_op(*_EQUALITY_OPS)
        return (starts and compares_right) or (compares_left and ends)

    def listed(self, start: int, end: int) -> bool:
        o = self.a.open_of[start]
        before, after = self.tok(start - 1), self.tok(end + 1)
        if o != -1 and self.a.frames[o].kind == "group" and self.a.frames[o].prev == "using":
            return before is not None and before.is_punct("(", ",") and \
                after is not None and after.is_punct(",", ")")
        if self.frame_kind(o) not in ("top", "subquery", "group"):
            return False
        if self.a.clause[start] not in ("group", "order", "partition"):
            return False
        if before is None or not (before.is_word("by") or before.is_punct(",")):
            return False
        return after is None or after.is_punct(",", ")") or after.is_word(*_LIST_END)

    # ---- scanning ----------------------------------------------------------------------
    def qualified_start(self, i: int) -> int:
        """Index of the first token of a possibly qualified name ending at i (a.b.c)."""
        start = i
        while (dot := self.tok(start - 1)) is not None and dot.is_punct(".") and \
                (owner := self.tok(start - 2)) is not None and owner.kind in (WORD, QIDENT):
            start -= 2
        return start

    def column_problems(self, personal: frozenset[str]) -> list[Problem]:
        out = []
        for i, tk in enumerate(self.t):
            if tk.kind not in (WORD, QIDENT) or tk.value not in personal or i in self.a.declarations:
                continue
            prev, nxt = self.tok(i - 1), self.tok(i + 1)
            if (prev is not None and prev.is_word("as")) or (nxt is not None and nxt.is_punct("(", ".")):
                continue      # an output label, a function name, or a qualifier, not this column
            start = self.qualified_start(i)
            if not (self.select_item(start, i) or self.counted(start, i)
                    or self.compared(start, i) or self.listed(start, i)):
                out.append(Problem(_REASON,
                    f"personal column '{tk.value}' may only be selected as-is, counted, compared "
                    "for equality or grouped; renaming, transforming or concatenating it is "
                    "refused for callers without unmasked access"))
        return out

    def whole_row_problems(self) -> list[Problem]:
        out = []
        for i, tk in enumerate(self.t):
            prev, nxt = self.tok(i - 1), self.tok(i + 1)
            if tk.is_op("*"):
                # A star after '.', ',' or SELECT expands a row; anywhere else (`a * b`,
                # `count(*)`) it is multiplication or the count-all form.
                star = prev is None or prev.is_punct(".", ",") or prev.is_word("select", "distinct")
                if not star:
                    continue
                start = self.qualified_start(i) if prev is not None and prev.is_punct(".") else i
                if not self.select_item(start, i):
                    out.append(Problem(_REASON, "a whole-row expansion (*) may only be selected "
                                                "directly when a source holds personal data"))
            elif tk.kind in (WORD, QIDENT) and tk.value in self.a.relation_names and \
                    i not in self.a.declarations and \
                    not (prev is not None and prev.is_punct(".")) and \
                    not (nxt is not None and nxt.is_punct(".", "(")):
                out.append(Problem(_REASON,
                    f"'{tk.value}' is used as a whole-row value; select named columns instead"))
        return out


def personal_problems(a: Analysis, personal_by_source: Mapping[str, frozenset[str]]) -> list[Problem]:
    """Problems that would let a personal value leave under a name the masking chokepoint
    does not recognise. Empty when no referenced source declares personal columns."""
    personal: set[str] = set()
    for ref in a.sources:
        personal |= personal_by_source.get(ref.name, frozenset())
    if not personal:
        return []
    check = _Check(a)
    problems: list[Problem] = []
    if any(tk.is_word("union", "intersect", "except") for tk in a.tokens):
        problems.append(Problem(_REASON, "set operations (UNION/INTERSECT/EXCEPT) re-label columns "
                                         "by position and are refused when a source holds personal data"))
    if any(fr.kind == "collist" for fr in a.frames.values()):
        problems.append(Problem(_REASON, "column-alias lists rename columns and are refused when a "
                                         "source holds personal data"))
    problems += check.whole_row_problems()
    problems += check.column_problems(frozenset(personal))
    return problems
