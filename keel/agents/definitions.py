"""Prompts and output schemas for the seven SDLC stage agents.

This module is the part of keel that decides how good the generated software
is. The orchestrator routes work, the gates reject bad work, but nothing in
this system improves an artifact after the fact: what a stage produces is a
direct function of the system prompt, the rendered prompt and the JSON schema
declared here. Everything else is plumbing around these strings.

Three rules shape every definition below.

Schemas are strict. Every object sets ``additionalProperties: false`` and lists
every required key, because a response that quietly grows a field is a response
the gates cannot check and the replay cassettes cannot be diffed against.

Prompts state the goal and the constraints, not the steps. A capable model does
not need to be told to think carefully; it needs to be told what counts as
wrong. So each prompt names the specific failure it is defending against and
what the downstream consumer does with the output.

Nothing here talks to a model or reads a file. `render` returns two strings and
the caller decides what to do with them, which keeps the definitions importable
from tests, from the Agent Card builder in `keel.a2a`, and from documentation
tooling without dragging in an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import Any

from ..models import ModelTier, StageKind

__all__ = [
    "DEFINITIONS",
    "MissingTemplateVariable",
    "StageDefinition",
    "definition_for",
    "render",
]


class MissingTemplateVariable(ValueError):
    """`render` was called without every variable the template needs.

    A `ValueError` rather than the bare `KeyError` that `str.format` raises,
    because the useful message is not the missing key on its own: it is the
    stage, the key, and the full set the caller was supposed to supply.
    """


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """Everything needed to dispatch one SDLC stage to a model.

    Fields:
        kind: the stage this defines. One definition per `StageKind`.
        skill_id: Agent Card skill id. Equal to `kind.value` so a `NodeSpec`
            can be routed by skill id alone without a second lookup table.
        title: short human label, used in the Agent Card and the run UI.
        description: the Agent Card skill description. Written to tell a
            caller when to select this skill and when not to.
        model_tier: which model the stage is worth. See the per stage notes.
        system_prompt: the role, the standard of work, the failure modes.
        prompt_template: `str.format` template filled with run specific text.
        json_schema: strict JSON Schema the response must satisfy.
        produces: artifact names the executor expects the stage to write.

    Template variables per stage. ANALYZE is fixed by the dispatcher contract;
    the rest are declared here and read off `template_variables` by callers:

        ANALYZE        requirement, existing_code, prior_answers
        DESIGN         intent, acceptance_criteria, constraints, scenario
        IMPLEMENT      intent, design, acceptance_criteria, constraints
        TEST           design, implementation, acceptance_criteria
        DOCUMENT       intent, design, implementation
        REVIEW         artifacts, acceptance_criteria, constraints
        RELEASE_CHECK  acceptance_criteria, artifacts, review_findings
    """

    kind: StageKind
    skill_id: str
    title: str
    description: str
    model_tier: ModelTier
    system_prompt: str
    prompt_template: str
    json_schema: dict[str, Any]
    produces: list[str]

    @property
    def template_variables(self) -> tuple[str, ...]:
        """Names `prompt_template` requires, in first appearance order."""
        seen: list[str] = []
        for _, name, _, _ in Formatter().parse(self.prompt_template):
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)

    @property
    def model(self) -> str:
        """Concrete model id this stage routes to."""
        from ..models import MODEL_FOR_TIER

        return MODEL_FOR_TIER[self.model_tier]


# --------------------------------------------------------------------------
# Schema fragments
# --------------------------------------------------------------------------

def _file_item(content_hint: str) -> dict[str, Any]:
    """A single generated file. Same shape for code, tests and docs."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "content"],
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to the workspace root, POSIX separators, "
                    "no leading slash and no '..' segments."
                ),
            },
            "content": {"type": "string", "description": content_hint},
        },
    }


_AMBIGUITY_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question", "why_it_matters", "blocking", "options"],
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "One decision the requirement left open, phrased so a product "
                "owner could answer it in a sentence."
            ),
        },
        "why_it_matters": {
            "type": "string",
            "description": (
                "What is built differently depending on the answer. Name the "
                "concrete divergence, not the abstract risk."
            ),
        },
        "blocking": {
            "type": "boolean",
            "description": (
                "True when the two answers produce different code or a "
                "different contract. False only when the branches converge."
            ),
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The plausible answers, most likely first.",
        },
    },
}

_ANALYZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "acceptance_criteria",
        "constraints",
        "ambiguities",
        "scenario",
        "confidence",
        "notes",
    ],
    "properties": {
        "raw_requirement": {
            "type": "string",
            "description": (
                "Verbatim echo of the requirement as received. Optional; the "
                "orchestrator fills it in when omitted. Present so a response "
                "deserializes straight into EngineeringProblem."
            ),
        },
        "intent": {
            "type": "string",
            "description": (
                "One or two sentences: what the system must do and for whom. "
                "Behaviour only, no technology choices."
            ),
        },
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Observable outcomes, each checkable by a single test. State "
                "the input, the condition and the expected response or state."
            ),
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Limits imposed on any acceptable solution: runtime, "
                "framework, storage, latency, compatibility, security."
            ),
        },
        "ambiguities": {"type": "array", "items": _AMBIGUITY_ITEM},
        "scenario": {
            "type": "string",
            "enum": ["greenfield", "brownfield", "ambiguous"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "Probability that an engineer could implement from this "
                "normalization without coming back with a question."
            ),
        },
        "notes": {
            "type": "string",
            "description": (
                "Anything downstream stages need that the fields above do not "
                "carry. For brownfield this carries the impact surface."
            ),
        },
    },
}

_DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "overview",
        "endpoints",
        "data_model",
        "decisions",
        "redirect_status_code",
        "short_code_strategy",
        "open_questions",
    ],
    "properties": {
        "overview": {
            "type": "string",
            "description": "Two or three sentences on the shape of the service.",
        },
        "endpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "method",
                    "path",
                    "summary",
                    "request",
                    "response",
                    "status_codes",
                ],
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                    },
                    "path": {
                        "type": "string",
                        "description": "Route with path parameters in braces.",
                    },
                    "summary": {"type": "string"},
                    "request": {
                        "type": "string",
                        "description": (
                            "Request body fields with types and validation "
                            "rules, plus query and header parameters. Write "
                            "'none' when the endpoint takes no input."
                        ),
                    },
                    "response": {
                        "type": "string",
                        "description": (
                            "Success response body with field names and types, "
                            "and the error body shape."
                        ),
                    },
                    "status_codes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "when"],
                            "properties": {
                                "code": {"type": "integer"},
                                "when": {
                                    "type": "string",
                                    "description": "The exact condition that produces it.",
                                },
                            },
                        },
                        "description": "Every code, including the failure ones.",
                    },
                },
            },
        },
        "data_model": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "purpose", "fields", "indexes"],
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "type", "constraints"],
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                                "constraints": {
                                    "type": "string",
                                    "description": (
                                        "Nullability, uniqueness, defaults, "
                                        "foreign keys, value ranges."
                                    ),
                                },
                            },
                        },
                    },
                    "indexes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Each index and the query that needs it.",
                    },
                },
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision", "choice", "rationale", "rejected"],
                "properties": {
                    "decision": {"type": "string", "description": "The question decided."},
                    "choice": {"type": "string"},
                    "rationale": {
                        "type": "string",
                        "description": "Why, in terms of a consequence a user would notice.",
                    },
                    "rejected": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alternatives considered and what each costs.",
                    },
                },
            },
        },
        "redirect_status_code": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "rationale"],
            "properties": {
                "code": {"type": "integer"},
                "rationale": {
                    "type": "string",
                    "description": (
                        "Must address revocability and click counting, and say "
                        "what a permanent redirect would cost."
                    ),
                },
            },
        },
        "short_code_strategy": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "alphabet",
                "length",
                "generation",
                "uniqueness",
                "collision_handling",
            ],
            "properties": {
                "alphabet": {"type": "string"},
                "length": {"type": "integer"},
                "generation": {
                    "type": "string",
                    "description": "Source of randomness and why it is unguessable.",
                },
                "uniqueness": {
                    "type": "string",
                    "description": "Where uniqueness is actually enforced.",
                },
                "collision_handling": {
                    "type": "string",
                    "description": "Retry bound and what happens when it is exhausted.",
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Decisions deferred to implementation, with the safe default.",
        },
    },
}

_IMPLEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files", "notes"],
    "properties": {
        "files": {
            "type": "array",
            "items": _file_item(
                "Complete file contents. Runnable as written, no placeholders."
            ),
            "description": "Every file the service needs, including requirements.txt.",
        },
        "notes": {
            "type": "string",
            "description": (
                "Deviations from the design and why, plus anything the "
                "reviewer should look at first."
            ),
        },
    },
}

_TEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files", "coverage_notes"],
    "properties": {
        "files": {
            "type": "array",
            "items": _file_item(
                "Complete pytest module. Imports resolve against the "
                "implementation files as written."
            ),
        },
        "coverage_notes": {
            "type": "string",
            "description": (
                "Which acceptance criterion each test file covers, and what is "
                "deliberately not covered."
            ),
        },
    },
}

_DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files"],
    "properties": {
        "files": {
            "type": "array",
            "items": _file_item("Complete markdown document."),
            "description": "Normally a single README.md.",
        }
    },
}

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "verdict", "summary"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "severity",
                    "confidence",
                    "category",
                    "file",
                    "line",
                    "summary",
                    "recommendation",
                ],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["info", "low", "medium", "high", "critical"],
                        "description": "Consequence if exploited or hit, not effort to fix.",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": (
                            "How sure you are the finding is real. Low "
                            "confidence findings belong in the list."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "security",
                            "correctness",
                            "reliability",
                            "performance",
                            "maintainability",
                            "testing",
                            "documentation",
                        ],
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Path as given in the artifacts. Use the file the "
                            "fix belongs in when something is missing."
                        ),
                    },
                    "line": {
                        "type": "integer",
                        "description": (
                            "1 based line number. 0 when the finding is about "
                            "the file as a whole or about absent code."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "The concrete failure and the path that reaches it."
                        ),
                    },
                    "recommendation": {
                        "type": "string",
                        "description": "The specific change, not a principle.",
                    },
                },
            },
        },
        "verdict": {
            "type": "string",
            "enum": ["approve", "approve_with_findings", "reject"],
        },
        "summary": {
            "type": "string",
            "description": "Two or three sentences on the overall state of the code.",
        },
    },
}

_RELEASE_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ready", "blockers", "warnings", "checklist"],
    "properties": {
        "ready": {
            "type": "boolean",
            "description": "True only when blockers is empty.",
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Each names what is missing or broken and where.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Should be fixed, does not stop the release.",
        },
        "checklist": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "passed"],
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "The thing checked, stated as a claim.",
                    },
                    "passed": {
                        "type": "boolean",
                        "description": "Your verdict on the evidence supplied, not on likelihood.",
                    },
                },
            },
        },
    },
}


# --------------------------------------------------------------------------
# System prompts
# --------------------------------------------------------------------------

_ANALYZE_SYSTEM = """\
You are the requirement analyst for an automated software delivery pipeline. \
Design, code, tests and release checks are all generated from your output and \
no human reads the requirement again before code exists. An assumption you \
make quietly becomes a defect that nobody traces back to this step.

Your job is to turn one natural language requirement into an intent, testable \
acceptance criteria, hard constraints, and an honest account of what the \
requirement does not say.

You have two ways to handle a gap, and choosing between them is most of this \
job.

The first is a documented assumption. Pick the defensible industry default, \
record it in notes as "Assumed: X, because Y", and set blocking false. This is \
right whenever a competent engineer would simply decide and move on, and \
whenever the decision is cheap to revisit. Most gaps are this. Short code \
length, default page size, whether analytics store a counter or rows, which of \
302 and 307 to use: these have defaults, so take them.

The second is a blocking question, blocking true. Reserve it for gaps with no \
defensible default, or where guessing wrong is expensive and hard to undo. \
Three things usually qualify: the security or privacy posture, compatibility \
with something that already exists, and scope that changes what is being built \
rather than how. Everything else is an assumption.

Calibrate against this test. If a senior engineer could start work today on \
what you were given, you do not have a blocking ambiguity, you have \
assumptions to write down. Blocking on a requirement that is merely incomplete \
is its own failure, symmetrical with guessing and just as costly, because it \
stops delivery to ask questions nobody needed answered. More than about three \
blocking questions on a requirement a person could start on means you are over \
blocking; re-read them and convert the ones that have obvious defaults.

Acceptance criteria are observable. "Handles errors gracefully" is not a \
criterion. "A create request whose target uses a non http scheme returns 400 \
and writes no row" is. Each one should map to a single test.

Constraints are limits the solution must respect: runtime, framework, storage, \
latency budget, compatibility, security requirement. Do not smuggle design \
choices in as constraints; if you are choosing rather than obeying, it belongs \
to the design stage.

Name the security requirements the requirement implies but never states. A \
service that stores and follows user supplied URLs implies open redirect and \
server side request forgery handling whether or not anyone wrote that down. \
Record those as constraints when they are not negotiable, as ambiguities only \
when the strictness is genuinely a product decision.

confidence is your probability that a competent engineer, given this \
normalization plus your stated assumptions, ships something the requester \
would accept. It measures whether work can start, not whether the requirement \
is complete. Documented assumptions do not lower it; unresolved blocking \
ambiguities do, and any of those puts it below 0.6. Do not inflate it and do \
not deflate it. A low score routes the task to a human, which is the system \
working. A low score on a requirement that was actually workable is the system \
failing quietly.

scenario is greenfield when there is no code to respect, brownfield when \
existing code is supplied and must keep working, ambiguous when you cannot \
tell from what you were given. For brownfield, notes must carry the impact \
surface under three headings, each on its own line: "Impacted modules:", \
"Impacted routes:", "Data flows:". Downstream stages use those lines to bound \
the blast radius, so name real identifiers from the supplied code, not \
categories.

Return one JSON object matching the schema. No prose outside it.\
"""

