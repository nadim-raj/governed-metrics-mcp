# Roadmap

What is planned, and what was considered and deliberately left out. The second list is the more useful one: a roadmap that only grows says nothing about judgement.

---

## Planned

**Minimum group sizes on sensitive breakdowns.** Masking reduces precision but does not prevent inference. A caller who can group and count can sometimes narrow a masked value down, particularly in a small group. Breakdowns on sensitive dimensions should refuse below a floor rather than return a group of one.

**Definition ownership surfaced in answers.** Every definition has an owner. The answer does not currently say who it is, so a caller who doubts a number has to ask around. Naming the owner in the provenance stamp turns a question about a number into a conversation with the person responsible for it.

**Drift detection on upstream labels.** Filters now fail when they match nothing, which catches a renamed label once it is total. A partial split, where some records move to a new label and others stay, still passes. Comparing the distinct values a filter sees against the previous run would catch the partial case.

**Query plan review for new definitions.** A definition can be correct and expensive. Reviewing the plan before a definition goes live would move the discovery from a production timeout to a review comment.

**Richer composite semantics.** The cross-unit tool returns each unit, its share and the total. Comparisons across time and across units at once are currently two calls and a client-side join, which is exactly the kind of arithmetic that should not happen on the client.

## Deliberately deferred

**Write access of any kind.** The system is a read path and is expected to stay one. A governed write path is a different system with a different threat model, and bolting it onto this one would compromise the property that makes this one safe to expose.

**A model inside the server.** Raised repeatedly, because a model could map a loosely worded question onto a metric. It would also make the same question return different numbers on different days, which is the failure the system exists to remove. Interpretation belongs on the client; the server stays deterministic. See [ADR 0001](decisions/0001-deterministic-server.md).

**Caller-supplied calculations in canonical answers.** A caller may explore with the ad-hoc tool and receive an answer labelled non-canonical. Letting a caller define a calculation and have the result presented as canonical would make the label meaningless.

**Self-service definition authoring.** Definitions are small and declarative, so a form could write one. The constraint is not the writing, it is the review: a definition that is valid and wrong is the most expensive artefact in the system. Authoring stays in version control where review is the default.

**Caching across knowledge versions.** The result cache is keyed on the knowledge version, so a definition change invalidates everything derived from it. A cleverer key would retain more, and would also eventually serve a number computed under a definition that no longer exists.

**A second transport.** Streamable HTTP covers every client in use. A second transport would double the surface that has to be authorised and audited for no current benefit.
