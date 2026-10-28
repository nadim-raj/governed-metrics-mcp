"""The audit log: one append-only line per decision, refusals included.

It answers two questions: who could see this number, and is somebody repeatedly being
refused. Both are answerable from decisions alone, so the log records decisions and nothing
else. It never holds query text or personal data: a log of what people asked, stored next to
the system that answered, would be a second copy of the sensitive material with none of its
controls. Fields are allow-listed, so a caller cannot widen that by accident.

The file is owner-only (0600) in an owner-only directory, rotated monthly to
`<stem>-YYYY-MM<ext>`, and kept outside the deployed tree.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("governed_metrics.audit")

FIELDS = ("ts", "caller", "tool", "unit", "metric", "knowledge_version", "outcome", "reason",
          "duration_ms", "rows_band", "cached", "subject")
# Some fields echo caller input (an unknown unit or metric name). Capped, so a caller cannot
# use the log as storage or bloat it.
MAX_FIELD_CHARS = 200


def rows_band(n: int | None) -> str | None:
    """Result size as a band, not a count: enough to spot a dump, too coarse to fingerprint."""
    if n is None:
        return None
    for limit, band in ((0, "0"), (10, "1-10"), (100, "11-100"), (1000, "101-1000")):
        if n <= limit:
            return band
    return ">1000"


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.write_failures = 0

    def _rotate(self, now: datetime) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return
        last = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        if (last.year, last.month) == (now.year, now.month):
            return
        stem, ext = os.path.splitext(self.path)
        dest = f"{stem}-{last:%Y-%m}{ext}"
        if os.path.exists(dest):
            dest = f"{stem}-{last:%Y-%m}-{int(st.st_mtime)}{ext}"
        os.rename(self.path, dest)

    def record(self, **fields: Any) -> None:
        """Append one decision. Never raises: a failing log must not fail the request, but
        every failure is counted (surfaced on /readyz) and logged to the process log."""
        if not self.path:
            return
        now = datetime.now(timezone.utc)
        entry = {k: (v[:MAX_FIELD_CHARS] if isinstance(v, str) else v)
                 for k in FIELDS if (v := fields.get(k)) is not None}
        entry.setdefault("ts", now.isoformat(timespec="milliseconds"))
        try:
            line = json.dumps(entry, sort_keys=True, default=str)
            with self._lock:
                os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
                self._rotate(now)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except (OSError, TypeError, ValueError):
            self.write_failures += 1
            log.error("audit write failed (path=%s)", self.path, exc_info=True)

    def files(self) -> list[str]:
        stem, ext = os.path.splitext(self.path)
        archives = sorted(glob.glob(f"{glob.escape(stem)}-[0-9][0-9][0-9][0-9]-[0-9][0-9]*{ext}"))
        return archives + ([self.path] if os.path.exists(self.path) else [])

    def read(self, since: datetime | None = None) -> Iterator[dict[str, Any]]:
        for path in self.files():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since is not None and str(entry.get("ts", "")) < since.isoformat():
                        continue
                    yield entry
