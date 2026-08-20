# keel

A governed agentic orchestrator for the software lifecycle. You give it a requirement in English; it plans the work as a dependency graph, runs the stages across a mesh of A2A agents, checks its own output at every step, stops to ask a human when the stakes justify it, and writes down everything it did and why.

It builds a URL shortener. **The URL shortener is not the point.** It is the thing being built, chosen because it is small enough to generate honestly and interesting enough to have real security properties. The deliverable is the machinery that builds it.

## Why that distinction matters

An agent that writes code is not hard to demonstrate. An agent you would let near a production repository is a different problem, and almost all of that problem is governance: knowing what to do next, refusing to proceed on a bad requirement, stopping before a change nobody approved, undoing a half-applied edit, and leaving a trail that survives an audit.

So the interesting surface here is the part between the requirement and the model:

```
requirement -> intake -> plan graph -> executor -> stage agents -> workspace
                  |          |            |
                  |          |            +-- entry gate, exit gate, retry,
                  |          |                fallback, rollback, safe stop
                  |          +-- versioned, re-planned when evidence changes
                  +-- refuses to plan a requirement it cannot plan
```

## Quickstart

Requires Python 3.13 (the A2A SDK does). No API key needed.

```bash
uv venv --python 3.13 && uv pip install -e .
.venv/bin/python -m keel.cli run --scenario greenfield
```

That replays a recorded run: real Claude output, captured from a live run and committed to `runs/`, played back offline and for free. The orchestration is not replayed. Every gate, retry, rollback and policy check executes for real against the recording.

To run it live against your own key:

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY
.venv/bin/python -m keel.cli run --scenario greenfield --mode live
```

`.env` is gitignored, and nothing reads a key from source.

## The three scenarios

```bash
keel run --scenario greenfield   # build the service from nothing
keel run --scenario brownfield   # add rate limiting to what greenfield built
keel run --scenario ambiguous    # "make the links more secure"
```

**Greenfield** exercises decomposition and parallelism. Implementation, test authoring and documentation all depend only on the design, so they run concurrently, and verification is a real synchronization barrier that actually executes pytest against the generated code. The exit code is recorded verbatim. Nothing claims a passing test suite it did not run.

**Brownfield** exercises codebase reasoning and change control. It reads what greenfield produced, identifies the impacted modules and routes, and because it modifies a public API surface it stops for human approval before writing anything. If impact analysis contradicts the original plan, the planner emits a new plan version rather than mutating the old one, and the audit log shows both.

**Ambiguous** is the one worth watching. Given "make the links more secure," the analyst does not guess. It parks the task in the A2A `input_required` state, emits the questions it needs answered, and writes zero lines of code. Answer them and it resumes from the park instead of starting over:

```bash
keel run --scenario ambiguous --answer-ambiguities
```

## What each run leaves behind

```
runs/<run_id>/
  audit.jsonl     every gate, decision, model call, retry and rollback, in order
  workspace/      the generated service
  report.html     self-contained, opens offline, no network requests
  summary.json    what was asked, what was decided, what it cost
