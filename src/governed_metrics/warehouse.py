"""Warehouse access, one read-only credential per unit (ADR 0006).

Read-only is enforced in layers, outermost first:

  1. the database role: each unit's DSN logs in as a role that can read only that unit's
     sources (examples/warehouse/init shows the grants). This is the boundary;
  2. the session: `default_transaction_read_only=on`, a `statement_timeout`, a fixed
     `search_path` and `standard_conforming_strings=on` (the lexer's assumption, pinned);
  3. pool hygiene: every connection is rolled back and `RESET ALL` on return, so nothing a
     statement does to its session survives into the next caller's checkout;
  4. the application guards in front of this module.

The engine depends on the `Warehouse` protocol, not on PostgreSQL, so the compiler,
executor and guard are unit-tested against a fake (tests/fakes.py).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

from .engine.compiler import Statement
from .errors import Failure

log = logging.getLogger("governed_metrics.warehouse")

ADHOC_CURSOR = "gm_adhoc"


class StatementTimeout(Exception):
    """The warehouse cancelled a statement at its timeout. Retried once by the executor."""


class Warehouse(Protocol):
    def scalar(self, unit: str, statement: Statement) -> Any: ...
    def rows(self, unit: str, statement: Statement, max_rows: int) -> tuple[list[str], list[tuple]]: ...
    def adhoc(self, unit: str, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]: ...
    def ping(self, unit: str) -> bool: ...
    def close(self) -> None: ...


def session_options(statement_timeout_ms: int) -> str:
    return " ".join([
        "-c default_transaction_read_only=on",
        f"-c statement_timeout={int(statement_timeout_ms)}",
        f"-c idle_in_transaction_session_timeout={int(statement_timeout_ms) * 2}",
        "-c standard_conforming_strings=on",
        "-c search_path=pg_catalog",
    ])


def _reset(conn) -> None:
    """Pool check-in: discard all session state. Without this a statement that changed a
    setting would leave it in place for whichever caller (and unit) checks the connection
    out next. RESET ALL restores the startup options, re-arming the read-only guard."""
    conn.rollback()
    conn.execute("RESET ALL")
    conn.commit()


class PostgresWarehouse:
    def __init__(self, dsn_for: Callable[[str], str], *, statement_timeout_ms: int,
                 pool_max_size: int = 8):
        self._dsn_for = dsn_for
        self._options = session_options(statement_timeout_ms)
        self._pool_max = pool_max_size
        self._pools: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _pool(self, unit: str):
        pool = self._pools.get(unit)
        if pool is not None:
            return pool
        with self._lock:
            pool = self._pools.get(unit)
            if pool is None:
                from psycopg_pool import ConnectionPool
                try:
                    dsn = self._dsn_for(unit)
                except LookupError as e:
                    raise Failure(str(e), reason="unit_not_configured") from e
                pool = ConnectionPool(
                    dsn, min_size=1, max_size=self._pool_max, timeout=15, name=f"gm-{unit}",
                    kwargs={"options": self._options, "application_name": f"governed-metrics:{unit}"},
                    reset=_reset, check=ConnectionPool.check_connection, max_idle=120, open=True)
                self._pools[unit] = pool
        return pool

    def _run(self, unit: str, work: Callable[[Any], Any]) -> Any:
        import psycopg
        try:
            with self._pool(unit).connection() as conn:
                return work(conn)
        except psycopg.errors.QueryCanceled as e:
            raise StatementTimeout(str(e)) from e

    def scalar(self, unit: str, statement: Statement) -> Any:
        def work(conn):
            with conn.cursor() as cur:
                cur.execute(statement.sql, dict(statement.params))
                ncols = len(cur.description or ())
                return ncols, cur.fetchmany(2)
        ncols, rows = self._run(unit, work)
        if ncols != 1 or len(rows) != 1:
            # A canonical value is one cell. Silently taking the first of many would return a
            # quietly wrong number, which is the one outcome this system exists to prevent.
            raise Failure("A definition did not compile to a single value.", reason="definition_shape")
        return rows[0][0]

    def rows(self, unit: str, statement: Statement, max_rows: int) -> tuple[list[str], list[tuple]]:
        def work(conn):
            with conn.cursor() as cur:
                cur.execute(statement.sql, dict(statement.params))
                return [d.name for d in cur.description or ()], cur.fetchmany(max_rows)
        return self._run(unit, work)

    def adhoc(self, unit: str, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
        """Run an approved ad-hoc statement through a server-side cursor, so only `max_rows`
        rows ever leave the warehouse, however large the full result. No parameters are
        passed, so the driver performs no placeholder interpolation on the text."""
        def work(conn):
            with conn.cursor(name=ADHOC_CURSOR) as cur:
                cur.execute(sql)
                rows = cur.fetchmany(max_rows)
                return [d.name for d in cur.description or ()], rows
        return self._run(unit, work)

    def ping(self, unit: str) -> bool:
        try:
            return self.scalar(unit, Statement("SELECT 1", {})) == 1
        except Exception:
            log.warning("warehouse ping failed for unit=%s", unit, exc_info=True)
            return False

    def close(self) -> None:
        for pool in list(self._pools.values()):
            try:
                pool.close()
            except Exception:
                log.debug("pool close failed", exc_info=True)
        self._pools.clear()
