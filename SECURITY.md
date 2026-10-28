# Security policy

The ad-hoc guard, the access policy and the masking chokepoint are the parts of this project most worth attacking, and reports about them are welcome.

## Reporting a vulnerability

Please report privately through GitHub's **Report a vulnerability** button on this repository's Security tab, not in a public issue. Include the smallest statement, request or configuration that reproduces the problem and what it lets a caller do.

A useful report for the ad-hoc guard is a statement that `governed_metrics.adhoc.guard.check` approves but that PostgreSQL would read differently: a source the analysis does not report, a function outside the allow-list, a write, or a personal column leaving under another name.

## What is in scope

- Bypasses of the ad-hoc guard, the policy enforcer, the live overlay or the masking chokepoint.
- Authentication or request-forgery issues in the JWT verifier or the admin console.
- Anything that lets caller input reach compiled query text.

## What is out of scope

These are documented residual risks ([docs/security.md](docs/security.md)), not vulnerabilities:

- Equality comparison on a personal column acting as an oracle.
- Inference from grouping and counting over masked values.
- Behaviour that depends on a deployment that skips the per-unit read-only database roles, which are the boundary the application guards sit in front of.

Every confirmed bypass gets a regression test written as an attack, one that fails against the unfixed code, before the fix is merged.
