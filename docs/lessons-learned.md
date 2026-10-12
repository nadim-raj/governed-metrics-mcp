# Lessons learned

Nine problems that cost real time, written as symptom, root-cause class, fix and the guard that stops the class recurring. They are kept general on purpose: each is a thing that happens to any team building a governed read path, and none depends on the business underneath.

The pattern across all nine is the same. In every case something returned an answer instead of raising an alarm.

---

## Label drift

**Symptom.** A metric declined gradually over several weeks. Nothing failed and no alert fired. The decline was real in the number and absent in the business.

**Root-cause class.** A filter written against an upstream label that was renamed. Records that should have matched stopped matching, and a smaller result is still a valid result.

**Fix.** The filter was rewritten against a stable identifier rather than a display label, and the affected periods were recomputed.

**Guard.** Definition tests assert that every filter resolves to a non-empty set of matching values. A filter that matches nothing now fails rather than quietly narrowing a metric.

## Copied rules drifting apart

**Symptom.** Two metrics that embedded the same business rule disagreed. Both owners were confident, and both were reading their own definition correctly.

**Root-cause class.** The rule had been copied into each definition at different times. When it changed, one copy was updated. Nothing connected the two, so nothing could detect the divergence.

**Fix.** The rule became a shared named fragment, written once and referenced from both definitions.

**Guard.** Validators reject a definition that inlines a rule for which a shared fragment exists. The class is removed rather than the instance. See [ADR 0008](decisions/0008-shared-fragments.md).

## A lexer that disagreed with the warehouse

**Symptom.** A statement the guard accepted did something other than what the guard believed it did.

**Root-cause class.** The guard was built from chained regular expressions, which inspect how a statement is spelled. Comments, nested sub-selects, unusual whitespace and case all change the spelling without changing the behaviour, so the guard and the warehouse were reading two different statements.

**Fix.** A real single-pass lexer that tokenises the way the warehouse does, with classification decided over tokens rather than text.

**Guard.** Regression tests pin the agreement between the lexer and the warehouse, and the warehouse upgrade checklist includes rerunning them. See [ADR 0004](decisions/0004-lexer-not-regular-expressions.md).

## A filter shape that disabled parallel execution

**Symptom.** A derived metric became several times slower. The warehouse looked healthy and each leg was individually fast.

**Root-cause class.** A change to a filter shape made the engine treat two legs as dependent, so they ran in sequence. Nothing was wrong, only slow, and slow does not fail.

**Fix.** Independence was determined explicitly rather than inferred from the filter's structure.

**Guard.** A test asserts that independent legs are scheduled concurrently, so a future change that serialises them fails rather than merely costing time. The incident playbook also puts this ahead of warehouse investigation when latency rises.

## A fail-closed fix that broke legitimate queries

**Symptom.** After the reference parser was made fail-closed, valid ad-hoc queries began to be refused.

**Root-cause class.** The parser met legitimate statement shapes it could not classify. Fail-closed behaved correctly; the parser's coverage was incomplete.

**Fix.** The parser was taught each shape, with a test per shape.

**Guard.** The default was never loosened. Every refusal of a legitimate shape is treated as a missing capability in the parser, not as evidence that refusing was too strict. See [ADR 0005](decisions/0005-fail-closed-parsing.md).

## A stale feed reading as zero

**Symptom.** A metric reported zero for a period. The query was correct and the pipeline had not failed.

**Root-cause class.** An upstream feed had stopped. The warehouse returned no rows, no rows aggregated to nothing, and nothing formatted as zero. A true zero and a missing feed are indistinguishable once rendered.

**Fix.** Freshness of the underlying source is checked before an answer is served, and a stale answer is refused.

**Guard.** The refusal is a distinct outcome rather than a zero with a warning, because a warning attached to a plausible number gets ignored. See [ADR 0010](decisions/0010-freshness-over-silent-zeros.md).

## A deploy that crash-looped on a runtime version mismatch

**Symptom.** A deploy came up and immediately died, repeatedly.

**Root-cause class.** The artefact was built against a different runtime version than the server runs. The build succeeded, so the pipeline believed the change was good.

**Fix.** The pipeline compiles on the target runtime version, so a mismatch fails the build rather than the service.

**Guard.** Health polling after deploy with automatic rollback, bounded by the rule that rollback will not cross a security fix. See [ADR 0013](decisions/0013-security-aware-rollback.md).

## A truncated breakdown presented as complete

**Symptom.** A breakdown's parts did not add up to the metric's total, and the gap was not obvious.

**Root-cause class.** The result had been cut at a row limit. The limit protected the warehouse and said nothing in the response, so a partial breakdown looked like a complete one.

**Fix.** Truncation is flagged in the response, naming the breakdown as partial.

**Guard.** A test asserts the flag is present whenever the limit is reached. The row cap stayed; the silence did not.

## Cohort values still moving after their window closed

**Symptom.** A figure quoted in a document did not match the same figure recomputed a fortnight later. Both were produced correctly.

**Root-cause class.** Records arrive late, so some values keep changing after their window has closed. Serving such a value as final makes the system appear to contradict itself.

**Fix.** The model marks these values, and an answer for such a window says it is still maturing.

**Guard.** Definition tests require an explicit maturity declaration on metrics whose windows are known to move, so the property is stated rather than discovered by a reader.

---

## What the nine have in common

Every one returned something plausible rather than raising an alarm. A renamed label returned a smaller number, a stale feed returned zero, a truncated breakdown returned part of an answer, a moving value returned a figure that would change.

The design response runs through the whole system: prefer a refusal to a plausible answer. A refusal is visible, specific and gets fixed. A plausible wrong number gets quoted in a meeting, and the cost arrives weeks later as an argument about whose figure is right.