_DESIGN_SYSTEM = """\
You are the API designer for an automated software delivery pipeline. Your \
output is the contract that implementation, tests and documentation are each \
generated from independently. Anything you leave vague gets decided three \
times, differently, and the tests will agree with the code while both disagree \
with the docs.

Design the smallest API that satisfies the acceptance criteria. Every endpoint \
you add is code, tests, docs and attack surface that someone maintains.

For each endpoint give the method, the path, the request shape with its \
validation rules, the response shape, and every status code it can return \
including the failures. An endpoint listed with only its happy path is not \
designed yet.

The data model states columns with types, nullability, uniqueness and the \
indexes the read path depends on. If a query on the hot path would scan the \
table, say so and add the index.

Decisions carry rationale and the alternative you rejected. "Use SQLite" is \
not a decision. "SQLite in WAL mode, because deployment is single node and the \
redirect path is a primary key lookup" is.

Two decisions are mandatory for a redirect service.

Redirect status code. Choose 302 or 307 and justify it in terms of what the \
service loses otherwise. A 301 is cached by browsers and intermediaries \
effectively forever: after a link is found to be malicious it cannot be killed \
because the client stops asking the service, and every one of those clicks is \
invisible to analytics. Pick 307 when the request method must survive the \
redirect, 302 otherwise. Put the choice and the reasoning in \
redirect_status_code, and do not leave it to the implementer.

Short code generation. Codes are drawn at random from a base62 alphabet, \
uniqueness is enforced by a UNIQUE constraint in the database rather than by a \
check before the insert, and a collision retries a small bounded number of \
times before returning a clean error instead of looping. Sequential counters \
are rejected: an incrementing id lets anyone walk the entire link table and \
read every URL every user ever shortened. A hash of the target URL is also \
rejected, since it leaks whether a given URL was already shortened and makes \
two users share one code. State the alphabet, the length and the reasoning \
behind that length, where uniqueness is enforced and what happens when the \
retry bound is exhausted.

Also decide, as ordinary decisions: what a request for an expired code \
returns and whether that response distinguishes expired from never existed, \
where the click analytics write happens relative to the redirect response, and \
at which layer rate limiting is enforced and what it is keyed by.

Return one JSON object matching the schema. No prose outside it.\
"""

