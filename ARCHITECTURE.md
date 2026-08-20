# Architecture

## The shape of the problem

Getting a model to write a URL shortener is a prompt. Getting a system you would let modify a repository unattended is an engineering problem, and nearly all of it lives outside the model: deciding what to do next, refusing bad input, stopping before something irreversible, undoing partial work, and leaving evidence.

So keel is two planes with a hard boundary between them.

```
GOVERNANCE PLANE
  policy engine | approval broker | audit log | lineage store
  metrics | gates | retry, fallback, rollback | re-planner
        ^ decides whether work may proceed, and records why
        |
EXECUTION PLANE
  intake -> planner -> executor -> A2A stage agents -> adapter -> Claude
        |
        v
WORKSPACE   the service under construction, snapshotted per node
```

The execution plane does work. The governance plane decides whether work is allowed and writes down what happened. Nothing in the execution plane can approve itself.

## Components

| Module | Responsibility |
| --- | --- |
| `models.py` | Frozen contracts. Imports nothing else in the package, which keeps the dependency graph acyclic. |
| `intake.py` | Requirement to `EngineeringProblem`, with ambiguity detection and a confidence score. |
| `planner.py` | Derives the dependency graph. Emits new plan versions; never mutates one. |
| `graph.py` | The DAG: topological levels, cycle detection, staleness propagation, subgraph extraction. |
| `executor.py` | The engine. Gates, concurrency, retry, fallback, rollback, safe stop, re-plan. |
| `dispatch.py` | The seam between orchestration and how a stage actually runs. |
| `a2a/` | Agent Cards, stage servers, client, and the two transports. |
| `adapters/` | Live Claude and replay, behind one interface. |
| `governance/` | Policy, approvals, audit, lineage, metrics. |
| `workspace.py` | Path-confined filesystem, snapshot and restore, real subprocess execution. |

## Control flow

Intake runs first and is deliberately not a node in the plan. You cannot schedule work before you know what the work is, so understanding the requirement cannot live inside the graph that understanding produces.

If the analyst reports a blocking ambiguity or low confidence, the run parks in `input_required` and stops. No plan is built and no file is written.

Otherwise the planner emits a graph and the executor walks it:

```
  while there is runnable work:
      ready = nodes whose dependencies are all satisfied
      run every node in `ready` concurrently
      if new evidence invalidates the plan: re-plan and continue
```

The scheduler recomputes the ready set each round rather than walking precomputed levels. Levels are correct until the plan changes underneath you, and re-planning is a requirement here, so the loop asks the graph what is runnable now.

Around each node:

```
  approval (if high impact)
      -> entry gate      preconditions and inputs
      -> snapshot        capture the workspace
      -> dispatch        stage agent does the work
      -> exit gate       artifacts present, policy clean, tests actually ran
      -> commit
           on failure or denial:
              bounded retry -> fallback -> rollback -> safe stop
```

Node-level retry handles a stage that failed on its own terms. It cannot handle
two stages that disagree, because re-running one of them in isolation changes
nothing. That case is repaired at the graph level instead:

```
  verify fails
      -> discard the implementation and the verification
      -> re-run implementation with the failing transcript
      -> verify again
      (bounded; the stages that did not fail keep their results)
```

Approval comes before the entry gate on purpose. Asking a person to sign off on work whose preconditions have not been checked wastes their attention, and attention is the scarce resource in a human-in-the-loop system.

## The plan graph

The topology is a diamond, and that is not decoration. `implement`, `author-tests` and `document` depend only on `design`, so they run at the same time. `verify` depends on two of them, which makes it a genuine synchronization barrier.

```
                 +--> implement ----+
   design -----> +--> author-tests -+--> verify --> review --> release-check
                 +--> document ----------------------------------^
```

`verify` does not ask a model whether the tests pass. It runs `pytest` in the workspace as a subprocess and records the exit code verbatim.

## Key decisions

**The plan is derived, not generated.** A model does the analysis; the topology follows from that analysis by rules a human can read. A graph that differs on every invocation cannot be reproduced, and an audit trail of an unreproducible decision is worth very little. Judgement lives in the analysis, where it belongs.

**Plans are versioned and immutable.** Re-planning emits version N+1 with a `supersedes` pointer and a written rationale. The audit log therefore shows what the system intended before and after new evidence arrived, which is the question an incident review actually asks.

**The audit log is the replay cassette store.** Recording every model call was already required for traceability. Reusing those recordings for replay means observability and reproducibility cannot drift apart, because there is only one artifact. It also makes the public repository runnable without a key.

**Task state mirrors A2A rather than importing it.** The values match the SDK enum exactly and convert at the boundary. Governance logic stays serializable and free of a wire-format dependency while remaining protocol-faithful.

**Cost routing is per node.** Each node declares the tier it needs. Analysis, design, implementation, tests and review get Opus 5, because each one is a decision that later stages encode. Documentation and release checks get Haiku 4.5, because the facts are already in the artifacts by then. Per-stage cost lands in the metrics so the choice can be checked against the bill.

**A failing test suite is evidence, not a verdict.** When verification fails,
the run repairs rather than stopping. The implementation is discarded and
regenerated with the failing transcript in hand, then verified again, bounded
to two attempts. The tests are treated as the specification and the
implementation is what moves, because the suite was written from the design and
encodes the acceptance criteria; letting the implementation rewrite its own
oracle would be marking its own work.

This exists because of a real failure. Implementation and test authoring run
concurrently from the same design and never see each other's output, so they can
disagree about anything the design left open. A live run disagreed on which
status code an exhausted retry budget returns and on whether a client may set
`created_at`. Both stages were defensible. The design simply had not decided,
and no amount of node-level retry could have resolved it.

**Everything a node touches is snapshotted first.** A stage that fails halfway through writing six files leaves the workspace in a state no later stage was designed for. Restoring is cheaper and more honest than compensating.

**Approval fails closed.** Empty input, EOF or an unrecognised answer all deny. For a high-impact change in a regulated environment, silence is not consent.

## How the assignment's requirements map to code

| Requirement | Where |
| --- | --- |
| Explicit dependency graph, entry and exit gates | `graph.py`, `gates.py`, `executor.py` |
| Sequential and parallel paths with synchronization | `executor.py` ready-set scheduler |
| Cross-stage context and decision lineage | `governance/lineage.py` |
| Human approval checkpoints | `governance/approvals.py`, A2A `input_required` |
| Bounded retries | `RetryPolicy` per node, plus a graph-level repair loop |
| Fallback | alternate tier and strategy hint on final attempt |
| Rollback | `workspace.py` snapshot and restore |
| Safe-stop controls | `Executor.request_stop`, A2A `tasks/cancel` |
| Policy guardrails | `governance/policy.py` |
| Audit-grade observability | `governance/audit.py` |
| Reliability metrics | `governance/metrics.py` |
| Dynamic re-planning | `planner.revise_from_evidence`, hash-based staleness |

## Limits

Documented rather than hidden; the full list is in `ENGINEERING_SUMMARY.md`.

The generated service is small by design, since the point is the orchestration. Re-planning triggers on one class of evidence (a missing extension point found during impact analysis) rather than on arbitrary contradictions. The mesh runs locally over HTTP with bearer auth and no TLS, which is right for a local demo and wrong for anything else. MTTR is undefined rather than zero when nothing has failed, which is the honest reading of an empty sample.
