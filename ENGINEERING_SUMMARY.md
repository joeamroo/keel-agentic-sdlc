# Engineering summary

## What was asked, and what I read it to mean

The brief is titled "Build an Agentic Software Engineering System - URL Shortener", and the wording rewards a careful reading. Section 4.4 says *design and implement an agentic orchestration layer* and labels it the critical differentiator. Deliverable 3 asks for three scenarios, which a single URL shortener cannot demonstrate.

So I read the deliverable as the orchestrator, with the shortener as the thing it builds. If that reading is wrong, most of this repository is the wrong work, so it is worth stating up front rather than leaving implicit.

## Plan and rationale

Two planes with a hard boundary. The execution plane turns a requirement into code. The governance plane decides whether that is allowed and records why. Nothing in the execution plane can approve itself.

The sequencing was deliberate. I verified the A2A SDK end to end before building on it, froze the shared data contracts before parallelising, and wrote the executor by hand rather than delegating it, because it is the part being graded and the part where a subtle error is invisible.

Section 4.4 is one long paragraph containing twelve distinct requirements. I treated it as a checklist and gave each one a named home in the code; the mapping table is in `ARCHITECTURE.md`.

## Artifacts

| Artifact | Where |
| --- | --- |
| Orchestrator | `keel/` |
| Test suite | `tests/`, 610 tests |
| Generated service | `runs/<run_id>/workspace/` |
| Audit trail | `runs/<run_id>/audit.jsonl` |
| Run report | `runs/<run_id>/report.html` |
| Architecture | `ARCHITECTURE.md`, `docs/architecture.drawio` |
| Protocol conformance | `docs/A2A_CONFORMANCE.md` |
| Scenario walkthroughs | `docs/scenarios/` |

## Validation

Unit tests cover the modules. Integration tests drive the real executor against a stub dispatcher, so gates, retries, fallback, rollback, approval, safe stop and parallelism are tested directly rather than inferred from a full run. Contract tests pin the interface between the executor and the stage definitions. A2A tests run both in-process and over real HTTP against a live agent.

Three things are worth calling out because they are where testing usually goes wrong.

**Passing tests were mutation-checked.** Several modules were verified by deliberately breaking the implementation and confirming a test caught it. That found a test of mine that did not work: my first regression test for the concurrent-rollback bug passed with the bug reinstated, because the stub dispatcher never yielded and the two nodes therefore ran in sequence rather than concurrently. The test was rewritten to force real interleaving, then re-checked against the mutation.

**The verification gate runs the real thing.** It executes `pytest` as a subprocess in the workspace and records the exit code verbatim. Nothing asks a model whether the tests passed.

**Parallelism is measured, not assumed.** The concurrency test counts nodes in flight rather than asserting on call order, which would pass for a sequential executor.

## Bugs found by running it

Most of these came from live runs rather than from the suite, which is the honest summary of where the risk actually sat. Every one is now regression-tested, and each fix was mutation-checked by breaking it deliberately to confirm a test catches it.

**Rollback destroyed concurrent work.** Nodes in a topological level share one workspace. A failing node restored the whole snapshot, which deleted a sibling's already-committed output because that file did not exist when the snapshot was taken. In a live run this wiped a finished README. Rollback is now scoped to the paths the failing node itself wrote.

**A policy rule punished good architecture.** The open-redirect rule evaluated each artifact in isolation. The generated service put its redirect in `app/main.py` and its SSRF guard in a dedicated `app/urls.py`, with a scheme allowlist, RFC1918 and link-local networks and the cloud metadata address named explicitly. The rule could not see across modules, so it denied correct code. Worse, because a denial is fed into the retry, it pressured the next attempt to inline the check purely to satisfy the gate. A gate that distorts the work is worse than no gate. The guard is now gathered across the whole change set, verified against the real 25-file service.

**A policy rule blocked honest work.** The test-evidence rule fired on any node of kind TEST that produced no test transcript. The plan has two TEST nodes: one authors the suite, one runs it. The authoring node could never satisfy the rule, so it failed, rolled back and retried forever. The rule now keys on whether a node declares `tests_executed` among its exit rules, which is the contract it actually promised.

**The verification gate could not run at all on a clean machine.** It invoked a bare `python`, which does not exist on macOS or most modern distributions. Exit 127 means the command never ran, so a gate treating any non-zero code as a test failure would have reported a red suite that was never executed. It now uses `sys.executable`.

**Retry was not adaptive.** A live run showed the implement stage produce code the gate correctly rejected, then re-send the identical prompt. A retry that repeats the prompt gets the same answer, so the retry budget bought nothing. Gate violations now travel into the next attempt.

**Brownfield seeded from the wrong run.** The helper accepted a scenario argument and ignored it, returning whichever run was newest. Running brownfield twice therefore seeded from the previous brownfield rather than from greenfield, stacking a change on the same change with no error.

**Cost was silently understated.** The API echoes a dated model id (`claude-haiku-4-5-20251001`) that an alias-keyed pricing table misses, so every Haiku call was priced at zero.