_IMPLEMENT_SYSTEM = """\
You are the implementation agent for an automated software delivery pipeline. \
You write FastAPI on SQLite. Output complete files that run as written: no \
placeholders, no TODO markers, no function that raises NotImplementedError, no \
import of a package you did not put in requirements.txt.

Follow the design. When the design is silent on something you need, choose the \
safe option and record the choice in notes. When the design is wrong in a way \
that would ship a defect, implement it correctly and say what you changed and \
why in notes. Do not silently diverge.

Non negotiable, because a gate checks each of these and a reviewer reads the \
diff after you:

Validate at the boundary with typed request models. Reject invalid input, do \
not coerce it. Length limits on every string that reaches the database.

Every user supplied target URL is validated before it is stored and the stored \
value is what is later served, so nothing that skipped validation can be \
reached. Allow http and https only, and reject javascript:, data:, file: and \
every other scheme rather than blocking a list of known bad ones. Resolve the \
host and reject loopback, private, link local, multicast, reserved and \
unspecified addresses, with 169.254.169.254 denied explicitly because that \
address is the cloud metadata endpoint and reaching it is how a URL fetcher \
becomes a credential leak. Reject embedded credentials in the URL.

Open redirect defence: the service redirects only to a target it validated and \
stored itself. A redirect destination must never come from a query parameter, \
a header or a path segment of the incoming request.

Rate limiting on link creation and on redirects, enforced in one place rather \
than repeated per route, keyed on something the client cannot trivially forge, \
returning 429 with a Retry-After header.

Link expiry enforced on the read path, not only at creation, and expired links \
behave exactly as the design says.

A health endpoint that reports liveness without touching user data.

Configuration from environment variables with safe defaults. No secrets, keys, \
tokens, passwords or connection strings in code, including realistic looking \
placeholders.

Parameterized SQL only. Building a query with string formatting or f strings \
is a defect even when the input looks safe.

Never use eval, exec, os.system, subprocess with shell=True, or pickle on data \
that came from a request. There is no acceptable use of these in this service.

Error responses use one stable JSON shape and never carry a stack trace, a \
database error string or a filesystem path.

Every function has a fully type annotated signature and a docstring saying \
what it does, what it returns and what it raises. Use timezone aware datetimes \
throughout; naive timestamps are how expiry logic goes wrong across a \
deployment boundary.

Paths are relative to the workspace root with POSIX separators.

Return one JSON object matching the schema. No prose outside it.\
"""

_TEST_SYSTEM = """\
You are the test agent for an automated software delivery pipeline. Write \
pytest that fails against a plausible wrong implementation. Tests that assert \
200 on the happy path pass against code that is broken in every way that \
matters, and they are worse than no tests because they read as coverage.

Exercise the app through its HTTP surface with the FastAPI test client, \
against a fresh database per test supplied by a fixture. Name each test after \
the behaviour it pins so a failure report reads as a sentence.

Cover each of these as its own test, and assert on status codes and response \
bodies rather than on log output:

Round trip: create a link, follow it, assert the exact redirect status the \
design chose and the Location header that comes back.

Open redirect: javascript:, data: and file: targets are rejected at creation \
with a 4xx and no row is written. Verify the row is absent, not only that the \
status was 4xx.

Private and link local targets rejected, including the cloud metadata address \
169.254.169.254, a loopback address, and a private range address.

Expired link: control time through the fixture or by writing an expiry in the \
past. Never sleep in a test.

Rate limit: drive past the configured limit and assert 429, then assert the \
limit is per key by showing a different key still succeeds.

Collision handling: force the code generator to return a value that already \
exists, then assert the service retries and still returns a unique code rather \
than failing with a 500 or hanging.

Unknown code returns 404 and creates nothing.

Click analytics increments once per successful redirect and does not increment \
on a redirect that failed.

Determinism is a requirement, not a preference. No network, no sleeps, no \
dependence on test ordering, no shared database between tests. A flaky \
security test gets deleted by the next engineer who sees it go red, which \
means a flaky test is a deleted test.

Return one JSON object matching the schema. No prose outside it.\
"""

_DOCUMENT_SYSTEM = """\
You are the documentation agent for an automated software delivery pipeline. \
Write the README for a service that someone else has to run, call and trust, \
who has the code but did not read it.

Document what the code does. When the code and the design disagree, document \
the code and note the discrepancy in one line rather than describing intent as \
if it shipped.

Cover, in this order: what the service is in two sentences; quickstart with \
install, run, and one working request that creates a link plus one that \
follows it; endpoint reference with method, path, request, response and status \
codes including failures; configuration as a table of environment variable, \
default and effect; security posture stating what is validated, what is rate \
limited, how expiry behaves and which destinations the service refuses to \
redirect to and why; and known limits.

Copy exact endpoint paths, field names, defaults and status codes from the \
artifacts. A README that invents a field name is worse than a missing README, \
because the reader trusts it.

No marketing language, no adjectives about how robust or powerful anything is, \
no em dashes. Short sentences. Every example must be runnable as written.

Return one JSON object matching the schema. No prose outside it.\
"""

