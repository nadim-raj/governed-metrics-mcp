# 0009 — Rules are effective-dated and served only when verified

**Status:** accepted

## Context

Governed rules change over time, and people quote the value they remember. The system can serve a rule's current value, or its value as at a date.

A separate problem: rules get drafted before they are confirmed, and a draft in the store looks like a rule.

## Decision

Rules are layered and effective-dated: a group default, then an optional per-unit override, then the latest entry already in force on the date asked about. A rule is served only when marked verified. An unverified value is refused.

## Consequences

- A question about a past period resolves the rule as it stood then, so historical numbers stay correct as rules change.
- The effective date travels with the value, which is usually the caller's next question.
- A draft cannot be mistaken for a rule. The caller gets a refusal naming that no verified value exists.
- The cost is that verification is a human step, so a real rule can sit unservable while somebody confirms it. That gap is visible, which is the point.

## Rejected alternatives

**Serve the current value only.** Makes every historical number wrong the moment a rule changes, and the wrongness is silent.

**Serve unverified values with a warning.** A warning attached to a plausible number gets ignored. A refusal does not.

**Let the caller specify which version they want.** Moves the governance problem to the caller, who will pick the value that matches what they expected.