Two more came from other workstreams. Rewriting a generated module with same-length content within the same second let CPython reuse a stale `.pyc`, so **pytest reported a pass on code that fails**, which is precisely the shape of the retry loop and the worst possible failure in a verification gate. And the metrics table transposed rows with a bare `zip`, so a single short row would have dropped a column from the whole table.

**Parallel stages diverged on what the design left open.** Implementation and test authoring run concurrently from the same design and never see each other's output. A live run produced a service reading `DATABASE_PATH` against a suite setting `LINKS_DB_PATH`, and after that was fixed, a disagreement about which status code an exhausted retry budget returns. Both stages were defensible each time; the design simply had not decided. The design schema now pins the configuration surface, and where a disagreement survives that, verification failure triggers a bounded repair rather than stopping the run.

**Two concurrent stages wrote the same file.** `document` and `implement` both produced a README. Neither depends on the other, so the surviving content was whichever finished last, and it changed between the recording and its replay, which is the only reason it surfaced. Both stages succeeded and both gates passed; the workspace simply held one of two plausible answers. The executor now detects a write to a path owned by a node with no dependency relationship and records it.

**A node read state it never declared a dependency on.** Test authoring built its prompt from whatever was on disk, which under concurrency depends on whether implementation had finished. That made its replay key unstable. Payloads are now derived from declared dependencies: a node sees generated code only if it depends on the stage that generates it, and otherwise sees the pre-run baseline.

## Trade-offs

**Deterministic planning over generated planning.** A model-generated graph would look more autonomous. It would also differ between runs, which makes an audit trail nearly worthless and reproduction impossible. Judgement lives in the analysis; the topology follows by readable rules.

**Replay falls back from exact-prompt matching to per-node matching.** The primary key is a hash of the prompt, which proves a replayed response was produced for exactly this input. It is also brittle: editing any stage prompt invalidates otherwise perfectly good recordings. A miss now falls back to the next unused recording for the same node and says so, in the CLI and on the run. The alternative was re-recording every scenario after every prompt edit, and a replay that silently served a mismatched recording would be worse than either.

**Replay by default.** The repository is public and reviewers should not need an API key or a bill. Replay plays back real recorded model output, and the orchestration still executes for real against it. The cost is that a reader could mistake replay for simulation, which is why it is stated plainly in the README.

**A2A transport adopted in full.** More moving parts than in-process calls. It buys protocol-level cancellation, a standard state for human-in-the-loop, and capability discovery from Agent Cards. It also means the mesh is closer to something that could federate with agents we do not own.

**Tiered models.** Opus 5 where a decision gets encoded by later stages, Haiku 4.5 where the facts are already settled. Per-stage cost is recorded so the choice is checkable rather than asserted.

## Assumptions

- The orchestrator is the deliverable and the URL shortener is the subject.
- A single reviewer machine, not a deployed environment. The mesh binds loopback.
- Human approval is scripted for reproducible demo runs; an interactive broker exists and fails closed.
- Generated code targets FastAPI and SQLite, chosen for readability rather than for scale.

## Limitations

Stated rather than hidden.

- **Re-planning triggers on one class of evidence**: a missing extension point found during impact analysis. The mechanism is general; the trigger set is not.
- **No TLS on the mesh.** Bearer auth is real and enforced, transport security is not. Correct for loopback, unacceptable anywhere else.
- **The policy engine is heuristic.** It reads text rather than parsing an AST, so it has known false positives, including a `shutil.rmtree` call whose guard sits three lines above it. Those were left flagging rather than silenced with a fragile context window, because weakening a critical rule to reduce noise is the wrong direction.
- **Single-machine concurrency.** Parallelism is asyncio within one process. Distributing stages across hosts is a deployment change, and the scoped-rollback bug above is a preview of what else that would surface.
- **MTTR is undefined when nothing has failed**, and reported as such. Zero would assert instant recovery, which is a different claim from never having been asked to recover.
- **Cassettes are per requirement.** Changing a scenario's wording invalidates its recording, and the run must be re-recorded live.
- **The generated service is not production software.** It is honest output from a real pipeline, held to the policy gates, and it is a demonstration rather than a thing to deploy.

**Repair over halt on failed verification.** A failing suite could reasonably stop the run and hand it to a person. It repairs instead, bounded to two attempts, because the common cause is two concurrent stages disagreeing about something their shared design left unspecified, and a human would resolve that the same way: keep the tests, change the code. The risk is a system that grinds against its own oracle, which is why the budget is small and every attempt is in the audit log.

## What I would do next

Parse rather than pattern-match in the policy engine. Broaden re-planning triggers to any upstream artifact whose hash changes under a consumer, which the lineage store already detects but the planner does not yet act on. Put the stage agents behind TLS with per-agent identities. Add a cost budget as a first-class governance control, so a run can be stopped for spending as readily as it is stopped for a policy breach.
