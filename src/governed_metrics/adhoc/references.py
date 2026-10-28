"""Structural analysis of an ad-hoc statement, over tokens (ADR 0004, ADR 0005).

The analysis answers three questions the guard needs, and refuses whatever it cannot answer:

1. Is this one read-only SELECT? A statement must start with SELECT or WITH and contain no
   second statement. Within a SELECT the only write vectors are data-modifying CTEs,
   SELECT INTO and locking clauses, so those words are refused wherever they appear. Every
   other DDL or DCL keyword cannot occur inside a SELECT at all.
2. Which sources does it read? Every FROM and JOIN item is parsed: schema-qualified tables
   are recorded, top-level and nested CTEs are resolved with PostgreSQL's scoping rules,
   and anything else (an unqualified name, a three-part name, `ROWS FROM`, an unknown
   construct) is reported as a problem. A reference that cannot be classified is refused;
   it is never treated as probably fine.
3. Which functions does it call? Functions are allow-listed, not deny-listed. A relation
   can only be read through FROM/JOIN or through a function, so with FROM/JOIN fully parsed
   and functions allow-listed there is no third path.

Every bracket is classified (sub-query, function call, grouping, type modifier, column-alias
list) and every token is tagged with the clause it sits in. The personal-data guard reuses
this so that it reasons about structure, not spelling.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import NUMBER, QIDENT, WORD, Token, tokenize

# Words that, wherever they appear, make the statement something other than a pure read.
WRITE_WORDS = frozenset({"insert", "update", "delete", "merge", "into", "returning", "table"})
LOCKING_FOLLOWERS = frozenset({"update", "share", "no", "key"})

# Words that may directly precede "(" without being a function name.
NON_CALL_KEYWORDS = frozenset({
    "select", "from", "where", "and", "or", "not", "on", "using", "join", "lateral", "as",
    "by", "when", "then", "else", "case", "between", "like", "ilike", "similar", "to", "is",
    "distinct", "with", "recursive", "materialized", "union", "intersect", "except", "having",
    "limit", "offset", "fetch", "window", "partition", "order", "group", "filter", "over",
    "within", "sets", "values", "only", "escape", "overlaps", "at", "zone", "rows", "range",
    "groups", "preceding", "following", "in", "returning", "into", "table",
})

# Reserved and structural words: never an alias, never a table name.
KEYWORDS = NON_CALL_KEYWORDS | frozenset({
    "all", "any", "some", "array", "asc", "desc", "nulls", "first", "last", "end", "null",
    "true", "false", "left", "right", "full", "inner", "outer", "cross", "natural",
    "tablesample", "for", "unbounded", "current", "row", "ties", "ordinality", "collate",
    "default", "do", "check", "unique", "primary", "references", "both", "leading",
    "trailing", "placing", "symmetric", "asymmetric", "variadic", "cast", "column",
    "constraint", "create", "grant", "user", "current_user", "session_user", "current_role",
    "current_date", "current_time", "current_timestamp", "localtime", "localtimestamp",
})

# Words PostgreSQL never accepts as a bare table alias: its reserved and type/function-name
# keywords. Everything else, unreserved keywords such as `first` or `zone` included, can be an
# alias, so the parser must recognise it as one or a whole-row use of it goes unseen.
NOT_AN_ALIAS = frozenset({
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric", "both", "case",
    "cast", "check", "collate", "column", "constraint", "create", "current_catalog", "current_date",
    "current_role", "current_time", "current_timestamp", "current_user", "default", "deferrable",
    "desc", "distinct", "do", "else", "end", "except", "false", "fetch", "for", "foreign", "from",
    "grant", "group", "having", "in", "initially", "intersect", "into", "lateral", "leading", "limit",
    "localtime", "localtimestamp", "not", "null", "offset", "on", "only", "or", "order", "placing",
    "primary", "references", "returning", "select", "session_user", "some", "symmetric",
    "system_user", "table", "then", "to", "trailing", "true", "union", "unique", "user", "using",
    "variadic", "when", "where", "window", "with",
    "authorization", "binary", "collation", "concurrently", "cross", "current_schema", "freeze",
    "full", "ilike", "inner", "is", "isnull", "join", "left", "like", "natural", "notnull", "outer",
    "overlaps", "right", "similar", "tablesample", "verbose",
})
MAX_NESTING = 100

CLAUSE_WORDS = frozenset({"select", "from", "join", "where", "having", "window", "limit",
                          "offset", "fetch", "on", "using", "group", "order", "partition",
                          "union", "intersect", "except", "values", "with"})
LIST_END = frozenset({"where", "group", "having", "order", "limit", "offset", "fetch",
                      "window", "union", "intersect", "except", "for"})
# Functions whose argument list itself contains FROM: that FROM takes an expression.
FROM_ARG_FUNCTIONS = frozenset({"extract", "substring", "trim", "overlay"})
TYPE_WORDS = frozenset({"numeric", "decimal", "dec", "varchar", "char", "character", "varying",
                        "nchar", "national", "bit", "timestamp", "timestamptz", "time",
                        "timetz", "interval", "float"})

# Fail-closed: a function not listed here is refused. Adding one is a reviewed change with a
# test (ADR 0005: teach the parser the shape, never loosen the default).
ALLOWED_FUNCTIONS = frozenset({
    # aggregates
    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp", "variance",
    "var_pop", "var_samp", "bool_and", "bool_or", "every", "array_agg", "string_agg",
    "percentile_cont", "percentile_disc", "mode", "corr", "covar_pop", "covar_samp",
    "regr_slope", "regr_intercept", "regr_r2", "regr_count",
    # window
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
    # conditional and quantifiers
    "coalesce", "nullif", "greatest", "least", "exists", "any", "all", "some", "array", "row",
    "grouping", "rollup", "cube",
    # numeric
    "abs", "ceil", "ceiling", "floor", "round", "trunc", "sign", "sqrt", "cbrt", "power", "pow",
    "exp", "ln", "log", "log10", "mod", "div", "width_bucket",
    # text
    "lower", "upper", "initcap", "length", "char_length", "character_length", "octet_length",
    "btrim", "ltrim", "rtrim", "trim", "substring", "substr", "left", "right", "replace",
    "position", "strpos", "split_part", "concat", "concat_ws", "reverse",
    "starts_with", "translate", "regexp_replace", "regexp_match", "regexp_like",
    # date and time
    "date_trunc", "date_part", "date_bin", "extract", "age", "make_date", "make_time",
    "make_timestamp", "make_interval", "to_char", "to_date", "to_timestamp", "to_number", "now",
    "current_timestamp", "current_time", "localtimestamp", "localtime", "timezone",
    "justify_days", "justify_hours", "justify_interval", "isfinite", "cast",
    # arrays and json reads
    "array_length", "cardinality", "unnest", "array_position", "array_to_string",
    "generate_series", "json_extract_path_text", "jsonb_extract_path_text", "json_array_length",
    "jsonb_array_length", "jsonb_typeof", "json_typeof",
})
# Set-returning functions allowed in FROM position.
ALLOWED_FROM_FUNCTIONS = frozenset({"generate_series", "unnest"})


@dataclass(frozen=True)
class Problem:
    reason: str
    message: str


@dataclass(frozen=True)
class SourceRef:
    schema: str
    table: str
    alias: str | None
    index: int                     # token index of the schema token
    table_index: int               # token index of the table token

    @property
    def name(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True)
class Frame:
    open: int
    close: int
    kind: str                      # subquery | call | group | bracket | typemod | collist
    name: str | None = None        # function name for calls
    schema: str | None = None      # function schema when qualified
    prev: str | None = None        # value of the token just before the bracket


@dataclass(frozen=True)
class CteScope:
    name: str
    visible_from: int
    scope_end: int
    body_open: int


@dataclass
class Analysis:
    tokens: list[Token]
    frames: dict[int, Frame] = field(default_factory=dict)
    open_of: list[int] = field(default_factory=list)
    clause: list[str | None] = field(default_factory=list)
    table_from: set[int] = field(default_factory=set)
    sources: list[SourceRef] = field(default_factory=list)
    ctes: list[CteScope] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    # Every relation name in the statement (aliases, tables, derived tables, CTEs) and the
    # token positions where they are declared. A use anywhere else, as a bare value, is a
    # whole-row reference.
    relation_names: set[str] = field(default_factory=set)
    declarations: set[int] = field(default_factory=set)

    def problem(self, reason: str, message: str) -> None:
        self.problems.append(Problem(reason, message))

    def clause_at(self, i: int) -> str | None:
        """The clause governing token i, looking through plain grouping parentheses."""
        c, o = self.clause[i], self.open_of[i]
        while c is None and o != -1 and self.frames[o].kind == "group":
            c, o = self.clause[o], self.open_of[o]
        return c

    def cte_visible(self, name: str, i: int) -> bool:
        return any(c.name == name and c.visible_from <= i < c.scope_end for c in self.ctes)


def norm(tok: Token) -> str:
    """Identifier as PostgreSQL resolves it: unquoted folds to lower case, quoted is exact."""
    return tok.value


def _is_identifier(tok: Token | None) -> bool:
    """Conservative: treats every structural word as a keyword. Used where guessing
    "identifier" wrongly could hide a call or a table."""
    return tok is not None and (tok.kind == QIDENT or (tok.kind == WORD and tok.value not in KEYWORDS))


def _is_alias(tok: Token | None) -> bool:
    """PostgreSQL's rule for a bare alias. Used where missing an alias would hide a relation name."""
    return tok is not None and (tok.kind == QIDENT or (tok.kind == WORD and tok.value not in NOT_AN_ALIAS))


