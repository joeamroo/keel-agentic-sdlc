"""Human-in-the-loop approval brokers (§4.4, §4.7: controlled autonomy).

A node whose `impact` is HIGH does not run until a human says so. HIGH is
reserved for the things you cannot take back cheaply: a public API change, a
destructive operation, anything touching the security surface. This module is
the seam where that sign-off is asked for, and it exists as a seam precisely so
the answer can come from a person, a script, or a policy without the
orchestrator knowing or caring which.

Four brokers, one contract:

* `AutoApprovalBroker` for tests and unattended runs.
* `ScriptedApprovalBroker` for reproducible scenario runs and the committed
  demo, where the same input must produce the same run every time.
* `InteractiveApprovalBroker` for a human at a terminal.
* your own, if the approval should come from a ticket queue or a chat bot.

Every broker keeps a `history` of what it was asked and what it answered. The
orchestrator is what writes APPROVAL_REQUESTED and APPROVAL_DECIDED into the
audit log; the history here is the broker's own record, which means an
approval trail survives even when a broker is used standalone.

The default direction of every ambiguous answer is DENY. Empty input, EOF, an
interrupt, an answer nobody anticipated: all deny. For a bank this is the only
defensible posture. A prompt that a script pipes past with a stray newline must
not be read as consent, and an operator who walks away from the terminal has
not approved anything. Fail closed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping

from keel.models import ApprovalDecision, ApprovalRequest, NodeSpec

# Only these are consent. Everything else, including the empty string, is not.
_AFFIRMATIVE: frozenset[str] = frozenset({"y", "yes"})
_NEGATIVE: frozenset[str] = frozenset({"n", "no"})


def needs_approval(node: NodeSpec) -> bool:
    """Whether this node must be signed off before it runs.

    Delegates to `NodeSpec.needs_approval` rather than re-deriving the rule
    from `impact`. One definition of "high impact" in the codebase, in the
    frozen contract where it belongs, so a change to the threshold cannot
    leave the gate and the model disagreeing about what needs a human.
    """
    return node.needs_approval


class ApprovalBroker(ABC):
    """Asks somebody, or something, whether a node may proceed."""

    def __init__(self) -> None:
        self.history: list[tuple[ApprovalRequest, ApprovalDecision]] = []

    @abstractmethod
    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Obtain a decision for one request.

        Implementations must route their answer through `_record` so the
        history is complete no matter which broker is in play.
        """

    def _record(
        self,
        req: ApprovalRequest,
        approved: bool,
        decided_by: str,
        note: str = "",
    ) -> ApprovalDecision:
        """Build the decision, file it, and hand it back."""
        decision = ApprovalDecision(
            request_id=req.request_id,
            approved=approved,
            decided_by=decided_by,
            note=note,
        )
        self.history.append((req, decision))
        return decision

    @property
    def approvals(self) -> list[tuple[ApprovalRequest, ApprovalDecision]]:
        """The subset of history that said yes. Convenient for the run report."""
        return [(q, d) for q, d in self.history if d.approved]

    @property
    def denials(self) -> list[tuple[ApprovalRequest, ApprovalDecision]]:
        """The subset that said no, which is the interesting half in a review."""
        return [(q, d) for q, d in self.history if not d.approved]


class AutoApprovalBroker(ApprovalBroker):
    """Answers every request the same way, without asking anyone.

    For tests and for unattended runs where the operator has already accepted
    the blast radius up front. `approve=False` gives the mirror image, a broker
    that refuses everything, which is how you exercise the safe-stop path
    without needing a person to sit there typing "n".

    It still records every request. An unattended run is exactly the run where
    you will later want to know what it would have asked about.
    """

    def __init__(self, approve: bool = True) -> None:
        super().__init__()
        self.approve = approve

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        verdict = "approved" if self.approve else "denied"
        return self._record(
            req,
            approved=self.approve,
            decided_by="auto",
            note=f"auto-{verdict}, no human consulted",
        )


