# 0008 — Shared fragments instead of copied rules

**Status:** accepted

## Context

Several metrics apply the same business rule. The direct approach writes the rule into each definition that needs it.

Two metrics that had embedded the same rule separately disagreed. Both owners were reading their own definition correctly. The rule had changed and one copy had been updated; nothing connected the two, so nothing could detect the divergence.

## Decision

A business rule is written once as a named fragment and referenced wherever it applies. Validators reject a definition that inlines a rule for which a fragment exists.

## Consequences

- A rule change takes effect everywhere at once, which is the behaviour people already assume they have.
- Divergence becomes impossible rather than detectable, which is a stronger guarantee than any amount of cross-checking.
- The dependency is visible: it is possible to list which metrics a fragment affects before changing it.
- The cost is coupling. A fragment change touches every metric that references it, so the review has to consider all of them.

## Rejected alternatives

**Copy the rule and reconcile periodically.** Reconciliation is a task somebody has to remember, and it detects drift after it has already produced wrong numbers.

**Copy the rule and test that copies agree.** Needs a test per pair, which nobody writes for a pair that does not exist yet. The next copy is untested by construction.

**One metric per rule, with others deriving from it.** Works where the rule is a metric. Most rules are filters or thresholds and are not quantities, so this does not generalise.
