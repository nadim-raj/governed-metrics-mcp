# 0011 — One retry per timed-out statement

**Status:** accepted

## Context

Statements occasionally time out for transient reasons: contention, a cold cache, a brief spike elsewhere in the warehouse. They also time out for structural reasons: the period is too wide or the breakdown too large.

## Decision

A timed-out statement is retried exactly once. A second timeout is returned to the caller as a timeout. Retries apply per statement, so one slow leg of a derived metric does not cause the whole request to be reissued.

## Consequences

- Transient timeouts disappear from the caller's experience without hiding structural ones.
- Cost is bounded. A pathological request costs at most twice its limit, not an escalating series of attempts.
- Retrying per statement rather than per request means the legs that already succeeded are not recomputed.
- The cost is a worst-case latency of roughly double the timeout before the caller learns the request will not succeed.

## Rejected alternatives

**No retry.** Exposes every transient blip to the caller, who retries by hand anyway, less efficiently.

**Retry with backoff until a deadline.** Turns an expensive request into several expensive requests, which is how a struggling warehouse is pushed over.

**Retry the whole request.** Discards completed legs and multiplies the load by the number of legs.
