# Glossary

Terms as this documentation uses them. Where a term is contested in the wider industry, the definition here is the narrow one the system actually implements.

---

**Ad-hoc answer.** A result produced from a caller-supplied statement rather than a declared definition. Labelled non-canonical in the response itself, not merely in documentation.

**Allow-list.** An enumeration of what is permitted, with everything else refused. Distinct from a deny-list, which enumerates what is forbidden and permits the remainder. The system uses allow-lists for sources, metrics, dimensions and units.

**Canonical answer.** A result produced from a declared definition, carrying a provenance stamp. The number the organisation means when it names the metric.

**Deep check.** A health check that proves the server can reach the warehouse and load the knowledge layer, as opposed to merely being up.

**Derived metric.** A metric defined as a ratio or difference of other metrics. Each leg is itself a metric, so a derived value cannot disagree with its components.

**Dimension.** A declared way to break a metric down. A breakdown a definition does not declare is unavailable rather than approximated.

**Effective date.** The date from which a governed rule's value applies. Resolving a rule for a past date returns the value that was in force then.

**Fail-closed.** Refusing anything that cannot be classified, rather than allowing it. The opposite, fail-open, treats "not recognised as dangerous" as safe.

**Half-open window.** A period that includes its start and excludes its end. Ensures a record on a boundary belongs to exactly one period, so adjacent periods never double-count it.

**Knowledge layer.** The version-controlled definitions, rules, glossary and reference data that give metrics their meaning.

**Knowledge version.** The identifier of the knowledge layer's state at a point in time. Stamped onto every answer and used as the result cache key.

**Leg.** One component query of a derived or composite metric. Independent legs run concurrently.

**Lexer.** A component that turns statement text into tokens in a single pass. The guard uses one so that it reads a statement the way the warehouse does, rather than inspecting how the statement is spelled.

**Masking chokepoint.** The single place where personal fields are masked on the way out. Single, because masking applied in several places is masking that will eventually be omitted in one of them.

**Maturity signal.** A marker on a value whose window has closed but which still changes as late records arrive.

**MCP.** Model Context Protocol, the protocol AI clients use to call tools on a server. Here it is the transport and tool convention, not a source of authorisation.

**Measure.** The quantity a metric aggregates, named rather than embedded so several metrics can reference it.

**Measure alias.** A mapping that lets units which name the same quantity differently be combined in one answer.

**Metric.** A named quantity with exactly one meaning. Two defensible calculations are two metrics with two names, never one name whose meaning depends on the asker.

**Named filter preset.** A frequently requested narrowing, named once so callers ask for the preset rather than reconstructing the filter.

**Period anchoring.** Which date field a metric is measured against, and how period boundaries are treated.

**Policy.** The per-caller statement of which units, metrics and dimensions may be read, whether ad-hoc querying is permitted, and which sources are available.

**Provenance stamp.** The knowledge version attached to an answer, which makes a number traceable to the definition that produced it.

**Reconciliation.** Writing live grants back into version control so the overlay stays a short-lived buffer rather than becoming the source of truth.

**Shared fragment.** A business rule written once, named, and referenced wherever it applies, instead of copied.

**Truncation flag.** A marker stating that a breakdown was cut at the row cap and is therefore partial.

**Unit.** A business unit. The unit of access control, of database role, and of override for governed rules. Written here as Unit A, Unit B.

**Variant.** A named form of a derived metric, where several forms are defensible. The answer states which variant produced it.
