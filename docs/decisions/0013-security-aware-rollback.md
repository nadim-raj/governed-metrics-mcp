# 0013 — Rollback refuses to cross a security fix

**Status:** accepted

## Context

A failed deploy triggers automatic rollback to the previous version. If the deploy being rolled back contained a security fix, rolling back reinstates the vulnerability it fixed, automatically and quietly, at the moment everyone's attention is on the outage.

## Decision

Rollback checks whether reverting would cross a security fix. If it would, the pipeline stops the service and pages an operator instead of rolling back.

## Consequences

- The system never silently reinstates a known vulnerability. The worst automatic outcome is an outage, not an exposure.
- An outage is loud, bounded and gets fixed within the hour. A quiet exposure is none of those.
- The fix for such a failure goes forward: deploy a corrected version rather than reaching for the previous one.
- The cost is a deliberate, operator-visible outage in a case where an automatic recovery existed. That is the intended trade and it has to be understood before it happens, not during.

## Rejected alternatives

**Always roll back.** The default, and it converts a bad deploy into a security regression precisely when nobody is watching for one.

**Roll back and raise a ticket.** The vulnerability is live while the ticket waits, and the ticket competes with the incident for attention.

**Never roll back automatically.** Makes every failed deploy a manual incident, which lengthens ordinary outages for no security benefit.
