# A2A conformance

What this project implements of the Agent2Agent protocol, what it deliberately does not, and where the line sits between the protocol and the governance layer built on top of it.

Written plainly because overstating protocol compliance is the kind of claim that gets checked.

Target: **A2A v1.0**, via `a2a-sdk` 1.1.2.

## Implemented

| Area | Detail |
| --- | --- |
| Agent Cards | Every lifecycle stage serves one at `/.well-known/agent-card.json`, with skills, capabilities and security schemes. |
| Transport | JSON-RPC 2.0 over HTTP, the baseline binding. |
| Streaming | `message/stream` over SSE. Stage progress reaches the orchestrator as it happens. |
| Task lifecycle | The SDK's own `TaskState` enum, all eight states. |
| `input_required` | Load-bearing, not decorative. An underspecified requirement or a pending approval parks here. |
| `tasks/cancel` | Real cancellation, used for safe stop. |
| Artifacts | Stage output is returned as A2A artifacts. |
| Auth | Bearer token declared in the card's security schemes and enforced server-side. Verified: correct token completes, wrong token and missing token both return 401. |
| Discovery | The planner reads skills from resolved cards, so adding an agent to the mesh makes its capability available without a code change. |

## Not implemented

| Area | Why |
| --- | --- |
| gRPC and HTTP+JSON bindings | JSON-RPC is sufficient for a local mesh. The SDK supports all three; wiring another is configuration, not design. |
| Push notification configs | The orchestrator holds the stream open for the life of a task. Webhooks matter when tasks outlive a connection, which is a deployment concern rather than a protocol one. |
| Extended Agent Card | No capability here needs to be hidden behind authentication. |
| Multi-tenant routing | The `tenant` field is unused. Every agent serves one workspace. |
| TLS | The mesh binds loopback. Correct for a local demo, wrong for anything else. |
| Cross-organisation federation | Every agent is ours. The interesting case is agents you do not control, and this does not demonstrate that. |

## Where the protocol stops

A2A standardises how agents find each other and talk. It says nothing about how work is governed, and that is a deliberate scope choice by its authors rather than an oversight.

Concretely, none of the following are in the protocol, and all of them are the subject of this project:

- an explicit dependency graph with entry and exit gates
- parallel execution with synchronization barriers
- bounded retry, fallback, rollback
- policy evaluation for security, compliance and change control
- decision lineage across stages
- reliability metrics
- re-planning when upstream evidence changes

The protocol does contribute real primitives to that layer. `input_required` gives human-in-the-loop a state that any A2A client understands rather than a bespoke pause mechanism. `tasks/cancel` makes safe stop protocol-enforced instead of a flag someone polls. Card-declared security schemes carry auth. Everything else above is ours.

This gap is documented in the literature as well as in practice; see the arXiv paper *Governance Gaps in Agent Interoperability Protocols: What MCP, A2A, and ACP Cannot Express* (2606.31498).

## Verified, not asserted

The claims above were checked rather than assumed:

- A standalone smoke test stands up an agent, fetches its card over HTTP, streams a task through `submitted -> working -> completed` with artifacts, parks a second task in `input_required`, and cancels a third.
- Removing the `pb.Task` enqueue from the executor produces exactly `InvalidAgentResponseError: Agent should enqueue Task before TaskStatusUpdateEvent` across five tests, confirming the tests observe the protocol rather than our own wrapper.
- Stripping the security scheme from a card causes five auth tests to fail, confirming the bearer token is sent because the card asks for it rather than because it is hardcoded.

## Gotchas found along the way

Recorded because they cost real time and are not in the documentation.

**The `Task` must be enqueued before any status update.** `TaskUpdater.submit()` alone is not enough. When `context.current_task` is `None` the executor must enqueue a `pb.Task` first, or the SDK rejects the stream.

**The served Agent Card is a hybrid document.** The card route emits protobuf-JSON alongside flattened v0.3 aliases, so strict `json_format.ParseDict` fails on it with `has no field named "bearerFormat"`. Use `A2ACardResolver`, which is the path `create_client` takes internally. This only appears once a card declares security schemes, which is why a simpler smoke test never hits it.

**Types are protobuf, not pydantic.** Construction, field access and `WhichOneof` all follow protobuf semantics.

**`SecurityRequirement.schemes` is a map to `StringList`.** An unscoped bearer requirement is `{"keel_bearer": pb.StringList(list=[])}`.
