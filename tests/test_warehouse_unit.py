"""PostgresWarehouse against a fake psycopg pool: shape checks, timeout mapping, cursors."""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from governed_metrics import __main__ as entry
from governed_metrics.engine.compiler import Statement
from governed_metrics.errors import Failure
from governed_metrics.warehouse import ADHOC_CURSOR, PostgresWarehouse, StatementTimeout, _reset, session_options


class _Col:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, conn, name=None):
        self.conn, self.name = conn, name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((self.name, sql, params))
        if self.conn.raise_on_execute:
            raise self.conn.raise_on_execute
        self.description = [_Col(c) for c in self.conn.columns]

    def fetchmany(self, n):
        return self.conn.rows[:n]


class FakeConn:
    def __init__(self, columns=("value",), rows=((1,),), raise_on_execute=None):
        self.columns, self.rows, self.raise_on_execute = columns, list(rows), raise_on_execute
        self.executed, self.log = [], []

    def cursor(self, name=None):
        return FakeCursor(self, name)

    def rollback(self):
        self.log.append("rollback")

    def commit(self):
        self.log.append("commit")

    def execute(self, sql):
        self.log.append(sql)


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    @contextmanager
    def connection(self):
        yield self.conn

    def close(self):
        self.closed = True


def warehouse_with(conn):
    wh = PostgresWarehouse(lambda unit: "postgresql://x", statement_timeout_ms=1000)
    wh._pools["unit_a"] = FakePool(conn)
    return wh


def test_session_options_pin_read_only_and_the_lexer_assumptions():
    opts = session_options(1500)
    for part in ("default_transaction_read_only=on", "statement_timeout=1500",
                 "standard_conforming_strings=on", "search_path=pg_catalog"):
        assert part in opts


def test_reset_discards_session_state_before_reuse():
    conn = FakeConn()
    _reset(conn)
    assert conn.log == ["rollback", "RESET ALL", "commit"]


def test_scalar_binds_parameters_and_requires_one_cell():
    conn = FakeConn(rows=[(42,)])
    wh = warehouse_with(conn)
    assert wh.scalar("unit_a", Statement("SELECT %(x)s", {"x": 1})) == 42
    assert conn.executed[-1] == (None, "SELECT %(x)s", {"x": 1})
    conn.rows = [(1,), (2,)]
    with pytest.raises(Failure) as e:
        wh.scalar("unit_a", Statement("SELECT 1", {}))
    assert e.value.reason == "definition_shape"


def test_statement_timeouts_are_mapped_for_the_retry():
    wh = warehouse_with(FakeConn(raise_on_execute=psycopg.errors.QueryCanceled("canceling statement")))
    with pytest.raises(StatementTimeout):
        wh.scalar("unit_a", Statement("SELECT 1", {}))


def test_adhoc_uses_a_server_side_cursor_without_parameters():
    conn = FakeConn(columns=("n",), rows=[(i,) for i in range(10)])
    cols, rows = warehouse_with(conn).adhoc("unit_a", "select 100%", 3)
    assert cols == ["n"] and len(rows) == 3
    assert conn.executed[-1] == (ADHOC_CURSOR, "select 100%", None)


def test_rows_and_ping_and_close():
    conn = FakeConn(columns=("dimension", "value"), rows=[("web", 1)])
    wh = warehouse_with(conn)
    assert wh.rows("unit_a", Statement("SELECT 1", {}), 10) == (["dimension", "value"], [("web", 1)])
    conn.columns, conn.rows = ("value",), [(1,)]
    assert wh.ping("unit_a") is True
    conn.raise_on_execute = RuntimeError("down")
    assert wh.ping("unit_a") is False
    pool = wh._pools["unit_a"]
    wh.close()
    assert pool.closed and wh._pools == {}


def test_a_unit_without_a_credential_fails_closed():
    def no_dsn(unit):
        raise LookupError(f"no warehouse credential configured for unit '{unit}'")
    wh = PostgresWarehouse(no_dsn, statement_timeout_ms=1000)
    with pytest.raises(Failure) as e:
        wh.scalar("unit_b", Statement("SELECT 1", {}))
    assert e.value.reason == "unit_not_configured"


def test_entry_point_refuses_unsafe_configuration(monkeypatch):
    monkeypatch.setenv("GM_TRANSPORT", "streamable-http")
    monkeypatch.setenv("GM_AUTH_MODE", "none")
    assert entry.main() == 2


def test_entry_point_refuses_invalid_knowledge(monkeypatch, tmp_path):
    monkeypatch.setenv("GM_TRANSPORT", "stdio")
    monkeypatch.setenv("GM_KNOWLEDGE_PATH", str(tmp_path / "missing"))
    assert entry.main() == 2


def test_entry_point_reports_bad_numbers(monkeypatch):
    monkeypatch.setenv("GM_PORT", "eighty")
    assert entry.main() == 2
