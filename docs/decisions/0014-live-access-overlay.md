# 0014 — A live grant overlay, reconciled into version control

**Status:** accepted

## Context

Policy belongs in version control, where it is reviewed and attributable. But access is needed faster than a deploy cycle: somebody joins a team on Monday and needs a number that morning.

## Decision

Administrators grant access through a live overlay that takes effect immediately. Grants are reconciled back into version control, with an author and a date, and reviewed like any other change. Version control remains the source of truth.

## Consequences

- Access requests are satisfied in minutes rather than at the next deploy, which is what stops people sharing credentials or exports.
- Every grant still ends up reviewable, so the audit trail does not pay for the speed.
- The overlay stays a short-lived buffer. Without reconciliation it would accumulate grants nobody remembers making, and the version-controlled policy would describe a system that no longer exists.
- Revocation must apply to both places at once. A revocation that cleared only the overlay would be reinstated at the next deploy.
- The cost is two places where access lives, and a reconciliation step that has to keep working.

## Rejected alternatives

**Version control only.** Correct and too slow. People route around it, usually by sharing a credential or sending a spreadsheet.

**Overlay only.** Fast, with no history, no review and no author. Within a year nobody can explain why a given caller has the access they have.

**Time-expiring grants with no reconciliation.** Bounds the mess instead of recording it, and expiry removes access from someone who still needs it, usually mid-task.
