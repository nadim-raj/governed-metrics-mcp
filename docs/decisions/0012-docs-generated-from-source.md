# 0012 — Generated documentation, with staleness as a test failure

**Status:** accepted

## Context

Documentation that lists metrics, tools and modules goes out of date within weeks. The inventory sections are exactly the parts readers trust most and the parts that drift fastest.

## Decision

Inventory sections are generated from the source. A test fails when the committed documentation does not match what generation produces, and the pipeline refuses to deploy on that failure.

## Consequences

- Stale documentation becomes a build failure rather than a matter of somebody's diligence.
- Prose and inventory are separated cleanly: the reasoning is written by hand, the lists are generated.
- Adding a tool or metric and forgetting the documentation is caught before merge.
- The cost is that generation is now part of the build and has to work. A generator defect blocks deploys.

## Rejected alternatives

**Hand-written documentation with a review checklist.** The checklist is followed until a busy week, and nothing fails when it is not.

**Generate at read time from a live endpoint.** Requires the reader to reach a running server, which a public documentation repository cannot assume.

**Accept drift and date the document.** Honest, and it makes the most useful sections untrustworthy. A reader cannot tell which parts aged.
