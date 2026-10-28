"""The live grant overlay: access granted now, reconciled into version control later (ADR 0014).

An append-only JSON-lines file of events, kept outside the deployed tree so a deploy cannot
erase it:

    {"action": "grant",  "user": ..., "units": [...], "by": ..., "reason": ..., "at": ...}
    {"action": "revoke", "user": ..., "by": ..., "reason": ..., "at": ...}
    {"action": "reconciled", "user": ..., "at": ...}

Semantics, enforced by policy.load_policy:

* a grant applies only to a user the version-controlled policy does not list (it wins);
  it grants units, never ad-hoc access, unmasked access or admin rights. Anything wider
  goes through review;
* a revoke denies the user outright, overriding the version-controlled policy too, and
  stays in force until that policy no longer lists them. A revocation that cleared only
  the overlay would be reinstated by the next deploy;
* `reconciled` retires a user's overlay history once version control governs them.

Writes are durable (fsync) and the file is owner-only; a grant must persist, so callers
check the return value.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("governed_metrics.overlay")
_lock = threading.Lock()
ACTIONS = frozenset({"grant", "revoke", "reconciled"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(path: str, event: dict[str, Any]) -> bool:
    if not path:
        log.error("overlay path is empty; cannot persist %s", event.get("action"))
        return False
    if event.get("action") not in ACTIONS or not str(event.get("user", "")).strip():
        log.error("refusing malformed overlay event")
        return False
    entry = {**event, "user": str(event["user"]).strip().lower()}
    entry.setdefault("at", _now())
    try:
        line = json.dumps(entry, sort_keys=True)
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        with _lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(path, 0o600)
        return True
    except (OSError, TypeError, ValueError):
        log.exception("failed to append to overlay %s", path)
        return False


def grant(path: str, user: str, units: list[str], *, by: str, reason: str) -> bool:
    return append(path, {"action": "grant", "user": user, "units": sorted(set(units)), "by": by,
                         "reason": reason})


def revoke(path: str, user: str, *, by: str, reason: str) -> bool:
    return append(path, {"action": "revoke", "user": user, "by": by, "reason": reason})


def mark_reconciled(path: str, users: list[str]) -> None:
    for user in users:
        append(path, {"action": "reconciled", "user": user, "by": "server"})


def read_events(path: str) -> list[dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.error("overlay %s line %d is not JSON; ignoring it", path, n)
                continue
            if isinstance(event, dict) and event.get("action") in ACTIONS and event.get("user"):
                out.append(event)
    return out


def effective(path: str) -> dict[str, dict[str, Any]]:
    """user -> their latest grant or revoke; a reconciled event removes the user."""
    out: dict[str, dict[str, Any]] = {}
    for event in read_events(path):
        user = str(event["user"]).strip().lower()
        if event["action"] == "reconciled":
            out.pop(user, None)
        else:
            out[user] = event
    return out


def signature(path: str) -> tuple[int, int] | None:
    """Cheap change detector, so the policy is rebuilt only when the overlay changed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
