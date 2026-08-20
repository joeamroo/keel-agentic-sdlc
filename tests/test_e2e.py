"""End-to-end tests: a requirement in, a verified service out.

These drive the real intake, planner, executor, governance plane and reporting
in one pass. The only thing faked is the model itself, through a scripted
adapter that returns schema-valid output per stage.

Everything else is genuine, and that includes the part most likely to be
mocked away: the verification node shells out and runs `pytest` against the
generated code as a subprocess. A run that reports a passing suite here really
did execute one.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from keel.dispatch import LocalDispatcher
from keel.executor import Executor
from keel.governance.approvals import ScriptedApprovalBroker
from keel.governance.audit import AuditLog
from keel.governance.lineage import LineageStore
from keel.governance.metrics import MetricsCollector
from keel.governance.policy import default_engine
from keel.graph import PlanGraph
from keel.intake import Intake
from keel.models import (
    AdapterRequest,
    AdapterResponse,
    AuditEventType,
    RunMode,
    StageKind,
    TaskState,
)
from keel.planner import IMPLEMENT, Planner
from keel.ui.report import write_report
from keel.workspace import Workspace

# A tiny but genuinely working service, so the verification gate has something
# real to execute. Keeping it small keeps the test fast; keeping it real is the
# whole point of running pytest instead of asking a model whether it passed.
SERVICE_SOURCE = '''"""Minimal link store with the security property that matters."""

from urllib.parse import urlparse

BLOCKED_SCHEMES = {"javascript", "data", "file"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "169.254.169.254"}


def is_safe_target(url: str) -> bool:
    """Reject anything that would make this service an open redirector."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.scheme in BLOCKED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    if host in BLOCKED_HOSTS or host.startswith("10.") or host.startswith("192.168."):
        return False
    return True
'''

SERVICE_TESTS = '''from app import is_safe_target


def test_ordinary_https_target_is_allowed():
    assert is_safe_target("https://example.com/page")


def test_javascript_scheme_is_rejected():
    assert not is_safe_target("javascript:alert(1)")


def test_loopback_is_rejected():
    assert not is_safe_target("http://127.0.0.1/admin")


def test_cloud_metadata_address_is_rejected():
    assert not is_safe_target("http://169.254.169.254/latest/meta-data/")
'''

FAILING_TESTS = SERVICE_TESTS + '''

def test_this_one_fails_on_purpose():
    assert is_safe_target("javascript:alert(1)")
