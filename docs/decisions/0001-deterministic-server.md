# 0001 — A deterministic server with no model inside it

**Status:** accepted

## Context

The server answers metric questions asked by AI assistants. The obvious move is to put a language model in the request path so that a loosely worded question can be mapped onto a metric, a period and a breakdown.

## Decision

No language model runs inside the server. The same call against the same definitions returns the same number every time. Composite behaviour ships as fixed tools with defined semantics. Open-ended interpretation stays on the client.

## Consequences

- A number can be reproduced. Given the provenance stamp, anyone can recompute the value and get the same answer, which is what makes it canonical.
- The server is auditable. Its behaviour is determined by definitions and policy, both version-controlled, rather than by a model's output on the day.
- Clients keep their flexibility. An assistant may call several tools and reason over the results; the results themselves do not move.
- The cost is friction. A vaguely worded question gets a refusal or the wrong metric rather than a helpful guess, and the client has to be specific.

## Rejected alternatives

**A model that maps questions onto metrics inside the server.** Would make the same question return different numbers on different days. That is precisely the failure the system exists to remove.

**A model that only picks the metric, with deterministic execution after.** Still non-deterministic where it matters, because picking the wrong metric produces a confidently wrong number rather than an error.

**A model that validates its own output.** Adds cost and latency without adding a guarantee, since nothing checks the checker.
