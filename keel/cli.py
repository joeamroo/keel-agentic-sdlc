"""Command line entry point.

`keel run --scenario greenfield` is the whole demo. Everything else exists so
that the pieces can be exercised independently, which matters more than it
sounds: a reviewer who wants to poke one stage agent should not have to boot
the entire mesh to do it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from keel import scenarios
from keel.adapters.base import load_env
from keel.dispatch import LocalDispatcher
from keel.executor import Executor
from keel.governance.approvals import (
    AutoApprovalBroker,
    InteractiveApprovalBroker,
    ScriptedApprovalBroker,
)
from keel.governance.audit import AuditLog
from keel.governance.lineage import LineageStore
from keel.governance.metrics import MetricsCollector
from keel.governance.policy import default_engine
from keel.graph import PlanGraph
from keel.intake import Intake
from keel.models import AuditEventType, Plan, RunMode, TaskState, new_run_id
from keel.planner import Planner
from keel.ui.live import LiveView
from keel.ui.report import write_report
from keel.workspace import Workspace

RUNS = Path("runs")


def _adapter(mode: RunMode, replay_from: str | None):
    """Pick where the thinking comes from.

    Replay is the default so that a reviewer with no Anthropic account can run
    every scenario. The recordings it plays back are real model output captured
    from a live run, not fabricated fixtures.
    """
    if mode is RunMode.LIVE:
        from keel.adapters.claude import ClaudeAdapter

        return ClaudeAdapter()

    from keel.adapters.replay import ReplayAdapter, load_cassettes

    if not replay_from:
        raise SystemExit(
            "replay mode needs --replay-from <run_id>, or a committed run in runs/.\n"
            "Record one first with: keel run --scenario greenfield --mode live"
        )
    return ReplayAdapter(load_cassettes(replay_from, RUNS))


def _broker(kind: str, scenario: scenarios.Scenario):
    if kind == "interactive":
        return InteractiveApprovalBroker()
    if kind == "auto":
        return AutoApprovalBroker(approve=True)
    return ScriptedApprovalBroker(dict(scenario.approvals))


async def _run(args: argparse.Namespace) -> int:
    load_env()
    scenario = scenarios.get(args.scenario)
    mode = RunMode(args.mode)
    run_id = args.run_id or new_run_id()

    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(run_dir / "workspace")

    # Brownfield operates on what greenfield produced, which is the only honest
    # way to demonstrate reasoning about an existing codebase.
    if scenario.seed_from:
        seed = _latest_workspace(scenario.seed_from)
        if seed is None:
            raise SystemExit(
                f"scenario {scenario.key!r} needs the {scenario.seed_from!r} run first.\n"
                f"run: keel run --scenario {scenario.seed_from}"
            )
        # Source only. copytree bypasses Workspace, so the stale-bytecode
        # protection in Workspace.write never applies to seeded files, and a
        # copied .pyc carries the seed run's paths: a brownfield traceback then
        # points at the greenfield workspace, which is confusing at exactly the
        # moment you are trying to work out why a test failed.
        shutil.copytree(
            seed,
            workspace.root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        print(f"seeded workspace from {seed}")

    audit = AuditLog(run_id, RUNS)
    lineage = LineageStore()
    policy = default_engine(approved_nodes=set())
    approvals = _broker(args.approvals, scenario)
    adapter = _adapter(mode, args.replay_from)
    dispatcher = LocalDispatcher(
        adapter,
        on_call=lambda req, resp: audit.record_model_call(req.node_id, req, resp),
    )

    print(f"\nrun {run_id}  scenario={scenario.key}  mode={mode.value}")
    print(f"requirement: {scenario.requirement}\n")

    intake = Intake(dispatcher)
    answers = dict(scenario.answers) if args.answer_ambiguities else None
    intake_result = await intake.analyze(
        scenario.requirement,
        existing_code=_existing_code(workspace),
        answers=answers,
    )
    audit.emit(
        AuditEventType.INTAKE,
        {
            "confidence": intake_result.problem.confidence,
            "scenario": intake_result.problem.scenario.value,
            "ambiguities": [a.question for a in intake_result.problem.ambiguities],
            "state": intake_result.state.value,
        },
    )

    if intake_result.parked:
        print("PARKED in input_required. No code was written.\n")
        print("The requirement cannot be planned until these are answered:\n")
        for i, q in enumerate(intake_result.questions, 1):
            print(f"  {i}. {q.question}")
            print(f"     why it matters: {q.why_it_matters}")
        print(f"\nResume with: keel run --scenario {scenario.key} --answer-ambiguities")
        audit.emit(AuditEventType.RUN_FINISHED, {"state": TaskState.INPUT_REQUIRED.value})
        _write_summary(run_dir, run_id, scenario, intake_result, None, None)
        return 0

    plan = Planner().build(intake_result.problem)
    print(f"plan v{plan.version}: {' -> '.join(n.id for n in plan.nodes)}\n")

    view = None if args.no_live else LiveView()
    executor = Executor(
        run_id=run_id,
        dispatcher=dispatcher,
        workspace=workspace,
        audit=audit,
        policy=policy,
        lineage=lineage,
        approvals=approvals,
        planner=Planner(),
        view=view,
    )
    if view:
        view.start()
    try:
        result = await executor.run(intake_result.problem, plan)
    finally:
        if view:
            view.stop()

    metrics = MetricsCollector(nodes=result.plan.nodes).from_results(
        run_id, list(result.results.values()), audit.events()
    )
    print("\n" + MetricsCollector.format_table(metrics))
    _write_summary(run_dir, run_id, scenario, intake_result, result, metrics)

    write_report(
        run_dir / "report.html",
        run_id=run_id,
        plan=result.plan,
        results=list(result.results.values()),
        metrics=metrics,
        events=audit.events(),
        mermaid=PlanGraph(result.plan).to_mermaid(),
    )

    drifted = getattr(adapter, "drifted", None)
    if drifted:
        print(
            f"\nnote: {len(drifted)} replayed response(s) were matched by node rather than "
            f"by exact prompt, because the prompts have changed since recording "
            f"({', '.join(sorted(set(drifted)))}). The responses are real recorded "
            f"model output; re-record with --mode live for an exact replay."
        )

    print(f"\nstate: {result.state.value}")
    if result.stopped_reason:
        print(f"reason: {result.stopped_reason}")
    print(f"evidence: {run_dir}")
    return 0 if result.ok else 1


def _existing_code(workspace: Workspace) -> str | None:
    files = workspace.list_files()
    if not files:
        return None
    return "\n\n".join(
        f"--- {p} ---\n{workspace.read(p)}" for p in files[:40]
    )


def _latest_workspace(scenario_key: str) -> Path | None:
    """Most recent non-empty workspace produced by a given scenario.

    The scenario has to be matched, not merely accepted. An earlier version
    took the argument and ignored it, returning whichever run was newest, so
    running brownfield twice seeded the second run from the first brownfield
    rather than from greenfield. That stacks a change on top of the same change
    and produces plausible nonsense, quietly.

    A run's scenario is read from its `summary.json`, which the CLI writes even
    for a parked run, so identification does not depend on the run id.
    """
    matches: list[tuple[float, Path]] = []
    for workspace in RUNS.glob("*/workspace"):
        if not workspace.is_dir() or not any(workspace.iterdir()):
            continue
        summary = workspace.parent / "summary.json"
        if not summary.is_file():
            continue
        try:
            if json.loads(summary.read_text()).get("scenario") != scenario_key:
                continue
        except (json.JSONDecodeError, OSError):
            continue
        matches.append((workspace.stat().st_mtime, workspace))

    if not matches:
        return None
    return max(matches)[1]


def _write_summary(run_dir, run_id, scenario, intake_result, result, metrics) -> None:
    summary = {
        "run_id": run_id,
        "scenario": scenario.key,
        "requirement": scenario.requirement,
        "demonstrates": scenario.demonstrates,
        "intake": {
            "state": intake_result.state.value,
            "confidence": intake_result.problem.confidence,
            "scenario_detected": intake_result.problem.scenario.value,
            "ambiguities": [
                {"question": a.question, "why": a.why_it_matters, "blocking": a.blocking}
                for a in intake_result.problem.ambiguities
            ],
        },
    }
    if result is not None:
        summary["run"] = {
            "state": result.state.value,
            "stopped_reason": result.stopped_reason,
            "plan_version": result.plan.version,
            "plan_revisions": result.plan_revisions,
            "nodes": {
                nid: {
                    "state": r.state.value,
                    "attempts": r.attempts,
                    "rolled_back": r.rolled_back,
                    "used_fallback": r.used_fallback,
                    "duration": round(r.duration, 3),
                }
                for nid, r in result.results.items()
            },
        }
    if metrics is not None:
        summary["metrics"] = json.loads(MetricsCollector.to_json(metrics))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))


def _report(args: argparse.Namespace) -> int:
    """Rebuild the HTML report from a run's audit log alone.

    Worth being able to do: it proves the audit trail is complete enough to
    reconstruct what happened without any other state.
    """
    run_dir = RUNS / args.run_id
    if not run_dir.exists():
        raise SystemExit(f"no such run: {run_dir}")

    audit = AuditLog.read(args.run_id, RUNS)
    problems = audit.verify_integrity()
    if problems:
        print("audit integrity problems found:")
        for p in problems:
            print(f"  - {p}")

    events = audit.events()
    metrics = MetricsCollector().from_results(args.run_id, [], events)
    out = write_report(
        run_dir / "report.html",
        run_id=args.run_id,
        plan=Plan(nodes=[]),
        results=[],
        metrics=metrics,
        events=events,
    )
    print(f"wrote {out}")
    return 0


def _doctor(_: argparse.Namespace) -> int:
    load_env()
    import os

    print(f"python           {sys.version.split()[0]}")
    print(f"ANTHROPIC_API_KEY {'set' if os.getenv('ANTHROPIC_API_KEY') else 'NOT SET (replay mode only)'}")
    print(f"KEEL_AGENT_MODE   {os.getenv('KEEL_AGENT_MODE', 'replay')}")
    runs = sorted(p.name for p in RUNS.glob("*") if p.is_dir())
    print(f"recorded runs     {', '.join(runs) if runs else '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keel", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a scenario end to end")
    run.add_argument("--scenario", required=True, choices=sorted(scenarios.ALL))
    run.add_argument("--mode", default="replay", choices=[m.value for m in RunMode])
    run.add_argument("--replay-from", default=None, help="run id to replay transcripts from")
    run.add_argument("--run-id", default=None)
    run.add_argument(
        "--approvals", default="scripted", choices=["scripted", "auto", "interactive"]
    )
    run.add_argument("--no-live", action="store_true", help="plain output, no live tree")
    run.add_argument(
        "--answer-ambiguities",
        action="store_true",
        help="supply the scenario's scripted answers, resuming a parked run",
    )
    run.set_defaults(func=lambda a: asyncio.run(_run(a)))

    rep = sub.add_parser("report", help="regenerate the HTML report for a run")
    rep.add_argument("run_id")
    rep.set_defaults(func=_report)

    doc = sub.add_parser("doctor", help="check the environment")
    doc.set_defaults(func=_doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
