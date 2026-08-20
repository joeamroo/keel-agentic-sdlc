"""Tests for the sandbox and the rollback mechanism.

Two things carry real weight here and both are tested adversarially rather than
happy-path: a path proposed by a model must not be able to reach outside the
root, and a restore must leave the tree byte-identical to the snapshot,
including deleting whatever the failed node created.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from keel.models import content_hash
from keel.workspace import (
    NOT_FOUND_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    Snapshot,
    Workspace,
    WorkspaceError,
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "sandbox")


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_init_creates_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    assert not root.exists()
    ws = Workspace(root)
    assert ws.root.is_dir()


def test_root_is_absolute_and_canonical(tmp_path: Path) -> None:
    """Stored root must be resolved, or containment checks misfire on macOS."""
    (tmp_path / "real").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)

    ws = Workspace(link)
    assert ws.root.is_absolute()
    assert ws.root == (tmp_path / "real").resolve()


def test_init_accepts_existing_root(tmp_path: Path) -> None:
    Workspace(tmp_path)
    assert Workspace(tmp_path).root == tmp_path.resolve()


# --------------------------------------------------------------------------
# Read and write
# --------------------------------------------------------------------------


def test_write_read_round_trip(ws: Workspace) -> None:
    content = "def add(a, b):\n    return a + b\n"
    artifact = ws.write("src/calc.py", content, produced_by="implement-1")

    assert ws.read("src/calc.py") == content
    assert artifact.path == "src/calc.py"
    assert artifact.name == "src/calc.py"
    assert artifact.produced_by == "implement-1"
    assert artifact.content == content
    assert artifact.sha == content_hash(content)
    assert artifact.media_type == "text/x-python"


def test_write_creates_parent_directories(ws: Workspace) -> None:
    ws.write("a/b/c/deep.txt", "hi")
    assert (ws.root / "a" / "b" / "c" / "deep.txt").is_file()


def test_write_overwrites(ws: Workspace) -> None:
    ws.write("f.txt", "first")
    ws.write("f.txt", "second")
    assert ws.read("f.txt") == "second"


def test_write_preserves_exact_bytes(ws: Workspace) -> None:
    """Trailing whitespace and unicode survive, since gates hash the content."""
    content = "line\n\n  indented \tt\nunicode: caf\u00e9 \u2713\n"
    ws.write("odd.txt", content)
    assert ws.read("odd.txt") == content
    assert (ws.root / "odd.txt").read_bytes() == content.encode("utf-8")


def test_read_missing_file_raises(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="no such file"):
        ws.read("nope.txt")


def test_exists(ws: Workspace) -> None:
    ws.write("here.txt", "x")
    assert ws.exists("here.txt")
    assert not ws.exists("gone.txt")


def test_exists_is_false_for_rejected_paths(ws: Workspace) -> None:
    """Outside the workspace and not in the workspace are the same answer."""
    assert not ws.exists("../../etc/passwd")
    assert not ws.exists("/etc/passwd")


def test_list_files_sorted_and_relative(ws: Workspace) -> None:
    ws.write("z.py", "z")
    ws.write("a.py", "a")
    ws.write("pkg/m.py", "m")
    assert ws.list_files() == ["a.py", "pkg/m.py", "z.py"]


def test_list_files_skips_machine_output(ws: Workspace) -> None:
    ws.write("keep.py", "k")
    for noisy in ("__pycache__/keep.cpython-313.pyc", ".pytest_cache/v/lastfailed",
                  ".venv/lib/thing.py", ".git/config"):
        target = ws.root / noisy
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("noise")
    assert ws.list_files() == ["keep.py"]


def test_list_files_skips_symlinks(ws: Workspace, tmp_path: Path) -> None:
    """A link is not content, and following one would leak the target."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    ws.write("real.txt", "real")
    (ws.root / "link.txt").symlink_to(outside)

    assert ws.list_files() == ["real.txt"]


# --------------------------------------------------------------------------
# Containment: the part that matters
# --------------------------------------------------------------------------


def test_traversal_is_rejected(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="traversal"):
        ws.write("../../etc/passwd", "pwned")


