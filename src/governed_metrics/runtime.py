"""The runtime: the knowledge snapshot, the policy, the engine and the audit log, together.

Knowledge and policy are swapped whole, never edited in place. A request reads each once at
its start and uses that snapshot throughout, so a reload in the middle of a request cannot
give it half of an old definition and half of a new one.

The policy is rebuilt when the live overlay changes (one `stat` per request, see
overlay.signature), so an admin's grant takes effect on the next call, not the next deploy.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from . import overlay
from .audit import AuditLog
from .config import Settings
from .engine.engine import MetricEngine
from .knowledge.loader import load_knowledge
from .knowledge.model import Knowledge
from .policy import AccessPolicy, load_policy
from .warehouse import Warehouse

log = logging.getLogger("governed_metrics.runtime")


class RateLimiter:
    """Fixed window per key. Applied to the ad-hoc path only: an arbitrary statement on a
    shared warehouse is the cost vector; canonical calls are cheap and cached."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        t = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._windows.get(key, (t, 0))
            if t - start >= 60:
                start, count = t, 0
            self._windows[key] = (start, count + 1)
            return count + 1 <= self.per_minute


def jsonify(obj: Any) -> Any:
    """JSON-native copy of a result. Decimals leave as numbers when that loses nothing, as
    strings otherwise, so no precision is ever dropped silently."""
    if isinstance(obj, dict):
        return {str(k): jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonify(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            return str(obj)
        if obj == obj.to_integral_value():
            return int(obj)
        as_float = float(obj)
        return as_float if Decimal(repr(as_float)) == obj else str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(obj))} bytes>"
    return str(obj)


class Runtime:
    def __init__(self, settings: Settings, warehouse: Warehouse, *, knowledge: Knowledge | None = None,
                 today: Callable[[], date] | None = None):
        self.settings = settings
        self.warehouse = warehouse
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self.engine = MetricEngine(warehouse, cache_ttl_s=settings.cache_ttl_s,
                                   freshness_ttl_s=settings.freshness_ttl_s,
                                   breakdown_row_cap=settings.breakdown_row_cap,
                                   max_parallel_legs=settings.max_parallel_legs, today=self.today)
        self.audit = AuditLog(settings.audit_path)
        self.adhoc_rate = RateLimiter(settings.adhoc_rate_per_minute)
        self._lock = threading.RLock()
        self.knowledge = knowledge if knowledge is not None else load_knowledge(settings.knowledge_path)
        self._overlay_sig: Any = None
        self.policy = self._build_policy()

    def _build_policy(self) -> AccessPolicy:
        path = self.settings.overlay_path
        policy = load_policy(self.settings.policy_path, self.knowledge, overlay_path=path)
        if policy.reconcile:
            # These users are governed by version control now; retire their overlay history so
            # a later removal from version control cannot resurrect an old overlay grant.
            overlay.mark_reconciled(path, list(policy.reconcile))
            log.info("overlay reconciled for %d user(s)", len(policy.reconcile))
        self._overlay_sig = overlay.signature(path)
        return policy

    def current_policy(self) -> AccessPolicy:
        if overlay.signature(self.settings.overlay_path) != self._overlay_sig:
            with self._lock:
                if overlay.signature(self.settings.overlay_path) != self._overlay_sig:
                    self.policy = self._build_policy()
        return self.policy

    def refresh_policy(self) -> AccessPolicy:
        with self._lock:
            self.policy = self._build_policy()
            return self.policy

    def reload(self) -> dict[str, Any]:
        """Re-read knowledge and policy. Raises KnowledgeError on invalid knowledge, leaving
        the current snapshot serving: a bad merge degrades to "still the old definitions"."""
        with self._lock:
            kb = load_knowledge(self.settings.knowledge_path)
            self.knowledge = kb
            self.policy = self._build_policy()
            self.engine.clear_cache()
        log.info("knowledge reloaded: %s", kb.version)
        return {"kind": "reload", "knowledge_version": kb.version, "policy_ok": self.policy.ok,
                "metrics": len(kb.metrics)}
