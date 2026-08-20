# Testing approach

## What is tested, and why there

There are 578 tests, but the split across layers matters more than the count.

| Layer | What it covers | Why it lives there |
| --- | --- | --- |
| Unit | Each module in isolation: graph, policy, audit, lineage, metrics, approvals, workspace, adapters, stage definitions. | Fast, precise failure messages, no shared state. |
| Integration | The real `Executor` against a stub dispatcher. Gates, retries, fallback, rollback, approval, safe stop, parking, tier routing, concurrency. | The orchestration is the deliverable, so it is tested directly rather than inferred from a full pipeline run. |
| Contract | The interface between the executor and the stage definitions. | A drifted template variable used to fail several minutes and several dollars into a live run. Now it fails in 80 milliseconds. |
| Protocol | A2A in-process for speed, plus real HTTP against a live agent for card discovery, streaming, parking and cancellation. | In-process alone would test our wrapper rather than the protocol. |

Tests that bind ports carry the `http` marker, so `pytest -m "not http"` gives a port-free run for constrained environments.

No test makes a network call to Anthropic. The Claude adapter is exercised through an injected fake client.

## The parts worth arguing about

**Green is not evidence.** Several modules were checked by deliberately breaking the implementation and confirming a test caught it. Where nothing caught it, the test was missing or wrong.

That found a bad test of mine. My first regression test for the concurrent-rollback bug passed with the bug put back, because the stub dispatcher never awaited, so `asyncio.gather` ran the two nodes one after the other and the race never happened. The test now forces real interleaving and fails when the fix is reverted.

**Parallelism is measured.** The concurrency test counts how many nodes are in flight at once. Asserting on call ordering would pass for a purely sequential executor, which is the failure this test exists to rule out.

**The verification gate runs pytest for real.** It shells out in the workspace and records the exit code verbatim. A test asserts a claimed pass with no transcript is rejected, so the gate cannot be satisfied by a model saying the suite is green.

**Secret scanning was tested against a real key shape.** The fixture is assembled from split literals at import time, so no contiguous key-shaped string sits in the repository, and a guard test asserts the fixture still has the right shape. A mangled fixture would make redaction look like it works.

## Limitations

**Coverage is not measured.** The number would be misleading here: large parts of the value are in prompts and in whether the model behaves, neither of which a coverage percentage speaks to.

**Stage prompts are tested structurally, not for output quality.** The tests check that schemas are strict, that templates render, and that required variables exist. Whether a prompt elicits good code is answered by running it, and that evidence lives in `runs/` rather than in the suite.

**Replay determinism is not asserted end to end.** Individual cassette lookups are tested, but there is no test that a full replayed run reproduces its recorded run byte for byte.

**One process, one machine.** Concurrency is asyncio within a single process. The scoped-rollback bug is a preview of what distributing stages across hosts would surface, and none of that is covered.

**No property-based or fuzz testing.** The path-containment logic in the workspace is the obvious candidate, and it is currently covered by enumerated cases (traversal, absolute paths, escaping symlinks, null bytes) rather than generated ones.

**Timing tests use real sleeps.** The concurrency and backoff tests wait on the clock, so they are slower than they need to be and could in principle flake on a heavily loaded machine.

## Running them

```bash
.venv/bin/python -m pytest -q                 # everything
.venv/bin/python -m pytest -q -m "not http"   # no ports bound
.venv/bin/python -m pytest tests/test_executor.py -q   # orchestration only
```