def test_traversal_is_rejected_on_read(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        ws.read("../../etc/passwd")


def test_traversal_that_stays_inside_is_still_rejected(ws: Workspace) -> None:
    """`a/../b` resolves inside the root, but `..` is refused regardless.

    Allowing the benign case means the validator has to reason about where the
    path lands, and that reasoning is where these bugs live.
    """
    with pytest.raises(WorkspaceError, match="traversal"):
        ws.write("pkg/../escaped_but_inside.txt", "x")


def test_sneaky_traversal_does_not_write_outside(ws: Workspace, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    with pytest.raises(WorkspaceError):
        ws.write("../victim.txt", "overwritten")
    assert victim.read_text() == "original"


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "/tmp/x.txt", "//server/share/f.txt", "C:\\Windows\\system.ini"],
)
def test_absolute_paths_are_rejected(ws: Workspace, bad: str) -> None:
    with pytest.raises(WorkspaceError, match="absolute|traversal"):
        ws.write(bad, "nope")


def test_escaping_symlink_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    """The core case: an innocent-looking name whose target is outside."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws.root / "innocent.txt").symlink_to(outside)

    with pytest.raises(WorkspaceError, match="escapes"):
        ws.read("innocent.txt")
    with pytest.raises(WorkspaceError, match="escapes"):
        ws.write("innocent.txt", "overwritten")
    assert outside.read_text() == "secret"


def test_escaping_symlinked_directory_is_rejected(ws: Workspace, tmp_path: Path) -> None:
    """A linked parent directory is the same escape one level up."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (ws.root / "sub").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="escapes"):
        ws.write("sub/planted.txt", "pwned")
    assert list(outside_dir.iterdir()) == []


def test_symlink_that_stays_inside_is_allowed(ws: Workspace) -> None:
    """Containment is about where a path lands, not about links being evil."""
    ws.write("target.txt", "inside")
    (ws.root / "alias.txt").symlink_to(ws.root / "target.txt")
    assert ws.read("alias.txt") == "inside"


@pytest.mark.parametrize("bad", ["", "   ", ".", "x\x00y"])
def test_degenerate_paths_are_rejected(ws: Workspace, bad: str) -> None:
    with pytest.raises(WorkspaceError):
        ws.write(bad, "x")


def test_non_string_path_is_rejected(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="must be a string"):
        ws.write(Path("/etc/passwd"), "x")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


def test_snapshot_captures_contents_and_file_list(ws: Workspace) -> None:
    ws.write("a.py", "alpha")
    ws.write("pkg/b.py", "beta")

    snap = ws.snapshot(label="before-implement")
    assert snap.paths == ["a.py", "pkg/b.py"]
    assert snap.files["pkg/b.py"] == "beta"
    assert snap.file_count == 2
    assert snap.label == "before-implement"
    assert snap.taken_at > 0


def test_snapshot_is_detached_from_disk(ws: Workspace) -> None:
    """In-memory means later writes cannot mutate the snapshot underneath us."""
    ws.write("a.py", "original")
    snap = ws.snapshot()
    ws.write("a.py", "changed")
    assert snap.files["a.py"] == "original"


def test_digest_is_stable_and_change_sensitive(ws: Workspace) -> None:
    ws.write("a.py", "alpha")
    first = ws.snapshot()
    assert first.digest == ws.snapshot().digest

    ws.write("a.py", "alpha!")
    assert ws.snapshot().digest != first.digest


def test_digest_ignores_insertion_order(ws: Workspace) -> None:
    one = Snapshot(files={"a": "1", "b": "2"})
    two = Snapshot(files={"b": "2", "a": "1"})
    assert one.digest == two.digest


def test_empty_snapshot_digest_is_defined(ws: Workspace) -> None:
    assert ws.snapshot().digest == content_hash("")


# --------------------------------------------------------------------------
# Restore: the rollback contract
# --------------------------------------------------------------------------


def test_restore_returns_exact_original_bytes(ws: Workspace) -> None:
    original = "def add(a, b):\n    return a + b\n"
    ws.write("calc.py", original)
    snap = ws.snapshot()

    ws.write("calc.py", "def add(a, b):\n    return a - b  # half-written\n")
    changed = ws.restore(snap)

    assert changed == ["calc.py"]
    assert (ws.root / "calc.py").read_bytes() == original.encode("utf-8")
    assert ws.snapshot().digest == snap.digest


def test_restore_deletes_files_created_after_snapshot(ws: Workspace) -> None:
    """The step naive rollback forgets, so it gets its own test."""
    ws.write("keep.py", "keep")
    snap = ws.snapshot()

    ws.write("junk.py", "half-written module")
    ws.write("pkg/more_junk.py", "also junk")

    changed = ws.restore(snap)

    assert changed == ["junk.py", "pkg/more_junk.py"]
    assert not (ws.root / "junk.py").exists()
    assert not (ws.root / "pkg" / "more_junk.py").exists()
    assert ws.list_files() == ["keep.py"]


