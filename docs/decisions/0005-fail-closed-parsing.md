# 0005 — Unclassifiable input is refused

**Status:** accepted

## Context

The guard's reference parser classifies every source a statement reads, so each can be checked against the caller's allow-list. Some statements produce references the parser cannot classify.

## Decision

Anything the parser cannot classify is refused. A reference that cannot be resolved to a known source is treated as not permitted, never as probably fine.

## Consequences

- An unknown shape cannot smuggle a read past the allow-list, which is the only property that makes the allow-list meaningful.
- The guard's behaviour is predictable: the set of permitted statements is exactly the set it understands.
- Legitimate queries were refused when this was introduced, because the parser's coverage was incomplete. Each shape was added with a test. The default was never loosened.
- The cost is ongoing work. Every new legitimate shape is a small change plus a test, and until it is made the caller is blocked.

## Rejected alternatives

**Allow what is not recognised as dangerous.** Fail-open. Treats the parser's ignorance as evidence of safety, which inverts the guarantee.

**Log the unclassifiable reference and allow it.** A log nobody reads in time, and an exposure that already happened.

**Fall back to a looser parser when the strict one fails.** Means the looser parser decides every interesting case, so the strict one is decoration.
