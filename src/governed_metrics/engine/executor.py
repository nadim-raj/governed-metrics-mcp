"""Run independent legs concurrently; retry one timed-out statement once (ADR 0011).

Independence is explicit, never inferred. Each leg of a derived metric and each unit of a
composite is a separate job by construction, so a change to a filter's shape cannot quietly
make the engine treat two legs as dependent and serialise them (lessons-learned.md, "A
filter shape that disabled parallel execution"). tests/test_engine.py pins this with a
barrier that only opens if both legs are in flight at once.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from typing import TypeVar

from ..errors import QueryTimeout
from ..warehouse import StatementTimeout

log = logging.getLogger("governed_metrics.executor")
T = TypeVar("T")


def run_parallel(jobs: Sequence[Callable[[], T]], max_workers: int) -> list[T]:
    """Results in submission order. Stops at the first failure instead of waiting for the
    slower siblings, so a failure costs one leg's time rather than all of them."""
    if len(jobs) <= 1:
        return [job() for job in jobs]
    pool = ThreadPoolExecutor(max_workers=max(1, min(len(jobs), max_workers)),
                              thread_name_prefix="gm-leg")
    futures = [pool.submit(job) for job in jobs]
    try:
        done, _ = wait(futures, return_when=FIRST_EXCEPTION)
        for fut in futures:
            if fut in done and fut.exception() is not None:
                raise fut.exception()
        return [fut.result() for fut in futures]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def retry_once(run: Callable[[], T], *, what: str) -> T:
    """One statement, retried exactly once on a timeout. A second timeout is the caller's
    answer: a structural cost (a wide window, a large breakdown) rather than load."""
    try:
        return run()
    except StatementTimeout:
        log.warning("statement timed out, retrying once: %s", what)
    try:
        return run()
    except StatementTimeout as e:
        raise QueryTimeout("The warehouse did not answer within the statement timeout, after one retry.",
                           hint="Narrow the period or the breakdown.") from e
