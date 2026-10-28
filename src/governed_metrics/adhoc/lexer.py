"""A single-pass SQL lexer that reads a statement the way PostgreSQL does (ADR 0004).

Regular expressions inspect how a statement is *spelled*; comments, quoting, nested
sub-selects and whitespace change the spelling without changing what the statement does.
This lexer is always in exactly one state (code, comment, literal, quoted identifier) and
emits tokens, so every later decision is made over tokens, never over raw text.

Anything the lexer cannot tokenise exactly as the warehouse would is refused, not guessed:

* non-ASCII outside literals and quoted identifiers (PostgreSQL treats such bytes as
  identifier characters, so a no-break space would *join* two words the guard sees apart);
* Unicode-escape forms (U&'…', U&"…"), whose meaning depends on UESCAPE;
* positional parameters ($1) and a lone '$';
* a number glued to an identifier (`1from`), which PostgreSQL versions lex differently;
* anything unterminated.

Assumes `standard_conforming_strings = on` (backslash is literal in '…', an escape only in
E'…'). The warehouse connection sets it explicitly, so the assumption is pinned, not hoped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

WORD = "word"          # unquoted identifier or keyword; value is lower-cased
QIDENT = "qident"      # "quoted identifier"; value is exact
STRING = "string"      # any string literal, including dollar-quoted
NUMBER = "number"
OP = "op"              # operator characters, e.g. =, <>, ||, ->>
PUNCT = "punct"        # ( ) [ ] , ; . : :: :=

_WHITESPACE = " \t\n\r\f\v"
_OP_CHARS = "~!@#^&|`?+-*/%<>="
# PostgreSQL trims a trailing + or - from a multi-character operator unless the operator
# contains one of these (scan.l, "operator" rule). Mirrored so `a=-1` lexes as `=` `-`.
_OP_KEEP_TAIL = set("~!@#^&|`?%")
_PUNCT_CHARS = "()[],;."
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_NUMBER = re.compile(
    r"0[xX][0-9A-Fa-f_]+|0[oO][0-7_]+|0[bB][01_]+"
    r"|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9]+)?"
)


class LexError(ValueError):
    """The statement cannot be tokenised unambiguously; refuse it."""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int

    def is_word(self, *words: str) -> bool:
        return self.kind == WORD and (not words or self.value in words)

    def is_punct(self, *chars: str) -> bool:
        return self.kind == PUNCT and (not chars or self.value in chars)

    def is_op(self, *ops: str) -> bool:
        return self.kind == OP and (not ops or self.value in ops)


def _ident_start(ch: str) -> bool:
    return ch.isascii() and (ch.isalpha() or ch == "_")


def _ident_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "_$")


def _scan_quoted(sql: str, i: int, quote: str, backslash_escapes: bool) -> int:
    """Index just past the literal or identifier whose opening quote is at i. A doubled quote
    is an escaped quote; inside E'…' so is a backslash followed by any character."""
    j, n = i + 1, len(sql)
    while j < n:
        ch = sql[j]
        if backslash_escapes and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if j + 1 < n and sql[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    raise LexError("unterminated quoted identifier" if quote == '"' else "unterminated string literal")


def _scan_block_comment(sql: str, i: int) -> int:
    """Index just past the /* … */ opened at i. PostgreSQL block comments nest."""
    depth, j, n = 1, i + 2, len(sql)
    while j < n and depth:
        if sql.startswith("/*", j):
            depth, j = depth + 1, j + 2
        elif sql.startswith("*/", j):
            depth, j = depth - 1, j + 2
        else:
            j += 1
    if depth:
        raise LexError("unterminated block comment")
    return j


def _scan_operator(sql: str, i: int) -> int:
    j, n = i, len(sql)
    while j < n and sql[j] in _OP_CHARS:
        # A comment start inside an operator run ends the operator (scan.l does the same).
        if j > i and (sql.startswith("--", j) or sql.startswith("/*", j)):
            break
        j += 1
    while j - i > 1 and sql[j - 1] in "+-" and not (_OP_KEEP_TAIL & set(sql[i:j])):
        j -= 1
    return j


def tokenize(sql: str) -> list[Token]:
    """Tokens of `sql` with comments and whitespace removed. Raises LexError when the text
    cannot be read exactly as PostgreSQL would read it."""
    if "\x00" in sql:
        raise LexError("NUL character in statement")
    out: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in _WHITESPACE:
            i += 1
        elif ch == "-" and nxt == "-":
            j = i
            while j < n and sql[j] not in "\n\r":
                j += 1
            i = j
        elif ch == "/" and nxt == "*":
            i = _scan_block_comment(sql, i)
        elif ch in "uU" and nxt == "&":
            raise LexError("Unicode-escape strings and identifiers (U&) are not supported")
        elif ch in "eE" and nxt == "'":
            j = _scan_quoted(sql, i + 1, "'", backslash_escapes=True)
            out.append(Token(STRING, "", i, j))
            i = j
        elif ch in "bBxXnN" and nxt == "'":
            j = _scan_quoted(sql, i + 1, "'", backslash_escapes=False)
            out.append(Token(STRING, "", i, j))
            i = j
        elif ch == "'":
            j = _scan_quoted(sql, i, "'", backslash_escapes=False)
            out.append(Token(STRING, "", i, j))
            i = j
        elif ch == '"':
            j = _scan_quoted(sql, i, '"', backslash_escapes=False)
            ident = sql[i + 1:j - 1].replace('""', '"')
            if not ident:
                raise LexError("zero-length quoted identifier")
            out.append(Token(QIDENT, ident, i, j))
            i = j
        elif ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if not m:
                raise LexError("positional parameters and bare '$' are not supported")
            close = sql.find(m.group(0), m.end())
            if close < 0:
                raise LexError("unterminated dollar-quoted string")
            j = close + len(m.group(0))
            out.append(Token(STRING, "", i, j))
            i = j
        elif ch.isdigit() or (ch == "." and nxt.isdigit()):
            m = _NUMBER.match(sql, i)
            j = m.end() if m else i + 1
            if j < n and (_ident_char(sql[j]) or not sql[j].isascii()):
                raise LexError("a number may not be followed directly by an identifier")
            out.append(Token(NUMBER, sql[i:j], i, j))
            i = j
        elif _ident_start(ch):
            j = i + 1
            while j < n and _ident_char(sql[j]):
                j += 1
            if j < n and not sql[j].isascii():
                raise LexError("non-ASCII characters outside literals are not supported")
            out.append(Token(WORD, sql[i:j].lower(), i, j))
            i = j
        elif ch == ":":
            if nxt == ":":
                out.append(Token(PUNCT, "::", i, i + 2))
                i += 2
            elif nxt == "=":
                out.append(Token(PUNCT, ":=", i, i + 2))
                i += 2
            else:
                out.append(Token(PUNCT, ":", i, i + 1))
                i += 1
        elif ch in _PUNCT_CHARS:
            out.append(Token(PUNCT, ch, i, i + 1))
            i += 1
        elif ch in _OP_CHARS:
            j = _scan_operator(sql, i)
            out.append(Token(OP, sql[i:j], i, j))
            i = j
        elif not ch.isascii():
            raise LexError("non-ASCII characters outside literals are not supported")
        else:
            raise LexError(f"unexpected character {ch!r}")
    return out
