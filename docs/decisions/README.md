# Decision records

Each record states the context, the decision, its consequences and the alternatives rejected. They are numbered in the order they were taken and are not rewritten when a later decision supersedes them.

| Record | Decision |
|---|---|
| [0001](0001-deterministic-server.md) | A deterministic server with no model inside it |
| [0002](0002-definitions-as-data.md) | Metric definitions as version-controlled data |
| [0003](0003-canonical-versus-ad-hoc.md) | Canonical and ad-hoc answers are different products |
| [0004](0004-lexer-not-regular-expressions.md) | A lexer-based guard instead of regular expressions |
| [0005](0005-fail-closed-parsing.md) | Unclassifiable input is refused |
| [0006](0006-least-privilege-roles.md) | Per-unit database roles are the root control |
| [0007](0007-single-masking-chokepoint.md) | One masking chokepoint |
| [0008](0008-shared-fragments.md) | Shared fragments instead of copied rules |
| [0009](0009-verified-effective-dated-rules.md) | Rules are effective-dated and served only when verified |
| [0010](0010-freshness-over-silent-zeros.md) | A stale source is refused, not reported as zero |
| [0011](0011-per-statement-retry.md) | One retry per timed-out statement |
| [0012](0012-docs-generated-from-source.md) | Generated documentation, with staleness as a test failure |
| [0013](0013-security-aware-rollback.md) | Rollback refuses to cross a security fix |
| [0014](0014-live-access-overlay.md) | A live grant overlay, reconciled into version control |

Three of these are load-bearing. [0006](0006-least-privilege-roles.md) is the control that holds when the others fail. [0004](0004-lexer-not-regular-expressions.md) and [0005](0005-fail-closed-parsing.md) are the pair that makes the ad-hoc surface defensible at all.
