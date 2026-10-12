# 0003 — Canonical and ad-hoc answers are different products

**Status:** accepted

## Context

People need both a governed number and the freedom to explore. Serving them from one undifferentiated tool surface is what lets an exploratory figure end up in a board pack.

## Decision

Two distinct paths. Canonical answers come from declared definitions and carry a provenance stamp. Ad-hoc answers come from a guarded, caller-supplied statement and are labelled non-canonical in the response itself. Ad-hoc access is a privilege that must be granted, not a default.

## Consequences

- The label travels. Because it is in the response rather than in documentation, it follows the number into whatever document quotes it.
- Exploration stays inside the system, with guards, a row cap, a timeout and masking, instead of happening somewhere with none of those.
- The two paths can have different controls. Ad-hoc has a per-caller source allow-list; canonical does not need one, because a definition cannot reference a source it was not written against.
- The cost is a surface that is harder to explain. Users must understand why the same question can be answered two ways and why the answers may differ.

## Rejected alternatives

**Canonical answers only.** Clean, and ignored. People who cannot explore do it in a spreadsheet, outside every control, which is worse than a guarded tool.

**One tool with a governed mode flag.** A flag defaults, defaults get forgotten, and the resulting number carries no visible provenance about which mode produced it.

**Ad-hoc enabled for everyone by default.** Makes the highest-risk surface the common path and turns every guard defect into an organisation-wide exposure.
