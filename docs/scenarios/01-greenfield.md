# Scenario 1: greenfield

Build a URL shortener from nothing.

```bash
keel run --scenario greenfield --replay-from demo-greenfield
```

## The requirement

Specified to the level a real ticket would be: endpoints, access model, the open-redirect posture, expiry behaviour, what analytics may retain, and how short codes are generated. The exact text is in `keel/scenarios.py`.

That specificity was earned rather than assumed. The first draft was vaguer, and the analyst refused to plan it, asking who may create links and whether redirect protection is an allowlist or a denylist. Both are security-posture questions with no defensible default, so the honest fix was to answer them in the requirement rather than to loosen the analyst until it stopped noticing.

## Decomposition

Intake normalizes the requirement and scores its confidence. Above threshold with no blocking ambiguity, the planner derives:

```
              +--> implement ----+
 design ----> +--> author-tests -+--> verify --> review --> release-check
              +--> document -----------------------------------^
```

Implementation, test authoring and documentation depend only on the design, so they are scheduled together. Verification depends on two of them.

## Orchestration

What to look for in `runs/<run_id>/audit.jsonl`:

**Parallelism.** Three consecutive `node_started` events with no `node_finished` between them. Those stages are genuinely in flight together, which is why the wall-clock is shorter than the sum of the stages.

**Cost routing.** The `model_call` events name the model. Documentation runs on Haiku 4.5 in about twenty seconds while implementation runs on Opus 5 for several minutes. That difference is the tiering decision, visible in the log rather than asserted in a README.

**The gate doing its job.** In one recorded run, the implement stage produced a redirect handler that never validated the destination scheme and never blocked internal hosts. `OpenRedirectRule` denied the exit gate at HIGH severity, rollback removed the files, and the retry was re-sent with the gate's reasons attached so the next attempt had something to act on.

**Scoped rollback.** When implement rolled back, the documentation node's `README.md` survived. An earlier version restored the whole workspace snapshot and deleted it, because the file did not exist when implement took its snapshot. Rollback is now scoped to the paths the failing node itself wrote.

## Validation

The `verify` node is the point of the scenario. It runs `pytest` as a subprocess against the generated code and records the exit code verbatim in an artifact. A passing run means a suite actually executed.

Two things back that up. A policy rule rejects a claimed pass with no transcript, so a stage cannot satisfy the gate by asserting the suite is green. And `test_a_failing_generated_suite_fails_the_run` in `tests/test_e2e.py` proves the gate can say no, because a gate that cannot fail is decoration.

## Evidence

```
runs/demo-greenfield/
  audit.jsonl     every decision in order
  workspace/      the generated service and its tests
  report.html     metrics, timeline, gate decisions
  summary.json    what was asked and what it cost
```