class _Analyzer:
    def __init__(self, tokens: list[Token]):
        self.t = tokens
        self.n = len(tokens)
        self.a = Analysis(tokens=tokens)

    def tok(self, i: int) -> Token | None:
        return self.t[i] if 0 <= i < self.n else None

    # ---- statement shape -------------------------------------------------------------
    def check_statement(self) -> bool:
        if not self.t:
            self.a.problem("not_a_select", "empty statement")
            return False
        if any(tk.is_punct(";") for tk in self.t):
            self.a.problem("multiple_statements", "only a single statement is allowed")
            return False
        if not self.t[0].is_word("select", "with"):
            self.a.problem("not_a_select", "only SELECT or WITH … SELECT statements are accepted")
            return False
        for i, tk in enumerate(self.t):
            if tk.kind != WORD:
                continue
            if tk.value == "table":
                # `TABLE x` reads a relation without FROM, so it would bypass source parsing.
                self.a.problem("unsupported_syntax", "TABLE syntax is not supported; use SELECT")
            elif tk.value in WRITE_WORDS:
                self.a.problem("write_statement",
                               f"'{tk.value.upper()}' is not permitted: the ad-hoc path is read-only")
            elif tk.value == "for":
                nxt = self.tok(i + 1)
                if nxt is not None and nxt.is_word(*LOCKING_FOLLOWERS):
                    self.a.problem("write_statement", "locking clauses (FOR UPDATE/SHARE) are not permitted")
        return not self.a.problems

    # ---- brackets --------------------------------------------------------------------
    def match_brackets(self) -> bool:
        stack: list[int] = []
        self.a.open_of = [-1] * self.n
        closes: dict[int, int] = {}
        for i, tk in enumerate(self.t):
            self.a.open_of[i] = stack[-1] if stack else -1
            if tk.is_punct("(", "["):
                stack.append(i)
                if len(stack) > MAX_NESTING:
                    self.a.problem("unsupported_syntax", f"brackets nest more than {MAX_NESTING} deep")
                    return False
            elif tk.is_punct(")", "]"):
                if not stack or self.t[stack[-1]].value != ("(" if tk.value == ")" else "["):
                    self.a.problem("unbalanced", "unbalanced brackets")
                    return False
                start = stack.pop()
                closes[start] = i
                self.a.open_of[i] = stack[-1] if stack else -1
        if stack:
            self.a.problem("unbalanced", "unbalanced brackets")
            return False
        self._closes = closes
        return True

    def _contents_only(self, k: int, kinds: tuple[str, ...]) -> bool:
        close = self._closes[k]
        for tk in self.t[k + 1:close]:
            if tk.is_punct(","):
                continue
            if tk.kind not in kinds:
                return False
        return close > k + 1

    def _typemod(self, k: int) -> bool:
        """`numeric(10,2)` after `::`, or inside CAST(x AS numeric(10,2))."""
        j = k - 1
        while j >= 0 and self.t[j].is_word(*TYPE_WORDS):
            j -= 1
        if j == k - 1:
            return False
        before = self.tok(j)
        in_cast = False
        o = self.a.open_of[k]
        if before is not None and before.is_word("as") and o != -1:
            fr = self.a.frames.get(o)
            in_cast = fr is not None and fr.kind == "call" and fr.name == "cast"
        return ((before is not None and before.is_punct("::")) or in_cast) and \
            self._contents_only(k, (NUMBER,))

    def _cte_collist(self, k: int) -> bool:
        after = self.tok(self._closes[k] + 1)
        nxt = self.tok(self._closes[k] + 2)
        return after is not None and after.is_word("as") and nxt is not None and \
            (nxt.is_punct("(") or nxt.is_word("not", "materialized"))

    def classify_frames(self) -> None:
        self._inferred_collists: set[int] = set()
        for k in sorted(self._closes):
            tk, close = self.t[k], self._closes[k]
            prev = self.tok(k - 1)
            prev_value = prev.value if prev is not None else None
            if tk.value == "[":
                kind, name, schema = "bracket", None, None
            elif (nxt := self.tok(k + 1)) is not None and nxt.is_word("select", "with", "values"):
                kind, name, schema = "subquery", None, None
            elif prev is not None and prev.kind in (WORD, QIDENT) and \
                    not (prev.kind == WORD and prev.value in NON_CALL_KEYWORDS):
                kind, name, schema = self._call_like(k, prev)
            else:
                kind, name, schema = "group", None, None
            self.a.frames[k] = Frame(k, close, kind, name, schema, prev_value)

    def _call_like(self, k: int, prev: Token) -> tuple[str, str | None, str | None]:
        before = self.tok(k - 2)
        if prev.kind == WORD and prev.value in TYPE_WORDS and self._typemod(k):
            return "typemod", None, None
        by_identifier = not self._cte_collist(k) and before is not None and \
            not before.is_word("as") and (before.is_punct(")") or _is_identifier(before))
        aliasish = self._cte_collist(k) or by_identifier or (before is not None and before.is_word("as"))
        # A column-alias list holds only names; anything else is treated as a call so the
        # allow-list decides. Misreading a call as an alias list must never skip that check.
        if aliasish and self._contents_only(k, (WORD, QIDENT)):
            if by_identifier:
                self._inferred_collists.add(k)
            return "collist", None, None
        schema = None
        if before is not None and before.is_punct("."):
            owner = self.tok(k - 3)
            schema = owner.value if owner is not None and owner.kind in (WORD, QIDENT) else "?"
        return "call", prev.value, schema

    def confirm_collists(self) -> None:
        """`alias(a, b)` after a bare alias is only valid in FROM. Anywhere else the same
        tokens are keyword syntax followed by a call, so they go back to the allow-list."""
        for k in self._inferred_collists:
            if self.a.clause_at(k) != "from":
                prev, before, owner = self.tok(k - 1), self.tok(k - 2), self.tok(k - 3)
                schema = (owner.value if owner is not None and owner.kind in (WORD, QIDENT) else "?") \
                    if before is not None and before.is_punct(".") else None
                fr = self.a.frames[k]
                self.a.frames[k] = Frame(fr.open, fr.close, "call", prev.value, schema, fr.prev)

    # ---- clauses ---------------------------------------------------------------------
    def mark_table_froms(self) -> None:
        for i, tk in enumerate(self.t):
            if not tk.is_word("from"):
                continue
            o = self.a.open_of[i]
            fr = self.a.frames.get(o) if o != -1 else None
            if fr is not None and fr.kind == "call" and fr.name in FROM_ARG_FUNCTIONS:
                continue
            prev, prev2, prev3 = self.tok(i - 1), self.tok(i - 2), self.tok(i - 3)
            if prev is not None and prev.is_word("distinct") and prev2 is not None and (
                    prev2.is_word("is") or (prev2.is_word("not") and prev3 is not None and prev3.is_word("is"))):
                continue
            if prev is not None and prev.is_word("rows"):
                self.a.problem("unsupported_syntax", "ROWS FROM (…) is not supported")
                continue
            self.a.table_from.add(i)

    def tag_clauses(self) -> None:
        current: dict[int, str | None] = {}
        self.a.clause = [None] * self.n
        for i, tk in enumerate(self.t):
            level = self.a.open_of[i]
            if tk.kind == WORD and tk.value in CLAUSE_WORDS:
                v = tk.value
                prev, nxt = self.tok(i - 1), self.tok(i + 1)
                # A function-argument FROM and DISTINCT ON are not clauses.
                not_a_clause = (v == "from" and i not in self.a.table_from) or \
                    (v == "on" and prev is not None and prev.is_word("distinct"))
                if not_a_clause:
                    pass
                elif v in ("group", "order", "partition"):
                    if nxt is not None and nxt.is_word("by"):
                        current[level] = v
                elif v in ("union", "intersect", "except"):
                    current[level] = None
                elif v == "join":
                    current[level] = "from"
                else:
                    current[level] = v
            self.a.clause[i] = current.get(level)

    # ---- CTEs ------------------------------------------------------------------------
    def scope_ctes(self) -> None:
        for w, tk in enumerate(self.t):
            if not tk.is_word("with"):
                continue
            nxt = self.tok(w + 1)
            if nxt is not None and nxt.is_word("ordinality", "time", "ties"):
                continue
            self._parse_with(w)

    def _parse_with(self, w: int) -> None:
        p, recursive, found = w + 1, False, []
        if (tk := self.tok(p)) is not None and tk.is_word("recursive"):
            recursive, p = True, p + 1
        while True:
            name_idx, name_tok = p, self.tok(p)
            if not _is_identifier(name_tok):
                self.a.problem("unsupported_syntax", "unrecognised WITH clause")
                return
            p += 1
            if (tk := self.tok(p)) is not None and tk.is_punct("("):
                p = self._closes[p] + 1
            if (tk := self.tok(p)) is None or not tk.is_word("as"):
                self.a.problem("unsupported_syntax", "unrecognised WITH clause")
                return
            p += 1
            if (tk := self.tok(p)) is not None and tk.is_word("not"):
                p += 1
            if (tk := self.tok(p)) is not None and tk.is_word("materialized"):
                p += 1
            body = self.tok(p)
            if body is None or not body.is_punct("(") or self.a.frames[p].kind != "subquery":
                self.a.problem("unsupported_syntax", "a CTE body must be a parenthesised query")
                return
            found.append((norm(name_tok), p, self._closes[p]))
            self.a.relation_names.add(norm(name_tok))
            self.a.declarations.add(name_idx)
            p = self._closes[p] + 1
            if (tk := self.tok(p)) is not None and tk.is_punct(","):
                p += 1
                continue
            break
        o = self.a.open_of[w]
        scope_end = self._closes[o] if o != -1 else self.n
        for name, body_open, body_close in found:
            # Non-recursive: a CTE is visible only after its own body. A name used before
            # that resolves through search_path to a real table, so it must not be trusted.
            visible = body_open if recursive else body_close
            self.a.ctes.append(CteScope(name, visible, scope_end, body_open))

    # ---- FROM items ------------------------------------------------------------------
    def parse_sources(self) -> None:
        for i, tk in enumerate(self.t):
            if i in self.a.table_from:
                end = self._item(i + 1)
                self._continue_list(end, self.a.open_of[i])
            elif tk.is_word("join"):
                self._item(i + 1)

    def _continue_list(self, p: int, level: int) -> None:
        while p < self.n:
            tk = self.t[p]
            if self.a.open_of[p] != level:
                return
            if tk.is_punct("(", "["):
                p = self._closes[p] + 1
            elif tk.is_punct(","):
                p = self._item(p + 1)
            elif tk.kind == WORD and tk.value in LIST_END:
                return
            else:
                p += 1

    def _item(self, p: int) -> int:
        while (tk := self.tok(p)) is not None and tk.is_word("only", "lateral"):
            p += 1
        tk = self.tok(p)
        if tk is None:
            self.a.problem("unclassifiable_reference", "FROM or JOIN without an item")
            return p
        if tk.is_punct("("):
            fr = self.a.frames[p]
            if fr.kind != "subquery":
                end = self._item(p + 1)                       # a parenthesised join group
                self._continue_list(end, p)
            return self._alias(fr.close + 1)[1]
        if not _is_identifier(tk):
            self.a.problem("unclassifiable_reference", f"unrecognised FROM item near '{tk.value}'")
            return p + 1
        first, second, third = self.tok(p + 1), self.tok(p + 2), self.tok(p + 3)
        if first is not None and first.is_punct(".") and second is not None and \
                second.kind in (WORD, QIDENT):
            if third is not None and third.is_punct("."):
                self.a.problem("unclassifiable_reference", "three-part names are not supported")
                return p + 4
            if third is not None and third.is_punct("("):
                self._from_function(norm(second), norm(tk))
                return self._alias(self._closes[p + 3] + 1)[1]
            after = p + 3
            if (star := self.tok(after)) is not None and star.is_op("*"):
                after += 1
            alias, end = self._alias(after)
            self.a.sources.append(SourceRef(norm(tk), norm(second), alias, p, p + 2))
            self._declare(p, p + 2)
            if alias is None:
                self.a.relation_names.add(norm(second))
            return end
        if first is not None and first.is_punct("("):
            self._from_function(norm(tk), None)
            return self._alias(self._closes[p + 1] + 1)[1]
        if self.a.cte_visible(norm(tk), p):
            self._declare(p)
            return self._alias(p + 1)[1]
        self.a.problem("unclassifiable_reference",
                       f"unqualified table name '{tk.value}': write every source as schema.table")
        return p + 1

    def _declare(self, *indexes: int) -> None:
        self.a.declarations.update(indexes)

    def _from_function(self, name: str, schema: str | None) -> None:
        if name not in ALLOWED_FROM_FUNCTIONS or schema not in (None, "pg_catalog"):
            shown = f"{schema}.{name}" if schema else name
            self.a.problem("function_not_permitted",
                           f"function '{shown}' is not permitted in FROM")

    def _alias(self, p: int) -> tuple[str | None, int]:
        alias = None
        tk = self.tok(p)
        if tk is not None and tk.is_word("as"):
            p += 1
            tk = self.tok(p)
            if not _is_alias(tk):
                self.a.problem("unclassifiable_reference", "AS must be followed by an alias")
                return None, p
        if _is_alias(tk):
            alias = norm(tk)
            self.a.relation_names.add(alias)
            self._declare(p)
            p += 1
            nxt = self.tok(p)
            if nxt is not None and nxt.is_punct("(") and self.a.frames[p].kind == "collist":
                p = self._closes[p] + 1
        return alias, p

    # ---- functions -------------------------------------------------------------------
    def check_functions(self) -> None:
        for fr in self.a.frames.values():
            if fr.kind != "call":
                continue
            if fr.name not in ALLOWED_FUNCTIONS or fr.schema not in (None, "pg_catalog"):
                shown = f"{fr.schema}.{fr.name}" if fr.schema else fr.name
                self.a.problem("function_not_permitted",
                               f"function '{shown}' is not permitted in ad-hoc queries")

    def run(self) -> Analysis:
        if not self.check_statement() or not self.match_brackets():
            return self.a
        self.classify_frames()
        self.mark_table_froms()
        self.tag_clauses()
        self.confirm_collists()
        self.scope_ctes()
        self.parse_sources()
        self.check_functions()
        return self.a


def analyze(sql: str) -> Analysis:
    """Tokenise and analyse `sql`. LexError propagates; structural problems are collected
    on the result, and the caller refuses the statement when any are present."""
    tokens = tokenize(sql)
    while tokens and tokens[-1].is_punct(";"):
        tokens.pop()
    return _Analyzer(tokens).run()
