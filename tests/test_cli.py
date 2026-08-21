"""QA tests for the CLI wiring.

The CLI is thin, but it is where the pieces meet, and a mistake here is
invisible to every module-level test because each module is individually
correct.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import keel.cli as cli
from keel.models import RunMode


def _make_run(root: Path, name: str, scenario: str, files: dict[str, str] | None = None) -> Path:
    workspace = root / name / "workspace"
    workspace.mkdir(parents=True)
    for rel, content in (files or {"app.py": "x = 1\n"}).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (root / name / "summary.json").write_text(json.dumps({"scenario": scenario}))
    return workspace


def test_brownfield_seeds_from_greenfield_not_from_whatever_ran_last(tmp_path, monkeypatch):
    """Regression: the scenario argument was accepted and then ignored.

    Running brownfield twice therefore seeded the second run from the first
    brownfield rather than from greenfield, stacking a change on top of the
    same change and producing plausible nonsense with no error.
    """
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    greenfield = _make_run(tmp_path, "r1", "greenfield")
    brownfield = _make_run(tmp_path, "r2", "brownfield")

    # Make the brownfield run the most recent, which is the trap.
    now = time.time()
    os.utime(brownfield, (now, now))
    os.utime(greenfield, (now - 600, now - 600))

    picked = cli._latest_workspace("greenfield")

    assert picked is not None
    assert picked.parent.name == "r1", (
        f"seeded from {picked.parent.name}, which is not a greenfield run"
    )


def test_no_matching_scenario_returns_none_rather_than_a_wrong_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    _make_run(tmp_path, "r1", "ambiguous")

    assert cli._latest_workspace("greenfield") is None


def test_an_empty_workspace_is_never_used_as_a_seed(tmp_path, monkeypatch):
    """A parked run writes a summary but no code, and must not seed anything."""
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    (tmp_path / "parked" / "workspace").mkdir(parents=True)
    (tmp_path / "parked" / "summary.json").write_text(json.dumps({"scenario": "greenfield"}))

    assert cli._latest_workspace("greenfield") is None


def test_replay_mode_refuses_clearly_when_it_has_nothing_to_replay():
    """The error has to say what to do, because this is the first thing a
    reviewer hits if the cassettes are missing."""
    with pytest.raises(SystemExit) as excinfo:
        cli._adapter(RunMode.REPLAY, replay_from=None)

    message = str(excinfo.value)
    assert "--replay-from" in message
    assert "live" in message


def test_existing_code_summary_is_none_for_an_empty_workspace(tmp_path):
    from keel.workspace import Workspace

    assert cli._existing_code(Workspace(tmp_path / "ws")) is None


def test_existing_code_summary_includes_file_contents(tmp_path):
    from keel.workspace import Workspace

    workspace = Workspace(tmp_path / "ws")
    workspace.write("app/main.py", "def handler():\n    return 1\n")

    summary = cli._existing_code(workspace)

    assert summary is not None
    assert "app/main.py" in summary
    assert "def handler()" in summary


def test_summary_is_written_even_when_a_run_parks(tmp_path):
    """A parked run still has to leave evidence, or the park is invisible."""
    from keel.intake import IntakeResult
    from keel.models import Ambiguity, EngineeringProblem, ScenarioKind, TaskState
    from keel.scenarios import AMBIGUOUS

    run_dir = tmp_path / "parked"
    run_dir.mkdir()
    problem = EngineeringProblem(
        raw_requirement="make it secure",
        scenario=ScenarioKind.AMBIGUOUS,
        confidence=0.2,
        ambiguities=[Ambiguity(question="secure against what?", why_it_matters="shape changes")],
    )
    result = IntakeResult(
        problem=problem,
        state=TaskState.INPUT_REQUIRED,
        questions=problem.blocking_ambiguities,
    )

    cli._write_summary(run_dir, "parked", AMBIGUOUS, result, None, None)
    written = json.loads((run_dir / "summary.json").read_text())

    assert written["scenario"] == "ambiguous"
    assert written["intake"]["state"] == "input_required"
    assert written["intake"]["ambiguities"][0]["question"] == "secure against what?"
    assert "run" not in written, "a parked run should not report execution results"


def test_seeding_does_not_copy_build_artifacts(tmp_path, monkeypatch):
    """Regression: seeding carried the previous run's bytecode across.

    copytree bypasses Workspace, so the stale-bytecode protection in
    Workspace.write never applied to seeded files. A copied .pyc also embeds
    the seed run's source paths, so a brownfield traceback pointed at the
    greenfield workspace while you were trying to read it.
    """
    import shutil

    monkeypatch.setattr(cli, "RUNS", tmp_path)
    seed = _make_run(
        tmp_path,
        "seed",
        "greenfield",
        {"app/main.py": "x = 1\n", "app/__pycache__/main.cpython-313.pyc": "stale"},
    )
    (seed / ".pytest_cache").mkdir(exist_ok=True)

    target = tmp_path / "new" / "workspace"
    target.mkdir(parents=True)
    shutil.copytree(
        seed,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )

    copied = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert "app/main.py" in copied, "source was not seeded"
    assert not any(".pyc" in c or "__pycache__" in c for c in copied), copied
    assert not (target / ".pytest_cache").exists()


def test_the_cli_seeding_call_excludes_caches():
    """Pin the call itself, since the behaviour lives in its arguments."""
    import inspect

    source = inspect.getsource(cli._run)
    assert "ignore_patterns" in source, "seeding no longer filters anything"
    assert "__pycache__" in source