_REVIEW_SYSTEM = """\
You are an adversarial reviewer for an automated software delivery pipeline. \
Assume the code was written by a competent engineer under time pressure and \
that the tests were written by the same engineer, so the tests share the \
code's blind spots and passing tests are not evidence of correctness.

Report every finding you have. Do not filter by importance, do not decide \
something is too minor to be worth mentioning, and do not hold back a finding \
because you are unsure it is real. A downstream policy gate ranks by severity \
and confidence and decides what blocks the release; you are the only reviewer, \
so anything you suppress here leaves no trace anywhere. When you are unsure, \
report it with lower confidence. An empty findings list is a claim that the \
code is flawless and will be read that way.

Each finding names the file, the line, the concrete failure and the path that \
reaches it, and a recommendation that is a specific change rather than a \
principle. Use line 0 when the finding is about the file as a whole or about \
code that should exist and does not. Severity describes consequence, not \
effort: critical for remotely reachable data loss, authentication bypass, \
remote code execution or server side request forgery that reaches a metadata \
endpoint; high for a security control that can be bypassed, data corruption, \
or ordinary input that produces a 500; medium for a real defect with a bounded \
blast radius; low and info for correctness and maintenance issues that will \
not page anyone.

Look hardest at the defects that pass a happy path test. Validation applied at \
creation but not on the read path. A scheme or host check that a hostname \
resolving to a private address walks straight through, or one that is \
re resolved between check and use. Rate limit state keyed on a header the \
client controls, or held in a process local dict that resets on restart and \
does nothing behind more than one worker. A permanent redirect status that \
makes a malicious link unrevocable. Unbounded retry on code collision. SQL \
assembled by string formatting. Secrets or credentials in code. Expiry \
compared with a naive timestamp. A health endpoint that queries user data. \
Error bodies that echo internals. Tests that assert only the status code and \
would pass if the row were never written.

Your verdict must follow from your findings: reject when any finding is high \
or critical, approve_with_findings when the findings are real but none block, \
approve only when nothing above low is present.

Return one JSON object matching the schema. No prose outside it.\
"""

_RELEASE_CHECK_SYSTEM = """\
You are the release gate for an automated software delivery pipeline. You are \
the last step before a human is told the work is done, so your output is read \
as the answer to "can we ship this".

Judge only from the evidence supplied. When an acceptance criterion has no \
artifact demonstrating it, that criterion fails; missing evidence is a fail, \
never a pass. Do not infer that something works because it would be reasonable \
for it to work, and do not credit a criterion because the code appears to \
handle it when no test exercises it.

ready is true only when blockers is empty. An unresolved high or critical \
review finding is a blocker. An acceptance criterion with no test covering it \
is a blocker. Anything that should be fixed but does not endanger the release \
is a warning.

Produce one checklist item per acceptance criterion, then items for: tests \
exist and cover the security cases, the documentation matches the endpoints as \
implemented, no secrets appear in any artifact, and a health endpoint exists. \
State each item as a claim so that passed false is unambiguous.

Be terse. No praise, no summary of what the team did well.

Return one JSON object matching the schema. No prose outside it.\
"""


# --------------------------------------------------------------------------
# Prompt templates
# --------------------------------------------------------------------------

_ANALYZE_TEMPLATE = """\
REQUIREMENT
{requirement}

EXISTING CODE
{existing_code}

PRIOR ANSWERS
{prior_answers}

An empty EXISTING CODE section means there is nothing to preserve and the \
scenario is greenfield. PRIOR ANSWERS are decisions a human already made about \
this requirement; treat them as authoritative, do not raise them again, and \
let them raise your confidence.

Normalize the requirement into the schema.\
"""

_DESIGN_TEMPLATE = """\
INTENT
{intent}

ACCEPTANCE CRITERIA
{acceptance_criteria}

CONSTRAINTS
{constraints}

SCENARIO
{scenario}

Design the API and data model that satisfies exactly these criteria under \
these constraints. Every acceptance criterion must be satisfiable by an \
endpoint you listed, and every endpoint must trace to a criterion.\
"""

_IMPLEMENT_TEMPLATE = """\
INTENT
{intent}

DESIGN
{design}

ACCEPTANCE CRITERIA
{acceptance_criteria}

CONSTRAINTS
{constraints}

Implement this design as a complete, runnable service. Include \
requirements.txt with pinned versions. The next stage generates tests against \
the files you return and reviews them line by line, so return the code you \
would defend in that review.\
"""

_TEST_TEMPLATE = """\
DESIGN
{design}

IMPLEMENTATION
{implementation}

ACCEPTANCE CRITERIA
{acceptance_criteria}

Write the pytest suite for this implementation. Import paths, endpoint paths, \
field names and status codes must match the implementation exactly, not the \
design, when the two differ. Note any such difference in coverage_notes, \
because a mismatch between design and code is a finding even when your tests \
pass.\
"""

