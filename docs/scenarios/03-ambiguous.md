# Scenario 3: ambiguous

> Make the links more secure.

```bash
keel run --scenario ambiguous --replay-from demo-ambiguous
```

This is the scenario worth watching, because it is the only one where the correct behaviour is to do nothing.

## What a naive agent does

It picks an interpretation. Maybe it adds authentication. Maybe it adds link expiry, or destination scanning, or rate limiting. Whichever it picks, it produces confident, plausible, well-tested code for a problem nobody asked it to solve, and the mistake surfaces at review time when someone reads it and asks why.

That failure is expensive precisely because the output looks good.

## What happens instead

Intake normalizes the requirement and finds it cannot be planned. "Secure" is doing all the work in that sentence and it resolves to at least four different systems with different data models and different tests.

So the run parks in `input_required`, prints the questions with the reason each one matters, and stops. No plan is built. **No file is written.**

`input_required` is the A2A protocol's own task state, not a local invention, so any A2A-aware client understands that this task is waiting on a human rather than broken.

Typical questions from a real run:

- What does secure mean here: blocking dangerous destinations, requiring authentication to create links, or scanning targets for malware?
- Should link creation require authentication, and if so what identity model?
- Do links need to expire, and what is the default?
- Is scanning destinations against a threat feed in scope?

Each carries a `why_it_matters` explaining what changes depending on the answer, because a question without stakes is a question a busy person ignores.

## Resume, not restart

```bash
keel run --scenario ambiguous --answer-ambiguities
```

The answers resolve the ambiguities in place and the same run proceeds to planning. It does not start over, and the audit log holds the whole arc: the park, the questions, the answers, and the work that followed.

Answering "block the service being used for phishing: reject javascript, data and file schemes, and reject targets resolving to private or link-local addresses" produces the SSRF and open-redirect defences, which is also what `OpenRedirectRule` independently enforces at the exit gate.

## Why the threshold is hard to get right

Both failure modes cost real money, and they are symmetrical.

Guessing produces work nobody wanted. Blocking on a requirement that was merely incomplete stops delivery to ask questions nobody needed answered.

The first version of the analyst had the second problem badly. It blocked on thirteen questions for a well-specified greenfield requirement a senior engineer could have started on that morning. The fix was not to lower the threshold, which would have broken this scenario. It was to give the analyst two ways to handle a gap:

- **A documented assumption.** Pick the defensible industry default, write it down, proceed. Most gaps are this: short code length, default page size, which of 302 and 307 to use.
- **A blocking question.** No defensible default exists, or being wrong is expensive and hard to undo. In practice: security and privacy posture, compatibility with something that already exists, and scope that changes what is being built rather than how.

The calibration test in the prompt: if a senior engineer could start work today on what you were given, you do not have a blocking ambiguity, you have assumptions to write down.

After that change the analyst still asked two questions about the greenfield requirement, and it was right both times. Both were security-posture decisions with no defensible default. The honest fix was to answer them in the requirement rather than to keep tuning the analyst until it stopped noticing.

## Validation

`tests/test_e2e.py` covers both halves:

- `test_ambiguous_requirement_parks_and_writes_nothing` asserts the workspace is empty and no plan was built.
- `test_answering_the_question_unblocks_the_same_run` asserts the resume path resolves the ambiguity rather than re-asking it.
