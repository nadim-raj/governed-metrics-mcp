# 0010 — A stale source is refused, not reported as zero

**Status:** accepted

## Context

A metric reported zero for a period. The query was correct and nothing in the pipeline had failed. An upstream feed had stopped: the warehouse returned no rows, no rows aggregated to nothing, and nothing formatted as zero.

A true zero and a missing feed are indistinguishable once rendered.

## Decision

Freshness of the underlying source is checked before an answer is served. A stale source produces a refusal, as its own outcome, rather than a value.

## Consequences

- The most dangerous answer the system could give is no longer possible. A plausible wrong number gets quoted; a refusal gets investigated.
- The refusal names staleness, so the reader knows the data is late rather than the system broken.
- Freshness expectations have to be declared per source, which is extra knowledge to maintain and occasionally wrong in both directions.
- The cost is availability. A late feed makes a metric unavailable rather than wrong, which is the right trade and still a cost somebody feels.

## Rejected alternatives

**Return zero with a warning in the response.** Tried in spirit elsewhere and it does not hold: a number with a caveat is copied without the caveat.

**Return the last known good value.** Silently answers a different question than the one asked, and the substitution is invisible in the result.

**Monitor freshness separately and alert operators.** Necessary, and not sufficient. The alert and the wrong answer race, and the answer usually wins.