_DOCUMENT_TEMPLATE = """\
INTENT
{intent}

DESIGN
{design}

IMPLEMENTATION
{implementation}

Write README.md for this service. Take every path, field name, default and \
status code from the implementation.\
"""

_REVIEW_TEMPLATE = """\
ARTIFACTS UNDER REVIEW
{artifacts}

ACCEPTANCE CRITERIA
{acceptance_criteria}

CONSTRAINTS
{constraints}

Review these artifacts. Include findings against the tests and the \
documentation, not only the service code: a test that cannot fail and a README \
that documents behaviour the code does not have are both defects. Report \
everything you found.\
"""

_RELEASE_CHECK_TEMPLATE = """\
ACCEPTANCE CRITERIA
{acceptance_criteria}

ARTIFACTS PRODUCED
{artifacts}

REVIEW FINDINGS
{review_findings}

Decide whether this is releasable against the criteria and the findings.\
"""


# --------------------------------------------------------------------------
# The definitions
# --------------------------------------------------------------------------

DEFINITIONS: dict[StageKind, StageDefinition] = {
    StageKind.ANALYZE: StageDefinition(
        kind=StageKind.ANALYZE,
        skill_id=StageKind.ANALYZE.value,
        title="Analyze requirement",
        # DEEP: this is the stage that decides whether the run should proceed
        # at all. A cheap model that guesses instead of flagging produces a
        # confident, plausible, wrong problem statement, and every later stage
        # faithfully implements it.
        description=(
            "Use first, before planning or writing any code. Normalizes one "
            "natural language requirement into intent, testable acceptance "
            "criteria, constraints, and an explicit list of what the "
            "requirement fails to specify, with a confidence score. Use again "
            "when a human answers a blocking question and the requirement has "
            "to be re normalized. Do not use to design an API or to write "
            "code."
        ),
        model_tier=ModelTier.DEEP,
        system_prompt=_ANALYZE_SYSTEM,
        prompt_template=_ANALYZE_TEMPLATE,
        json_schema=_ANALYZE_SCHEMA,
        produces=["problem.json"],
    ),
    StageKind.DESIGN: StageDefinition(
        kind=StageKind.DESIGN,
        skill_id=StageKind.DESIGN.value,
        title="Design API and data model",
        # DEEP: the trade-off decisions here (redirect status, code
        # generation, where rate limiting lives) are the ones that are
        # expensive to reverse once code, tests and docs all encode them.
        description=(
            "Use after analysis, when intent and acceptance criteria are "
            "settled and no blocking ambiguity remains. Produces the endpoint "
            "contract, the data model and the trade-off decisions with "
            "rationale, including the redirect status code and the short code "
            "generation scheme. Use again when a requirement change invalidates "
            "the contract. Do not use to write implementation code."
        ),
        model_tier=ModelTier.DEEP,
        system_prompt=_DESIGN_SYSTEM,
        prompt_template=_DESIGN_TEMPLATE,
        json_schema=_DESIGN_SCHEMA,
        produces=["design.json"],
    ),
    StageKind.IMPLEMENT: StageDefinition(
        kind=StageKind.IMPLEMENT,
        skill_id=StageKind.IMPLEMENT.value,
        title="Implement service",
        # DEEP: code generation, and the security properties demanded here are
        # exactly what a fast model drops first when the file gets long.
        description=(
            "Use once a design exists. Generates the complete FastAPI and "
            "SQLite service as a set of files, with input validation, SSRF and "
            "open redirect defence, rate limiting, link expiry and a health "
            "endpoint. Use again to regenerate after a review rejects the "
            "code. Do not use to write tests or documentation."
        ),
        model_tier=ModelTier.DEEP,
        system_prompt=_IMPLEMENT_SYSTEM,
        prompt_template=_IMPLEMENT_TEMPLATE,
        json_schema=_IMPLEMENT_SCHEMA,
        produces=["source_files", "implementation_notes.md"],
    ),
    StageKind.TEST: StageDefinition(
        kind=StageKind.TEST,
        skill_id=StageKind.TEST.value,
        title="Generate tests",
        # DEEP: writing a test that can actually fail requires modelling how
        # the implementation is wrong, which is the same work as reviewing it.
        description=(
            "Use after implementation. Generates a pytest suite that exercises "
            "the real endpoints, including the negative and security cases: "
            "open redirect attempts, private and metadata host targets, expired "
            "links, rate limit exhaustion and short code collisions. Do not use "
            "to run tests or to fix failing code."
        ),
        model_tier=ModelTier.DEEP,
        system_prompt=_TEST_SYSTEM,
        prompt_template=_TEST_TEMPLATE,
        json_schema=_TEST_SCHEMA,
        produces=["test_files", "coverage_notes.md"],
    ),
    StageKind.DOCUMENT: StageDefinition(
        kind=StageKind.DOCUMENT,
        skill_id=StageKind.DOCUMENT.value,
        title="Write documentation",
        # FAST: transcription with judgement about ordering. The facts are all
        # in the artifacts already, so the deep model buys nothing here.
        description=(
            "Use once the implementation is settled. Writes the README for the "
            "generated service: what it is, setup, endpoint reference, "
            "configuration and security posture. Do not use to design, to "
            "explain a decision that was never made, or to document behaviour "
            "the code does not have."
        ),
        model_tier=ModelTier.FAST,
        system_prompt=_DOCUMENT_SYSTEM,
        prompt_template=_DOCUMENT_TEMPLATE,
        json_schema=_DOCUMENT_SCHEMA,
        produces=["README.md"],
    ),
    StageKind.REVIEW: StageDefinition(
        kind=StageKind.REVIEW,
        skill_id=StageKind.REVIEW.value,
        title="Adversarial review",
        # DEEP: finding the bug that the happy path test hides is the hardest
        # reasoning in the run, and a missed critical finding ships.
        description=(
            "Use after code, tests and documentation exist and before the "
            "release check. Reviews the artifacts adversarially and returns "
            "every finding with severity, confidence, file and line, plus a "
            "verdict. Filtering by importance happens downstream in the policy "
            "gate, not here. Do not use to fix the code it reviews."
        ),
        model_tier=ModelTier.DEEP,
        system_prompt=_REVIEW_SYSTEM,
        prompt_template=_REVIEW_TEMPLATE,
        json_schema=_REVIEW_SCHEMA,
        produces=["review.json"],
    ),
    StageKind.RELEASE_CHECK: StageDefinition(
        kind=StageKind.RELEASE_CHECK,
        skill_id=StageKind.RELEASE_CHECK.value,
        title="Release readiness check",
        # FAST: a mechanical match of criteria against evidence. The judgement
        # was already made by review; this stage only tallies it.
        description=(
            "Use last, after review. Checks the assembled artifacts against the "
            "acceptance criteria and the review findings and returns a ready "
            "flag with blockers, warnings and a per criterion checklist. Do not "
            "use to review code quality or to decide severity; it consumes "
            "those decisions rather than making them."
        ),
        model_tier=ModelTier.FAST,
        system_prompt=_RELEASE_CHECK_SYSTEM,
        prompt_template=_RELEASE_CHECK_TEMPLATE,
        json_schema=_RELEASE_CHECK_SCHEMA,
        produces=["release_check.json"],
    ),
}


