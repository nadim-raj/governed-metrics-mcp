"""Entry point: `governed-metrics` or `python -m governed_metrics`.

Refuses to start on a configuration that would weaken a control (see Settings.problems)
or on knowledge that fails validation. A server that starts is a server whose definitions
and settings passed every check.
"""
from __future__ import annotations

import logging
import sys

from .config import load_settings
from .errors import KnowledgeError

log = logging.getLogger("governed_metrics")


def main() -> int:
    # Logs go to stderr: on the stdio transport, stdout carries the protocol itself.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
    except ValueError as e:
        log.critical("invalid configuration: %s", e)
        return 2
    if problems := settings.problems():
        for p in problems:
            log.critical("refusing to start: %s", p)
        return 2

    from .runtime import Runtime
    from .server import build_server
    from .warehouse import PostgresWarehouse

    warehouse = PostgresWarehouse(settings.dsn_for, statement_timeout_ms=settings.statement_timeout_ms)
    try:
        rt = Runtime(settings, warehouse)
    except KnowledgeError as e:
        for p in e.problems:
            log.critical("knowledge: %s", p)
        return 2
    if not rt.policy.ok:
        log.error("access policy did not load (%s): every request will be denied", rt.policy.error)
    if not settings.http and not settings.local_user:
        log.warning("GM_LOCAL_USER is not set: on stdio the caller has no identity and is denied everything")
    log.info("serving knowledge %s over %s", rt.knowledge.version, settings.transport)
    try:
        build_server(rt).run(transport=settings.transport)
    finally:
        warehouse.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
