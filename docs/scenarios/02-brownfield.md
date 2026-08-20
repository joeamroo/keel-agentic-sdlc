# Scenario 2: brownfield

Add per-API-key rate limiting to the service greenfield built.

```bash
keel run --scenario greenfield --replay-from demo-greenfield   # first, if you have not
keel run --scenario brownfield --replay-from demo-brownfield
```

The workspace is seeded from the greenfield run's output, so this operates on real generated code rather than on a fixture written to make the scenario work.

## Decomposition

Intake gets the existing source and a different question. Not what to build, but what this change touches. It comes back with the impacted modules, the affected routes, the data flows those routes read and write, and a classification of the scenario as brownfield.

So the planner produces a different graph. An `impact-analysis` node runs first, design depends on it, and implementation is marked **high impact**, because changing a public API surface that existing clients depend on is a change-control event rather than an ordinary edit.

## Orchestration

Three things happen here that greenfield never exercises.

### Change control

Implementation carries `ImpactLevel.HIGH`, so `needs_approval` is true and the executor requests sign-off **before** the node runs. Decline it and the node is `REJECTED` with nothing written, which the audit log records as `approval_decided` with `approved: false`.

Approval is requested before the entry gate on purpose. Asking a person to authorise work whose preconditions have not been checked wastes their attention.

The broker fails closed. Empty input, EOF and an unrecognised answer all deny, because for a high-impact change silence is not consent.

### Dynamic re-planning

This is the requirement most implementations skip.

Impact analysis can discover that the existing code has no extension point for the requested change. The plan is now wrong: it assumed implementation could hook into a seam that turns out not to exist. Rather than pushing on regardless, the planner emits **version 2**, inserts a `refactor-seam` node ahead of implementation, and rewires implementation to depend on it.

The old plan is never mutated. The new version carries a `supersedes` pointer and a written rationale, and both land in the audit log. That matters when something goes wrong later, because an incident review wants to know what the system intended before the evidence arrived as much as what it did afterwards.

The scheduler recomputes the ready set every round rather than walking precomputed levels, which is what makes mid-run re-planning possible at all.

### Lineage

`LineageStore` records which version of which artifact fed which node, keyed by content hash. That is what makes staleness detectable: when an upstream artifact changes under a consumer, the consumers of the old hash are known.

## Validation

The same gates as greenfield, plus `ChangeControlRule`, which flags a high-impact node that produced artifacts without approval evidence. The generated tests still have to pass under a real pytest run. Existing behaviour has to survive too.

## Evidence

Look for `approval_requested`, `approval_decided` and `plan_revised` in `runs/<run_id>/audit.jsonl`. The report renders the approval log and both plan versions.

## Honest limit

Re-planning triggers on one class of evidence: a missing extension point found during impact analysis. The mechanism is general, and the lineage store already detects hash-level staleness that the planner does not yet act on. Broadening the trigger set is the obvious next step.
