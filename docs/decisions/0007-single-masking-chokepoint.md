# 0007 — One masking chokepoint

**Status:** accepted

## Context

Personal fields must not reach callers who are not entitled to them. Masking could be applied where each field is selected, in each tool that returns rows, or once on the way out.

## Decision

One chokepoint on the output path masks personal fields, with additional guards against reconstructing a masked field inside the query.

## Consequences

- There is one place to audit and one place to test. A reviewer can answer "where is masking applied" with a single location.
- A new tool that returns rows is covered by construction, because the rows pass through the chokepoint regardless of which tool produced them.
- Output-side masking alone would be defeated by rebuilding a masked value inside the query, for example by concatenating or hashing it into a derived column, so the guard refuses those constructions.
- The cost is that the chokepoint must understand the shape of every result, and a result shape it does not understand has to be refused rather than passed through.

## Rejected alternatives

**Mask at each selection site.** Masking in many places is masking that will be omitted in one of them, and the omission is invisible until it matters.

**Mask per tool.** The next tool is written by someone who did not know the rule, and nothing fails when they forget.

**Rely on the database to mask.** Moves the control into warehouse configuration, splits the policy across two systems, and still does nothing about a value reconstructed in the query.