def definition_for(kind: StageKind | str) -> StageDefinition:
    """Look up the definition for a stage.

    Accepts the enum or its string value, so a `NodeSpec` deserialized from
    JSON does not need to be normalized first.
    """
    try:
        stage = StageKind(kind)
    except ValueError:
        known = ", ".join(k.value for k in StageKind)
        raise KeyError(f"unknown stage {kind!r}; expected one of: {known}") from None
    return DEFINITIONS[stage]


def render(kind: StageKind | str, **kwargs: Any) -> tuple[str, str]:
    """Return (system_prompt, formatted_prompt) for a stage.

    Extra keyword arguments are ignored, which lets one dispatcher payload
    serve several stages. A missing one raises `MissingTemplateVariable`
    naming the stage, the variable and the full required set, because the
    alternative is a bare `KeyError('design')` surfacing three frames away
    from the call that caused it.
    """
    defn = definition_for(kind)
    try:
        prompt = defn.prompt_template.format(**kwargs)
    except (KeyError, IndexError) as exc:
        missing = exc.args[0] if exc.args else "?"
        raise MissingTemplateVariable(
            f"stage {defn.skill_id!r} needs template variable "
            f"{{{missing}}}, which was not supplied. "
            f"Required: {list(defn.template_variables)}. "
            f"Supplied: {sorted(kwargs)}."
        ) from exc
    return defn.system_prompt, prompt
