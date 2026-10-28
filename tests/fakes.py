"""A fake warehouse: unit tests exercise the compiler, executor, guard and policy without a
database, so a failing test points at logic rather than at an environment."""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from governed_metrics.engine.compiler import Statement

TODAY = date(2026, 10, 27)
_FRESHNESS = re.compile(r"^SELECT MAX\((\w+)\)::date AS value FROM (\w+\.\w+)$")


class FakeWarehouse:
    """Answers scalar, breakdown and ad-hoc calls from injectable functions and records
    every statement it was asked to run."""

    def __init__(self, *, scalar: Callable[[str, Statement], Any] | None = None,
                 rows: Callable[[str, Statement, int], tuple[list[str], list[tuple]]] | None = None,
                 adhoc: Callable[[str, str, int], tuple[list[str], list[tuple]]] | None = None,
                 newest: Callable[[str, str], Any] | None = None):
        self.scalar_fn = scalar or (lambda unit, stmt: 100)
        self.rows_fn = rows or (lambda unit, stmt, n: (["dimension", "value"], [("web", 1), ("app", 2)]))
        self.adhoc_fn = adhoc or (lambda unit, sql, n: (["n"], [(1,)]))
        self.newest_fn = newest or (lambda unit, source: TODAY - timedelta(days=1))
        self.calls: list[tuple[str, str, str]] = []
        self.pings: dict[str, bool] = {}
        self._lock = threading.Lock()

    def _log(self, kind: str, unit: str, sql: str) -> None:
        with self._lock:
            self.calls.append((kind, unit, sql))

    def scalar(self, unit: str, statement: Statement) -> Any:
        if (m := _FRESHNESS.match(statement.sql)):
            self._log("freshness", unit, statement.sql)
            return self.newest_fn(unit, m.group(2))
        self._log("scalar", unit, statement.sql)
        return self.scalar_fn(unit, statement)

    def rows(self, unit: str, statement: Statement, max_rows: int):
        self._log("rows", unit, statement.sql)
        cols, rows = self.rows_fn(unit, statement, max_rows)
        return cols, rows[:max_rows]

    def adhoc(self, unit: str, sql: str, max_rows: int):
        self._log("adhoc", unit, sql)
        cols, rows = self.adhoc_fn(unit, sql, max_rows)
        return cols, rows[:max_rows]

    def ping(self, unit: str) -> bool:
        return self.pings.get(unit, True)

    def close(self) -> None:
        pass

    def statements(self, kind: str = "scalar") -> list[str]:
        return [sql for k, _, sql in self.calls if k == kind]