def test_restore_recreates_deleted_files(ws: Workspace) -> None:
    ws.write("gone.py", "important")
    snap = ws.snapshot()
    (ws.root / "gone.py").unlink()

    changed = ws.restore(snap)

    assert changed == ["gone.py"]
    assert ws.read("gone.py") == "important"


def test_restore_handles_all_three_repairs_at_once(ws: Workspace) -> None:
    ws.write("mod.py", "original")
    ws.write("doomed.py", "delete me later")
    snap = ws.snapshot()

    ws.write("mod.py", "mangled")
    (ws.root / "doomed.py").unlink()
    ws.write("new.py", "created by the failed node")

    changed = ws.restore(snap)

    assert changed == ["doomed.py", "mod.py", "new.py"]
    assert ws.list_files() == ["doomed.py", "mod.py"]
    assert ws.read("mod.py") == "original"
    assert ws.read("doomed.py") == "delete me later"
    assert ws.snapshot().digest == snap.digest


def test_restore_is_a_noop_when_nothing_moved(ws: Workspace) -> None:
    ws.write("a.py", "a")
    snap = ws.snapshot()
    assert ws.restore(snap) == []


def test_restore_is_idempotent(ws: Workspace) -> None:
    ws.write("a.py", "a")
    snap = ws.snapshot()
    ws.write("a.py", "b")

    assert ws.restore(snap) == ["a.py"]
    assert ws.restore(snap) == []


def test_restore_to_empty_snapshot_clears_generated_work(ws: Workspace) -> None:
    snap = ws.snapshot()
    ws.write("pkg/a.py", "a")
    ws.write("pkg/sub/b.py", "b")

    assert ws.restore(snap) == ["pkg/a.py", "pkg/sub/b.py"]
    assert ws.list_files() == []
    assert not (ws.root / "pkg").exists()  # emptied directories are pruned
    assert ws.root.is_dir()


def test_restore_after_clear_rebuilds_the_tree(ws: Workspace) -> None:
    ws.write("a.py", "a")
    ws.write("pkg/b.py", "b")
    snap = ws.snapshot()

    ws.clear()
    assert ws.list_files() == []

    assert ws.restore(snap) == ["a.py", "pkg/b.py"]
    assert ws.snapshot().digest == snap.digest


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


def test_diff_classifies_added_modified_deleted(ws: Workspace) -> None:
    ws.write("same.py", "unchanged")
    ws.write("edit.py", "before")
    ws.write("drop.py", "bye")
    snap = ws.snapshot()

    ws.write("edit.py", "after")
    (ws.root / "drop.py").unlink()
    ws.write("new.py", "hello")

    assert ws.diff(snap) == {
        "drop.py": "deleted",
        "edit.py": "modified",
        "new.py": "added",
    }


def test_diff_is_empty_when_nothing_changed(ws: Workspace) -> None:
    ws.write("a.py", "a")
    snap = ws.snapshot()
    assert ws.diff(snap) == {}


def test_diff_is_empty_after_restore(ws: Workspace) -> None:
    ws.write("a.py", "a")
    snap = ws.snapshot()
    ws.write("a.py", "b")
    ws.write("c.py", "c")
    ws.restore(snap)
    assert ws.diff(snap) == {}


# --------------------------------------------------------------------------
# run_command: how the test gate actually runs pytest
# --------------------------------------------------------------------------


def test_run_command_captures_success(ws: Workspace) -> None:
    code, out, err = ws.run_command([sys.executable, "-c", "print('ok')"])
    assert code == 0
    assert out.strip() == "ok"
    assert err == ""


def test_run_command_captures_nonzero_exit_and_stderr(ws: Workspace) -> None:
    code, out, err = ws.run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    assert code == 3
    assert "boom" in err
    assert out == ""


def test_run_command_runs_in_the_workspace(ws: Workspace) -> None:
    ws.write("marker.txt", "x")
    code, out, _ = ws.run_command(
        [sys.executable, "-c", "import os; print(os.getcwd()); print(os.listdir())"]
    )
    assert code == 0
    cwd_line, listing = out.strip().splitlines()
    assert Path(cwd_line).resolve() == ws.root
    assert "marker.txt" in listing


def test_run_command_times_out_cleanly(ws: Workspace) -> None:
    """A timed-out gate returns 124 rather than raising into the orchestrator."""
    code, _, err = ws.run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
    )
    assert code == TIMEOUT_EXIT_CODE
    assert "timed out" in err


def test_run_command_reports_missing_binary(ws: Workspace) -> None:
    code, _, err = ws.run_command(["keel-definitely-not-a-real-binary"])
    assert code == NOT_FOUND_EXIT_CODE
    assert "not found" in err


