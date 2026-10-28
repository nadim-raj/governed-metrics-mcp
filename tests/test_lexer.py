"""The lexer must read a statement exactly as PostgreSQL does, or refuse it."""
from __future__ import annotations

import pytest

from governed_metrics.adhoc.lexer import NUMBER, OP, PUNCT, QIDENT, STRING, WORD, LexError, tokenize


def kinds(sql):
    return [(t.kind, t.value) for t in tokenize(sql)]


def test_words_are_case_folded_and_quoted_identifiers_are_exact():
    assert kinds('SELECT "MixedCase" FROM Unit_A.Orders') == [
        (WORD, "select"), (QIDENT, "MixedCase"), (WORD, "from"), (WORD, "unit_a"), (PUNCT, "."),
        (WORD, "orders")]


def test_comments_are_whitespace_and_block_comments_nest():
    assert kinds("select /* a /* nested */ still comment */ 1 -- trailing\n") == [
        (WORD, "select"), (NUMBER, "1")]


@pytest.mark.parametrize("sql", [
    "select 'from unit_b.orders'",
    "select 'it''s' ",
    "select e'\\' from unit_b.orders'",
    "select $$ from unit_b.orders $$",
    "select $tag$ ; drop $tag$",
    "select b'1010', x'ff', n'text'",
])
def test_literal_contents_are_never_tokens(sql):
    toks = tokenize(sql)
    assert all(t.kind != WORD or t.value == "select" for t in toks)
    assert any(t.kind == STRING for t in toks)


def test_doubled_quote_inside_identifier():
    assert kinds('select "a""b"') == [(WORD, "select"), (QIDENT, 'a"b')]


def test_operators_follow_postgres_trailing_sign_rule():
    assert [t.value for t in tokenize("a=-1") if t.kind == OP] == ["=", "-"]
    assert [t.value for t in tokenize("a||b") if t.kind == OP] == ["||"]
    assert [t.value for t in tokenize("x::numeric") if t.kind == PUNCT] == ["::"]


def test_operator_run_stops_at_comment():
    assert [t.value for t in tokenize("a =--c\n1") if t.kind == OP] == ["="]


@pytest.mark.parametrize("sql,why", [
    ("select 'open", "unterminated string"),
    ('select "open', "unterminated quoted identifier"),
    ("select /* open", "unterminated block comment"),
    ("select $$ open", "unterminated dollar"),
    ("select $1", "positional"),
    ("select u&'\\0041'", "Unicode-escape"),
    ('select U&"x"', "Unicode-escape"),
    ("select x", "non-ASCII"),
    ("select 1from t", "number may not be followed"),
    ("select 1 \\ 2", "unexpected character"),
    ("select ``", None),
    ('select ""', "zero-length"),
    ("select 1\x00", "NUL"),
])
def test_ambiguous_input_is_refused(sql, why):
    if why is None:
        tokenize(sql)          # backtick is an operator character in PostgreSQL
        return
    with pytest.raises(LexError, match=why):
        tokenize(sql)


def test_non_ascii_inside_literals_is_fine():
    assert any(t.kind == STRING for t in tokenize("select 'café'"))
