"""A small thread-safe TTL + LRU cache for canonical results.

A canonical value is a pure function of its arguments at a knowledge version, so callers of
this cache always put the knowledge version in the key: a definition change can never be
served a number computed under the old definition (roadmap.md, "Caching across knowledge
versions" is deliberately deferred). The TTL only bounds how stale live-ish data may be.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any


class TTLCache:
    def __init__(self, ttl_s: float, maxsize: int = 2000):
        self._ttl = ttl_s
        self._max = maxsize
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: Hashable, now: float | None = None) -> Any | None:
        if not self.enabled:
            return None
        t = time.monotonic() if now is None else now
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored, value = item
            if t - stored > self._ttl:
                del self._items[key]
                return None
            self._items.move_to_end(key)
        return copy.deepcopy(value)

    def set(self, key: Hashable, value: Any, now: float | None = None) -> None:
        if not self.enabled:
            return
        t = time.monotonic() if now is None else now
        with self._lock:
            self._items[key] = (t, copy.deepcopy(value))
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
