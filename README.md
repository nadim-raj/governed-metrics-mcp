# Governed Metrics MCP

One canonical, governed answer for every business metric, served to AI assistants over the Model Context Protocol.

This repository is documentation. It explains the problem the system solves, how it is built, and the decisions that shaped it. The running server and the metric definitions it serves are private, and nothing here is derived from them. See [Why the implementation stays private](#why-the-implementation-stays-private).

---

## The problem

Ask four people for the same number and you get four numbers. Each wrote their own query, each made a defensible choice about which records to include and which date boundary to use, and none of the choices are visible in the result. The meeting that follows argues about whose number is right instead of what to do about it.

Hand-written queries also drift. When an upstream label is renamed or a category is split, a query that filtered on the old value keeps running and keeps returning a smaller number. Nothing fails. The number is simply wrong, and it stays wrong until somebody notices it does not match a different report.

Business rules have the same problem in slower motion. A rule changes, the change is announced once, and people go on quoting the value they remember. Months later two teams are both confident and both out of date.

Giving an AI assistant direct access to the warehouse makes all of this worse rather than better. An assistant with broad credentials can read personal data it was never meant to see, read another business unit's data because nothing stopped it, or run an unbounded scan that takes the warehouse down. Prompt text arriving from a document or a ticket can steer it, so the question it runs is not always the question a person asked.

Two failures make the rest harder to catch. A stale upstream feed usually returns an empty result rather than an error, and an empty result formats as zero, which looks like a real answer. And when a number does reach a leadership deck, nobody can say afterwards which definition produced it.

## The approach

Metric definitions are version-controlled data rather than code. A definition names its measures, dimensions, filters and date anchoring, and the server compiles it into a parameterised read-only query at request time. Changing a definition is a reviewed change with a history, and every answer carries a stamp saying which version of the knowledge produced it.

No language model runs inside the server. The same call against the same definitions returns the same number every time. Composite behaviour ships as fixed tools rather than as a model deciding what to do, which keeps the server auditable and leaves open-ended analysis on the client side where it belongs.

Access is denied by default. A per-user policy governs which units, metrics and dimensions a caller can reach, whether they may query ad hoc at all, and which sources they may read. The application enforces this, and least-privilege database roles enforce it again underneath.

## Architecture at a glance

```mermaid
flowchart LR
  subgraph clients [AI clients]
    A1[Chat assistant]
    A2[Coding agent]
  end

  EDGE[Reverse proxy<br/>TLS termination]
  A1 --> EDGE
  A2 --> EDGE
  EDGE --> SRV

  subgraph server [MCP server, deterministic by design]
    SRV[Tool surface<br/>no model runs here]
    POL[Access policy<br/>deny by default]
    ENG[Metric engine<br/>compiles definitions]
    SRV --> POL
    POL --> ENG
  end

  KNOW[(Knowledge layer<br/>definitions, rules, glossary<br/>version-controlled)]
  ENG --> KNOW
  ENG --> WH[(Data warehouse<br/>read-only, per-unit roles)]
  SRV --> LOG[[Usage log<br/>append-only]]
  ADMIN[Admin console] --> POL
```

How a question travels from an assistant to the warehouse, and what stands in its way at each step.

The server listens only on localhost; the proxy in front of it terminates TLS and carries single sign-on. Clients connect over streamable HTTP. Every request passes the access policy before the engine sees it, and every decision lands in the usage log whether it succeeded or was refused.

## Decisions that shaped it

The full set is in [docs/decisions/](docs/decisions/). These are the eight that most changed the result.

**No language model inside the server.** A model in the request path would make the same question return different numbers on different days, which is the exact failure the system exists to remove. Determinism is the product. ([ADR 0001](docs/decisions/0001-deterministic-server.md))

**Definitions as version-controlled data, not code.** A definition that is data can be validated, diffed, reviewed and stamped into provenance. A definition expressed as code can do anything, and its behaviour cannot be summarised to a reader. ([ADR 0002](docs/decisions/0002-definitions-as-data.md))

**Canonical and ad-hoc answers are different products.** Both are useful; conflating them is what lets an exploratory number end up in a board pack. Ad-hoc answers are labelled non-canonical in the response itself, not merely in documentation. ([ADR 0003](docs/decisions/0003-canonical-versus-ad-hoc.md))

**The ad-hoc guard tokenises rather than pattern-matches.** Chained regular expressions were bypassable. A comment, a nested sub-select or unusual whitespace changes how a statement is spelled without changing what it does, so the guard reads it the way the warehouse reads it: a real single-pass lexer. ([ADR 0004](docs/decisions/0004-lexer-not-regular-expressions.md))

**Anything the parser cannot classify is refused.** A guard that guesses when it is unsure is not a guard. Fail-closed produced some false refusals of legitimate queries, which were fixed by teaching the parser the shape it had not seen, never by loosening the default. ([ADR 0005](docs/decisions/0005-fail-closed-parsing.md))

**Per-unit database roles are the real control.** The application guards are a second wall, not the first. If the credential the server holds cannot read a source, no bug in the query path can read it either. ([ADR 0006](docs/decisions/0006-least-privilege-roles.md))

**One business rule is written once and referenced everywhere.** Copies drift. Two definitions that embedded the same rule separately disagreed within months, and neither owner knew. Shared named fragments removed the class of problem rather than the instance. ([ADR 0008](docs/decisions/0008-shared-fragments.md))

**A stale feed must raise an alarm, never return zero.** An empty result is indistinguishable from a true zero once it is formatted, so freshness is checked and a stale answer is refused rather than rendered. ([ADR 0010](docs/decisions/0010-freshness-over-silent-zeros.md))

## What is in this repository

```
README.md                     the story on one screen
docs/
  architecture.md             context, container and component views, trust boundaries
  request-lifecycle.md        sequence diagrams for five representative requests
  knowledge-model.md          the concepts behind the definition model
  security.md                 threat model, controls, residual risks
  access-model.md             policy concepts, grant and reconcile flow
  operations.md               deploy pipeline, rollback rules, incident playbook
  quality.md                  testing strategy, validators, review practice
  lessons-learned.md          what broke, why, and the guard that prevents it
  roadmap.md                  what is next and what was deliberately deferred
  glossary.md                 terms used throughout
  decisions/                  numbered decision records
LICENSE                       Creative Commons Attribution 4.0
```

There are no source folders and no placeholder code files. That is deliberate, not unfinished.

## Why the implementation stays private

The server is built for one organisation, and its metric definitions encode that organisation's rules. Publishing the definitions would publish the business; publishing the server without them would be a shell that demonstrates nothing. What transfers between organisations is the architecture and the reasoning, so that is what is here.

The engineering problems were general even where the data was not: a guard that disagreed with the parser underneath it, a filter shape that quietly disabled parallel execution, a fail-closed fix that broke valid queries, a deploy that crash-looped on a runtime version mismatch. Those are written up in [lessons-learned.md](docs/lessons-learned.md) in the form most useful to someone building a similar system.

## License

Documentation in this repository is licensed under [Creative Commons Attribution 4.0](LICENSE).