def test_run_command_rejects_a_shell_string(ws: Workspace) -> None:
    """shell=True is never used, so a string here is always a mistake."""
    with pytest.raises(WorkspaceError, match="argv list"):
        ws.run_command("rm -rf / ; echo pwned")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [[], [sys.executable, 3]])
def test_run_command_rejects_malformed_argv(ws: Workspace, bad: list) -> None:
    with pytest.raises(WorkspaceError):
        ws.run_command(bad)


def test_run_command_rejects_nonpositive_timeout(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="timeout"):
        ws.run_command([sys.executable, "-c", "pass"], timeout=0)


def test_run_command_does_not_interpret_shell_metacharacters(ws: Workspace) -> None:
    """The argument is data. A shell would have made it a second command."""
    code, out, _ = ws.run_command([sys.executable, "-c", "print('a; rm -rf x')"])
    assert code == 0
    assert out.strip() == "a; rm -rf x"
    assert ws.root.is_dir()


def test_run_command_actually_runs_pytest(ws: Workspace) -> None:
    """The gate is real: generated tests execute and their exit code decides.

    The rewrite here is deliberately the same byte length as the original and
    lands in the same second, which is precisely the case where CPython's
    mtime-and-size bytecode cache would otherwise rerun the previous attempt.
    A green result on stale bytecode is the worst failure this module can have,
    so it is pinned here rather than left to luck.
    """
    ws.write("test_generated.py", "def test_math():\n    assert 1 + 1 == 2\n")
    code, out, _ = ws.run_command([sys.executable, "-m", "pytest", "-q"], timeout=120)
    assert code == 0, out

    ws.write("test_generated.py", "def test_math():\n    assert 1 + 1 == 3\n")
    code, out, _ = ws.run_command([sys.executable, "-m", "pytest", "-q"], timeout=120)
    assert code != 0
    assert "1 failed" in out


def test_write_invalidates_stale_bytecode(ws: Workspace) -> None:
    """Import the module twice across a same-size, same-second rewrite."""
    probe = [
        sys.executable,
        "-c",
        "import gen; print(gen.VALUE)",
    ]
    ws.write("gen.py", "VALUE = 'aaa'\n")
    code, out, err = ws.run_command(probe)
    assert code == 0, err
    assert out.strip() == "aaa"
    assert (ws.root / "__pycache__").is_dir()  # the cache really was populated

    ws.write("gen.py", "VALUE = 'bbb'\n")
    code, out, err = ws.run_command(probe)
    assert code == 0, err
    assert out.strip() == "bbb"


def test_restore_invalidates_stale_bytecode(ws: Workspace) -> None:
    """Rollback has the same exposure as write, from the other direction."""
    probe = [sys.executable, "-c", "import gen; print(gen.VALUE)"]
    ws.write("gen.py", "VALUE = 'old'\n")
    snap = ws.snapshot()

    ws.write("gen.py", "VALUE = 'new'\n")
    assert ws.run_command(probe)[1].strip() == "new"

    ws.restore(snap)
    assert ws.run_command(probe)[1].strip() == "old"


# --------------------------------------------------------------------------
# clear
# --------------------------------------------------------------------------


def test_clear_empties_but_keeps_the_root(ws: Workspace) -> None:
    ws.write("a.py", "a")
    ws.write("pkg/b.py", "b")
    ws.clear()
    assert ws.root.is_dir()
    assert list(ws.root.iterdir()) == []
    assert ws.list_files() == []


def test_clear_does_not_follow_symlinks_out_of_the_workspace(
    ws: Workspace, tmp_path: Path
) -> None:
    """Deleting the link must not delete the real work it points at."""
    outside_dir = tmp_path / "real_work"
    outside_dir.mkdir()
    (outside_dir / "important.txt").write_text("do not delete")
    (ws.root / "link").symlink_to(outside_dir, target_is_directory=True)

    ws.clear()

    assert list(ws.root.iterdir()) == []
    assert (outside_dir / "important.txt").read_text() == "do not delete"


def test_clear_refuses_when_the_root_is_gone(ws: Workspace) -> None:
    shutil.rmtree(ws.root)
    with pytest.raises(WorkspaceError, match="missing"):
        ws.clear()


def test_clear_refuses_the_home_directory() -> None:
    ws = Workspace(Path.home())
    with pytest.raises(WorkspaceError, match="home directory"):
        ws.clear()


def test_clear_refuses_a_filesystem_root() -> None:
    ws = Workspace(Path("/"))
    with pytest.raises(WorkspaceError, match="filesystem root"):
        ws.clear()
