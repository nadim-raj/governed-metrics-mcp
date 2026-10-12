# 0004 — A lexer-based guard instead of regular expressions

**Status:** accepted

## Context

The ad-hoc tool accepts a statement written by a model and has to decide whether it may run. The first implementation chained regular expressions to look for forbidden constructs.

It was bypassable. A regular expression inspects how a statement is spelled, and comments, nested sub-selects, unusual whitespace and case all change the spelling without changing what the statement does. The guard and the warehouse were reading two different statements.

## Decision

A real single-pass lexer tokenises the statement the way the warehouse does. Every classification decision is made over tokens, never over raw text.

## Consequences

- The known bypass classes stop working, because they change spelling and the guard no longer reads spelling.
- A write hidden inside something that looks like a read becomes visible, since the structure is inspected rather than the surface.
- The guard must track the warehouse. A warehouse upgrade that changes lexical rules can move the two apart, so regression tests pin the agreement and the upgrade checklist reruns them.
- The cost is a component that has to be correct and maintained, rather than a list of patterns anyone can extend.

## Rejected alternatives

**More regular expressions.** Each new pattern closes one bypass and implies the set is now complete, which it never is. The approach cannot be made sound, only longer.

**A denylist of forbidden keywords.** Answers what was thought of. A caller improvising statements will eventually produce something nobody thought of.

**Trusting the read-only database role alone.** The role is the control that matters and is in place, but it does not bound cost: an unbounded read-only scan still degrades the warehouse for everyone.
