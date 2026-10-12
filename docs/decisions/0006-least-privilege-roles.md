# 0006 — Per-unit database roles are the root control

**Status:** accepted

## Context

The server reads a warehouse on behalf of callers from several business units. It could hold one broad credential and decide in application code what may be read, or hold a narrow credential per unit.

## Decision

A read-only database role per unit, scoped to that unit's sources. The application guards are a second wall, not the boundary.

## Consequences

- A defect in the guard, the reference parser, the policy enforcer or the tool surface cannot become a cross-unit read. The credential cannot see the other unit's sources at all.
- The blast radius of any future bug in the query path is bounded by configuration rather than by code correctness.
- Guards keep their jobs: refusing early, giving the caller a useful error, and bounding cost.
- The cost is administrative. Roles and grants have to be provisioned per unit and kept current as sources change, which is real work that never finishes.

## Rejected alternatives

**One broad credential with application-level authorisation.** The common arrangement, and indistinguishable from this one until a guard has a defect. Then it reads everything. The failure mode is total and the bug is always in the guard nobody suspected.

**One read-only credential for the whole warehouse.** Prevents writes, not cross-unit reads, which is the disclosure that actually matters here.

**Row-level security in the warehouse alone.** Useful and complementary, but it ties authorisation to warehouse features, spreads policy across two systems, and does not bound which sources can be referenced at all.