class ScriptedApprovalBroker(ApprovalBroker):
    """Answers from a fixed script, keyed by node id.

    This is what makes a scenario run reproducible: the same plan and the same
    script produce the same trace on every execution, including the denial
    branches, so the committed demo shows the governance behaviour rather than
    whatever the operator happened to type that afternoon.

    Raises on a node it was not told about. Defaulting to approve would make
    the script a rubber stamp for anything the plan grew since it was written,
    and defaulting to deny would silently rewrite the scenario into a different
    one. An unscripted node means the script is stale, and a stale script
    should fail loudly at the point of use.
    """

    def __init__(self, decisions: Mapping[str, bool]) -> None:
        super().__init__()
        self.decisions = dict(decisions)

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        if req.node_id not in self.decisions:
            known = ", ".join(sorted(self.decisions)) or "(none)"
            raise KeyError(
                f"no scripted approval for node {req.node_id!r}; scripted nodes: {known}"
            )
        approved = self.decisions[req.node_id]
        return self._record(
            req,
            approved=approved,
            decided_by="script",
            note=f"scripted decision for {req.node_id}",
        )


class InteractiveApprovalBroker(ApprovalBroker):
    """Prompts a human at the terminal.

    Shows the node id, the impact level, the reason and the details, because a
    reviewer cannot consent to something described only as "approve node 7?".
    Accepts y/n. Everything else denies:

    * empty input, which is what a bare Enter and a piped newline both produce
    * EOF, which is what a non-interactive stdin produces immediately
    * an interrupt, which is a person backing out
    * anything unrecognized, since a typo is not consent

    `input_fn` and `output_fn` exist for tests that would rather inject than
    patch. When `input_fn` is left as None the builtin is looked up at call
    time rather than captured at construction, so patching `builtins.input`
    works on an already-constructed broker.
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._input_fn = input_fn
        self._output_fn = output_fn

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self._write(self._prompt(req))
        read = self._input_fn if self._input_fn is not None else input

        try:
            raw = read("approve? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            # Nobody is there, or somebody just left. Either way: no consent.
            self._write("\nno input available, denying")
            return self._record(
                req,
                approved=False,
                decided_by="human",
                note="denied by default: no input available",
            )

        answer = raw.strip().lower()
        if answer in _AFFIRMATIVE:
            return self._record(req, approved=True, decided_by="human", note="approved at terminal")
        if answer in _NEGATIVE:
            return self._record(req, approved=False, decided_by="human", note="denied at terminal")
        if not answer:
            return self._record(
                req,
                approved=False,
                decided_by="human",
                note="denied by default: empty input",
            )
        return self._record(
            req,
            approved=False,
            decided_by="human",
            note=f"denied by default: unrecognized answer {answer!r}",
        )

    @staticmethod
    def _prompt(req: ApprovalRequest) -> str:
        lines = [
            "",
            "=" * 60,
            f"APPROVAL REQUIRED  [{req.impact.value.upper()} impact]",
            "=" * 60,
            f"node:    {req.node_id}",
            f"reason:  {req.reason}",
        ]
        if req.details:
            lines.append(f"details: {req.details}")
        lines.append(f"request: {req.request_id}")
        lines.append("-" * 60)
        return "\n".join(lines)

    def _write(self, text: str) -> None:
        (self._output_fn or print)(text)


def broker_for(
    interactive: bool,
    scripted: Mapping[str, bool] | None = None,
) -> ApprovalBroker:
    """Pick a broker for a run.

    The precedence is deliberate. A script is the most specific instruction
    available, so it wins; a human at a terminal beats a blanket auto-approve;
    auto-approve is the last resort and is only reached when the caller has
    explicitly asked for an unattended run.
    """
    if scripted is not None:
        return ScriptedApprovalBroker(scripted)
    if interactive:
        return InteractiveApprovalBroker()
    return AutoApprovalBroker(approve=True)


__all__ = [
    "ApprovalBroker",
    "AutoApprovalBroker",
    "InteractiveApprovalBroker",
    "ScriptedApprovalBroker",
    "broker_for",
    "needs_approval",
]