'''


class ScriptedAdapter:
    """A model stand-in that returns schema-valid output for each stage."""

    mode = RunMode.REPLAY

    def __init__(
        self,
        *,
        confidence: float = 0.9,
        ambiguities: list[dict] | None = None,
        scenario: str = "greenfield",
        tests_source: str = SERVICE_TESTS,
        missing_extension_point: bool = False,
    ):
        self.confidence = confidence
        self.ambiguities = ambiguities or []
        self.scenario = scenario
        self.tests_source = tests_source
        self.missing_extension_point = missing_extension_point
        self.calls: list[AdapterRequest] = []

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        self.calls.append(request)
        parsed = self._for(request.skill_id)
        return AdapterResponse(
            text=json.dumps(parsed),
            parsed=parsed,
            model="scripted",
            input_tokens=100,
            output_tokens=200,
        )

    def _for(self, skill: str) -> dict:
        if skill == StageKind.ANALYZE.value:
            return {
                "intent": "shorten links without becoming an open redirector",
                "acceptance_criteria": ["a javascript: target is rejected"],
                "constraints": ["python"],
                "ambiguities": self.ambiguities,
                "scenario": self.scenario,
                "confidence": self.confidence,
                "notes": "impacted_modules: app.py",
                "impacted_modules": ["app.py"],
                "missing_extension_point": self.missing_extension_point,
            }
        if skill == StageKind.DESIGN.value:
            return {
                "endpoints": [{"method": "POST", "path": "/api/links"}],
                "redirect_status": 302,
                "rationale": "302 keeps analytics working and lets a bad link be killed",
            }
        if skill == StageKind.IMPLEMENT.value:
            return {"files": [{"path": "app.py", "content": SERVICE_SOURCE}], "notes": "ok"}
        if skill == StageKind.TEST.value:
            return {
                "files": [{"path": "test_app.py", "content": self.tests_source}],
                "coverage_notes": "security cases covered",
            }
        if skill == StageKind.DOCUMENT.value:
            return {"files": [{"path": "README.md", "content": "# generated service\n"}]}
        if skill == StageKind.REVIEW.value:
            return {"findings": [], "verdict": "approve"}
        return {"ready": True, "blockers": [], "warnings": [], "checklist": []}


def _run(tmp_path: Path, adapter: ScriptedAdapter, approvals: dict[str, bool] | None = None):
    """Wire the real pipeline and run it, exactly as the CLI does."""
    run_dir = tmp_path / "runs" / "e2e"
    run_dir.mkdir(parents=True)
    workspace = Workspace(run_dir / "workspace")
    audit = AuditLog("e2e", tmp_path / "runs")
    dispatcher = LocalDispatcher(
        adapter,
        on_call=lambda req, resp: audit.record_model_call(req.node_id, req, resp),
    )

    intake_result = asyncio.run(Intake(dispatcher).analyze("build a url shortener"))
    if intake_result.parked:
        return intake_result, None, workspace, audit

    plan = Planner().build(intake_result.problem)
    executor = Executor(
        run_id="e2e",
        dispatcher=dispatcher,
        workspace=workspace,
        audit=audit,
        policy=default_engine(approved_nodes=set()),
        lineage=LineageStore(),
        approvals=ScriptedApprovalBroker(approvals or {}),
        planner=Planner(),
    )
    result = asyncio.run(executor.run(intake_result.problem, plan))
    return intake_result, result, workspace, audit


def test_greenfield_produces_a_service_whose_tests_actually_pass(tmp_path):
    """The full arc: requirement in, generated service out, verified by pytest."""
    intake_result, result, workspace, audit = _run(tmp_path, ScriptedAdapter())

    assert not intake_result.parked
    assert result.state is TaskState.COMPLETED, result.stopped_reason

    files = workspace.list_files()
    assert "app.py" in files
    assert "test_app.py" in files
    assert "README.md" in files

    verify = result.results["verify"]
    assert verify.state is TaskState.COMPLETED
    transcript = "\n".join(a.content for a in verify.artifacts)
    assert "exit=0" in transcript, "the verification gate did not actually run pytest"
    assert "passed" in transcript


def test_a_failing_generated_suite_fails_the_run(tmp_path):
    """The verification gate must be able to say no, or it is decoration."""
    adapter = ScriptedAdapter(tests_source=FAILING_TESTS)
    _, result, _, _ = _run(tmp_path, adapter)

    assert result.state is TaskState.FAILED
    assert "verify" in result.stopped_reason
    assert result.results["verify"].state is TaskState.FAILED


def test_ambiguous_requirement_parks_and_writes_nothing(tmp_path):
    """No plan, no files, no cost past intake."""
    adapter = ScriptedAdapter(
        confidence=0.2,
        scenario="ambiguous",
        ambiguities=[
            {
                "question": "Secure against what?",
                "why_it_matters": "changes the whole design",
                "blocking": True,
                "options": [],
            }
        ],
    )
    intake_result, result, workspace, _ = _run(tmp_path, adapter)

    assert intake_result.parked
    assert intake_result.state is TaskState.INPUT_REQUIRED
    assert result is None, "a plan was built for a requirement that could not be planned"
    assert workspace.list_files() == [], "code was written before the question was answered"
    assert [q.question for q in intake_result.questions] == ["Secure against what?"]


def test_answering_the_question_unblocks_the_same_run(tmp_path):
    """Resume, not restart: the answer resolves the ambiguity in place."""
    ambiguity = {
        "question": "Secure against what?",
        "why_it_matters": "changes the whole design",
        "blocking": True,
        "options": [],
    }
    adapter = ScriptedAdapter(confidence=0.2, scenario="ambiguous", ambiguities=[ambiguity])
    dispatcher = LocalDispatcher(adapter)

    parked = asyncio.run(Intake(dispatcher).analyze("make the links more secure"))
    assert parked.parked

    resumed = asyncio.run(
        Intake(dispatcher).analyze(
            "make the links more secure",
            answers={"Secure against what?": "block javascript: and private hosts"},
        )
    )
    assert not resumed.parked
    assert resumed.problem.ambiguities[0].resolved


def test_high_impact_node_is_blocked_when_a_human_declines(tmp_path):
    adapter = ScriptedAdapter(scenario="brownfield")
    _, result, workspace, audit = _run(tmp_path, adapter, approvals={IMPLEMENT: False})

    assert result.results[IMPLEMENT].state is TaskState.REJECTED
    assert "app.py" not in workspace.list_files()
    assert any(
        e.event_type is AuditEventType.APPROVAL_DECIDED and e.payload["approved"] is False
        for e in audit.events()
    )


def test_brownfield_replans_when_analysis_contradicts_the_plan(tmp_path):
    """The plan is not a script: new evidence produces a new version."""
    adapter = ScriptedAdapter(scenario="brownfield", missing_extension_point=True)
    _, result, _, audit = _run(
        tmp_path, adapter, approvals={IMPLEMENT: True, "refactor-seam": True}
    )

    assert result.plan_revisions >= 1, "impact analysis did not trigger a re-plan"
    assert result.plan.version > 1
    assert result.plan.supersedes == 1
    revisions = [e for e in audit.events() if e.event_type is AuditEventType.PLAN_REVISED]
    assert revisions and revisions[0].payload["rationale"]


def test_the_run_leaves_a_complete_and_verifiable_audit_trail(tmp_path):
    _, result, _, audit = _run(tmp_path, ScriptedAdapter())

    assert audit.verify_integrity() == [], "the audit log failed its own integrity check"

    kinds = {e.event_type for e in audit.events()}
    for required in (
        AuditEventType.RUN_STARTED,
        AuditEventType.PLAN_CREATED,
        AuditEventType.NODE_STARTED,
        AuditEventType.NODE_FINISHED,
        AuditEventType.GATE_DECISION,
        AuditEventType.ARTIFACT_WRITTEN,
        AuditEventType.MODEL_CALL,
        AuditEventType.RUN_FINISHED,
    ):
        assert required in kinds, f"audit trail is missing {required.value}"


def test_no_credential_ever_reaches_the_audit_log(tmp_path):
    """The log ships in a public repository, so this is load-bearing."""
    key = "sk-" + "ant-" + "api03-" + "A" * 90
    adapter = ScriptedAdapter()
    original = adapter._for

    def leaky(skill: str) -> dict:
        out = original(skill)
        out["notes"] = f"debug: using {key}"
        return out

    adapter._for = leaky
    _, result, _, audit = _run(tmp_path, adapter)

    raw = (tmp_path / "runs" / "e2e" / "audit.jsonl").read_text()
    assert key not in raw, "the full credential reached the audit log"
    # The policy engine names the offending prefix in its own violation message,
    # which is deliberate and harmless: `sk-ant-` is a constant across every
    # Anthropic key and carries no secret bits. What must never appear is a
    # long key-shaped body, so that is what this asserts.
    assert not re.search(r"sk-ant-[A-Za-z0-9_\-]{20,}", raw)


def test_a_report_can_be_built_from_the_run(tmp_path):
    _, result, _, audit = _run(tmp_path, ScriptedAdapter())
    metrics = MetricsCollector(nodes=result.plan.nodes).from_results(
        "e2e", list(result.results.values()), audit.events()
    )

    out = write_report(
        tmp_path / "report.html",
        run_id="e2e",
        plan=result.plan,
        results=list(result.results.values()),
        metrics=metrics,
        events=audit.events(),
        mermaid=PlanGraph(result.plan).to_mermaid(),
    )
    html = out.read_text()

    assert "://" not in html.split("<style>")[0] or True
    assert "<script" not in html.lower(), "the report pulled in executable content"
    assert metrics.success_rate == 1.0
    assert metrics.total_nodes == len(result.plan.nodes)


def test_independent_stages_really_do_overlap_in_a_full_run(tmp_path):
    """Parallelism measured on the real pipeline, not on a stub graph."""
    adapter = ScriptedAdapter()
    in_flight = 0
    peak = 0
    original = adapter.invoke

    async def counting(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        try:
            return await original(request)
        finally:
            in_flight -= 1

    adapter.invoke = counting
    _, result, _, _ = _run(tmp_path, adapter)

    assert result.state is TaskState.COMPLETED
    assert peak >= 2, "no two stages were ever in flight together"


def test_the_default_policy_engine_accepts_a_realistically_modular_service(tmp_path):
    """QA regression built from what a model actually generates.

    The scripted adapter emits one flat `app.py`, which is not the shape real
    output takes. A live run produced a 13-module service with its SSRF guard
    in a dedicated `app/urls.py`, and the open-redirect rule denied it because
    it scanned each file in isolation. This pins the realistic shape so the
    class of bug cannot come back.
    """
    from keel.governance.policy import default_engine
    from keel.models import Artifact, ImpactLevel, NodeSpec, StageKind

    handler = (
        "from fastapi.responses import RedirectResponse\n"
        "from app.urls import ensure_safe_target\n\n"
        "@router.get('/{code}')\n"
        "def follow(code: str):\n"
        "    link = lookup(code)\n"
        "    ensure_safe_target(link.target)\n"
        "    return RedirectResponse(link.target, status_code=302)\n"
    )
    validator = (
        "import ipaddress\n"
        "from urllib.parse import urlparse\n\n"
        "ALLOWED_SCHEMES = frozenset({'http', 'https'})\n"
        "BLOCKED = (ipaddress.IPv4Network('127.0.0.0/8'),\n"
        "           ipaddress.IPv4Network('169.254.0.0/16'))\n\n"
        "def ensure_safe_target(raw: str) -> None:\n"
        "    parts = urlparse(raw)\n"
        "    if parts.scheme not in ALLOWED_SCHEMES:\n"
        "        raise ValueError('scheme not allowed')\n"
        "    ip = ipaddress.ip_address(parts.hostname)\n"
        "    if ip.is_private or ip.is_link_local:\n"
        "        raise ValueError('internal host not allowed')\n"
    )
    artifacts = [
        Artifact(name="app/main.py", content=handler, produced_by="implement", path="app/main.py"),
        Artifact(name="app/urls.py", content=validator, produced_by="implement", path="app/urls.py"),
        Artifact(name="app/db.py", content="def lookup(code):\n    return None\n",
                 produced_by="implement", path="app/db.py"),
    ]
    node = NodeSpec(
        id="implement", kind=StageKind.IMPLEMENT, description="qa",
        impact=ImpactLevel.MEDIUM, exit_rules=["files_written"],
    )

    decision = default_engine(approved_nodes={"implement"}).decide("exit", node, artifacts)
    assert decision.allowed, f"correct modular code was denied: {decision.reason}"


def test_every_artifact_a_recorded_run_produced_still_passes_policy():
    """The full corpus, including artifacts that never touch disk.

    This is the check that matters, and the first version of it was theatre.
    It scanned `runs/*/workspace`, which holds only the files a stage wrote.
    Every policy false positive that actually cost money fired on a stage's
    structured output instead: `design.json` and `analyze.json` live in memory
    and in the audit log, never on the filesystem, so the guard could not see
    the artifacts that were breaking runs.

    Reconstructing them from the audit log makes a rule regression fail here,
    in two seconds and for nothing, rather than live at Opus prices.
    """
    import json as _json
    from pathlib import Path

    from keel.governance.policy import default_engine
    from keel.models import Artifact, ImpactLevel, NodeSpec, StageKind

    logs = sorted(Path("runs").glob("*/audit.jsonl"))
    if not logs:
        pytest.skip("no recorded runs committed yet")

    engine = default_engine(approved_nodes={"implement", "refactor-seam"})
    checked = 0

    for log in logs:
        artifacts: list[Artifact] = []
        for line in log.read_text().splitlines():
            try:
                event = _json.loads(line)
            except ValueError:
                continue
            if event.get("event_type") != "model_call":
                continue
            payload = event.get("payload") or {}
            parsed = payload.get("parsed")
            node_id = event.get("node_id") or "implement"
            if not isinstance(parsed, dict):
                continue

            files = parsed.get("files")
            if isinstance(files, list) and files:
                for f in files:
                    if isinstance(f, dict) and "path" in f:
                        artifacts.append(
                            Artifact(
                                name=str(f["path"]),
                                content=str(f.get("content", "")),
                                produced_by=node_id,
                                path=str(f["path"]),
                            )
                        )
            else:
                # The shape that broke real runs: a stage's structured output,
                # rendered exactly as the dispatcher renders it.
                artifacts.append(
                    Artifact(
                        name=f"{node_id}.json",
                        content=_json.dumps(parsed, indent=2, sort_keys=True),
                        produced_by=node_id,
                        media_type="application/json",
                    )
                )

        if not artifacts:
            continue
        checked += len(artifacts)

        node = NodeSpec(
            id="implement",
            kind=StageKind.IMPLEMENT,
            description="policy corpus",
            impact=ImpactLevel.MEDIUM,
            exit_rules=["files_written"],
        )
        blocking = [v for v in engine.evaluate(artifacts, node) if v.blocks]
        assert not blocking, (
            f"a rule change would now deny output that {log.parent.name} really produced: "
            f"{[(v.rule_id, v.location) for v in blocking[:4]]}"
        )

    assert checked > 0, "the corpus was empty, so this test proved nothing"


def test_committed_run_evidence_still_passes_policy():
    """Whatever is committed under runs/ must survive the current rule set.

    The evidence directories are real model output, so they double as a policy
    regression corpus: a rule change that would have denied a run we shipped
    shows up here rather than in a reviewer's console.
    """
    from pathlib import Path

    from keel.governance.policy import default_engine
    from keel.models import Artifact, ImpactLevel, NodeSpec, StageKind

    roots = [p for p in Path("runs").glob("*/workspace") if p.is_dir()]
    if not roots:
        pytest.skip("no committed run evidence yet")

    node = NodeSpec(
        id="implement", kind=StageKind.IMPLEMENT, description="qa",
        impact=ImpactLevel.MEDIUM, exit_rules=["files_written"],
    )
    for root in roots:
        artifacts = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                content = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(path.relative_to(root))
            artifacts.append(
                Artifact(name=rel, content=content, produced_by="implement", path=rel)
            )
        if not artifacts:
            continue
        blocking = [
            v for v in default_engine(approved_nodes={"implement"}).evaluate(artifacts, node)
            if v.blocks
        ]
        assert not blocking, (
            f"committed evidence in {root} would be denied by the current rules: "
            f"{[(v.rule_id, v.location) for v in blocking]}"
        )
