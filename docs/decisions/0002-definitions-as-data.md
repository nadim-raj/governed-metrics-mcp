# 0002 — Metric definitions as version-controlled data

**Status:** accepted

## Context

A metric's meaning has to live somewhere. The options are code in the server, rows in a database, or declarative files under version control.

## Decision

Definitions are declarative, version-controlled data. A definition names measures, dimensions, filters, presets and period anchoring. The server compiles it into a parameterised read-only query at request time.

## Consequences

- Changes are reviewed, attributed and reversible, with the same tooling the code uses.
- Definitions can be validated before deploy: references resolve, anchoring is declared, no orphaned fragments.
- A definition can be summarised to a reader. A reviewer sees what changed rather than inferring behaviour from code.
- Every answer can carry the knowledge version, making numbers traceable after the fact.
- The cost is expressiveness. Anything the definition language cannot say has to be added to the language, which is slower than writing code for the one case.

## Rejected alternatives

**Definitions as code.** Maximum expressiveness, no reviewability. Code can do anything, so a reviewer cannot bound what a change does, and nothing can be validated ahead of deploy.

**Definitions in database rows, edited through a console.** Easy to change, which is the problem: no diff, no review, no history, and a definition that changed under a running report with nobody able to say when.

**Definitions as saved queries.** The thing that drifts. Query text duplicates business rules across every query that needs them, which is the failure [ADR 0008](0008-shared-fragments.md) exists to prevent.
