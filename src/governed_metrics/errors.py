"""One error vocabulary for every tool: invalid request, access denied, timeout, failure.

Separating a refusal from a failure is what lets the audit log tell a caller who needs a
grant apart from a component that is broken (docs/request-lifecycle.md, "One error
vocabulary"). Every outcome a tool can produce maps to exactly one of these four kinds;
`reason` is a stable machine word that narrows it (e.g. `stale_source`, `unit_not_permitted`),
and `detail` is safe to show the caller.
"""
from __future__ import annotations

from typing import Any, ClassVar

# kind -> what the caller should do next. Exported so docs/reference.md can be generated
# from it rather than restated by hand.
ERROR_KINDS: dict[str, str] = {
    "invalid_request": "Fix the request: it was malformed, or the guard could not classify it.",
    "access_denied": "Request a grant: the caller is not entitled to what was asked.",
    "timeout": "Narrow the period or the breakdown: the statement exceeded its limit after one retry.",
    "failure": "Nothing: something inside the system broke, or a source is stale. Operators are alerted.",
}


class GovernedError(Exception):
    """Base for every outcome that is not a successful answer."""

    kind: ClassVar[str] = "failure"
    default_reason: ClassVar[str] = "internal"

    def __init__(self, detail: str, *, reason: str | None = None, hint: str | None = None,
                 extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.reason = reason or self.default_reason
        self.hint = hint
        self.extra = dict(extra or {})

    def envelope(self) -> dict[str, Any]:
        """The response body for this outcome. `kind: error` lets the masking chokepoint
        recognise the shape; `error` is the kind the caller branches on."""
        out: dict[str, Any] = {"kind": "error", "error": self.kind, "reason": self.reason,
                               "detail": self.detail}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


class InvalidRequest(GovernedError):
    kind = "invalid_request"
    default_reason = "invalid_request"


class AccessDenied(GovernedError):
    kind = "access_denied"
    default_reason = "not_permitted"


class QueryTimeout(GovernedError):
    kind = "timeout"
    default_reason = "statement_timeout"


class Failure(GovernedError):
    kind = "failure"
    default_reason = "internal"


class StaleSource(Failure):
    """A source is behind its declared freshness. Refused rather than rendered, because an
    empty result from a stopped feed formats exactly like a true zero (ADR 0010)."""

    default_reason = "stale_source"


class KnowledgeError(Exception):
    """The knowledge layer failed validation. Raised at load time, never at request time:
    a server with invalid definitions refuses to start, and a reload keeps the old ones."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        head = "; ".join(self.problems[:5])
        more = f" (+{len(self.problems) - 5} more)" if len(self.problems) > 5 else ""
        super().__init__(f"knowledge validation failed: {head}{more}")