```

`audit.jsonl` is append-only, credential-redacted, and integrity-checkable. It is also the replay cassette store, so observability and reproducibility are the same artifact rather than two systems that can disagree.

## Cost routing

Nodes declare the thinking they need. Planning, code generation and review run on Claude Opus 5; documentation and release checks run on Haiku 4.5. The per-stage cost breakdown is in every run's metrics, so the routing decision can be checked against the bill.

## A2A

Each lifecycle stage is a real A2A agent serving an Agent Card at `/.well-known/agent-card.json` over JSON-RPC, with bearer auth between the orchestrator and the agents. The task states are the SDK's own enum, so `input_required` above is the protocol's state, not a local invention, and safe stop is a genuine `tasks/cancel` call.

A2A is an interoperability protocol. It deliberately says nothing about governance, which means no dependency graph, gates, retry policy, rollback or re-planning. That gap is what this project is. See `docs/A2A_CONFORMANCE.md` for exactly what is protocol and what is ours, including what we do not implement.

## The assignment, answered

Every numbered requirement, what it means here, and where to look.

### 4.1 Requirement understanding

`keel/intake.py` turns English into an `EngineeringProblem`: intent, testable acceptance criteria, constraints, an explicit ambiguity list and a confidence score. The interesting behaviour is the refusal. Below the confidence threshold, or with any unresolved blocking ambiguity, the run parks and writes nothing.

Calibrating that was real work. The first version blocked on thirteen questions for a requirement a senior engineer could have started on, which is the mirror image of guessing and costs just as much. The prompt now separates a **documented assumption** (pick the defensible default, record it, proceed) from a **blocking question** (no defensible default, or wrong is expensive and irreversible).

### 4.2 Task decomposition

`keel/planner.py` derives a dependency graph with explicit sequencing. Nodes declare dependencies, gates, model tier, retry budget, impact level and rollback behaviour.

### 4.3 Codebase reasoning

The brownfield scenario reads the service greenfield produced, identifies impacted modules, routes and data flows, and marks the change high-impact because it touches a public surface. `keel/governance/lineage.py` tracks which version of which artifact fed which node, so "why does this file look like this" has an answer.

### 4.4 Workflow orchestration

The critical differentiator, and the twelve requirements buried in that paragraph each have a home:

| Requirement | Where |
| --- | --- |
| Dependency graph with entry and exit gates | `keel/graph.py`, `keel/executor.py` |
| Sequential and parallel paths with synchronization | ready-set scheduler; `verify` is the barrier |
| Cross-stage context and decision lineage | `keel/governance/lineage.py` |
| Human approval checkpoints | `keel/governance/approvals.py`, A2A `input_required` |
| Bounded retries | `RetryPolicy` per node, with the gate's reasons fed back |
| Fallback | alternate tier and strategy on the final attempt |
| Rollback | `keel/workspace.py`, scoped to the failing node's own writes |
| Safe-stop controls | `Executor.request_stop`, A2A `tasks/cancel` |
| Policy guardrails | `keel/governance/policy.py` |
| Audit-grade observability | `keel/governance/audit.py` |
| Reliability metrics | `keel/governance/metrics.py` |
| Dynamic re-planning | `Planner.revise_from_evidence` |

Non-linear and stateful: the scheduler recomputes what is runnable each round rather than walking a fixed list, which is what lets the plan change underneath it.

### 4.5 Engineering output

Production-shaped FastAPI and SQLite source, an API design with justified trade-offs, tests including security cases, and documentation. Held to the policy gates like anything else.

### 4.6 Validation and risk control

Gates at both ends of every node. Six policy rules covering secrets, open redirect, destructive operations, change control, test evidence and PII. Risks and trade-offs are in `ENGINEERING_SUMMARY.md`, including the known false positives, which were left flagging rather than silenced.

### 4.7 Controlled autonomy

Agents run multi-step work; humans hold the boundary. High-impact nodes need sign-off before they execute, the approval broker fails closed on silence, and an underspecified requirement stops the run rather than proceeding on a guess.

### 4.8 Final engineering summary

`ENGINEERING_SUMMARY.md`, including the bugs the live runs found.

### Deliverables

| Asked for | Here |
| --- | --- |
| Working prototype, runnable end to end | `make demo`, no key required |
| Architecture overview | `ARCHITECTURE.md`, `docs/architecture.drawio`, `docs/architecture.pdf` |
| Three scenarios with decomposition, orchestration, validation | `docs/scenarios/`, evidence in `runs/` |
| Setup instructions | above |
| Testing approach, limitations, trade-offs | `docs/TESTING.md`, `ENGINEERING_SUMMARY.md` |

## Development

```bash
.venv/bin/python -m pytest -q          # full suite
.venv/bin/python -m pytest -m "not http"   # skip tests that bind ports
```

## Documentation

| File | What it covers |
| --- | --- |
| `ARCHITECTURE.md` | components, control flow, and the decisions behind them |
| `docs/architecture.drawio` | editable diagram, opens at app.diagrams.net |
| `docs/A2A_CONFORMANCE.md` | what is protocol, what is ours, what is missing |
| `docs/TESTING.md` | test strategy, limitations, trade-offs |
| `ENGINEERING_SUMMARY.md` | plan, rationale, risks, assumptions, limits |
| `docs/scenarios/` | a walkthrough per scenario |
