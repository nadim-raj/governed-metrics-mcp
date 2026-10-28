"""Freshness: a stale source is refused, never rendered as a number (ADR 0010).

Two signals are kept apart on purpose:

* **stale**: the source's newest record is older than its declared `max_age_days`, or the
  source is empty. The feed has stopped; an answer would be a plausible wrong number, so
  none is given. The refusal names the source and the last date it covers.
* **incomplete window**: the source is healthy, but the requested window extends past what
  it has published yet (a daily feed asked about today). The value is served with
  `coverage.complete = false` and the date it covers through, because that gap is the
  source's normal lag, not a fault.

Treating the second as the first would raise a false alarm every day, and people learn to
ignore alarms that cry wolf.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..errors import StaleSource
from ..knowledge.model import Source
from .compiler import Window


@dataclass(frozen=True)
class Coverage:
    source: str
    through: date | None
    age_days: int | None
    max_age_days: int

    @property
    def stale(self) -> bool:
        return self.through is None or (self.age_days or 0) > self.max_age_days


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def assess(source: Source, newest: Any, today: date) -> Coverage:
    through = _as_date(newest)
    age = (today - through).days if through is not None else None
    return Coverage(source.name, through, age, source.freshness.max_age_days)


def refuse_if_stale(coverage: Coverage) -> None:
    if not coverage.stale:
        return
    if coverage.through is None:
        detail = f"Source '{coverage.source}' has no data at all."
    else:
        detail = (f"Source '{coverage.source}' has no data after {coverage.through.isoformat()} "
                  f"({coverage.age_days} days old; at most {coverage.max_age_days} expected).")
    raise StaleSource(detail + " The value is withheld: a stopped feed would read as a false zero.",
                      hint="Report the staleness, not a number. Operators are alerted through the log.",
                      extra={"source": coverage.source,
                             "covers_through": coverage.through.isoformat() if coverage.through else None})


def coverage_block(coverages: Sequence[Coverage], window: Window) -> dict[str, Any] | None:
    """{through, complete} for the sources a value read, or None when none declare freshness."""
    dated = [c.through for c in coverages if c.through is not None]
    if not dated:
        return None
    through = min(dated)
    complete = window.all_time or (window.end - timedelta(days=1)) <= through
    return {"through": through.isoformat(), "complete": complete}


def merge_blocks(blocks: Sequence[dict[str, Any] | None]) -> dict[str, Any] | None:
    present = [b for b in blocks if b]
    if not present:
        return None
    return {"through": min(b["through"] for b in present),
            "complete": all(b["complete"] for b in present)}
