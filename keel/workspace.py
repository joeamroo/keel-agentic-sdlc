"""The sandbox that generated code lands in, and the rollback mechanism.

Two responsibilities, deliberately in one place because they are the same
concern seen from two angles:

1. Containment. Every path handed to this module was, somewhere upstream,
   proposed by a language model. A model asked to "write the config file" will
   occasionally propose `../../etc/passwd` or `/usr/local/bin/thing`, not out of
   malice but because the training data is full of such paths. So the workspace
   treats every relative path as untrusted input and proves it stays under the
   root before touching the filesystem.

2. Reversibility. §4.4 asks for rollback and safe-stop controls. The
   orchestrator snapshots the workspace before a node runs; if that node burns
   through its retry budget, the snapshot is restored so a half-written change
   never survives into the next stage. `restore` reports exactly which files it
   had to touch, which is what the audit log records as the blast radius of the
   rollback.

Snapshots are held in memory. These are small generated projects, a handful of
text files, and an in-memory copy makes rollback atomic from the caller's point
of view rather than a second thing that can fail mid-way.
"""

from __future__ import annotations

from collections.abc import Iterable

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from keel.models import Artifact, content_hash

__all__ = ["Workspace", "WorkspaceError", "Snapshot"]


class WorkspaceError(RuntimeError):
    """A path escaped the workspace, or the workspace refused an operation.

    Raised rather than returned because there is no sensible partial result: if
    containment fails, the only correct behaviour is to stop the node.
    """


# Directories that are machine output, not authored content. They are noise in
# a diff, they can be large, and restoring them buys nothing.
IGNORED_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".pytest_cache", ".venv", ".git"}
)

# Exit codes borrowed from shell convention so the test gate can distinguish
# "the command failed" from "the command never ran" without a second channel.
TIMEOUT_EXIT_CODE = 124
NOT_FOUND_EXIT_CODE = 127

_MEDIA_TYPES: dict[str, str] = {
    ".py": "text/x-python",
    ".md": "text/markdown",
    ".json": "application/json",
    ".toml": "text/x-toml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".txt": "text/plain",
    ".cfg": "text/plain",
    ".html": "text/html",
}


@dataclass(slots=True)
class Snapshot:
    """A full in-memory copy of the workspace at one instant.

    `files` maps relative path to complete contents, so it is both the file
    list and the file bodies. Anything not in it did not exist when the
    snapshot was taken, which is what lets `restore` delete files a failed node
    created.
    """

    files: dict[str, str] = field(default_factory=dict)
    taken_at: float = field(default_factory=time.time)
    label: str = ""

    @property
    def paths(self) -> list[str]:
        """The file list, sorted, so callers get a stable ordering."""
        return sorted(self.files)

    @property
    def digest(self) -> str:
        """Order-independent fingerprint of the whole tree.

        Two snapshots with the same digest are the same workspace state, which
        lets the audit log record "rollback restored state X" without dumping
        every file body into the log.
        """
        lines = [f"{path}:{content_hash(self.files[path])}" for path in self.paths]
        return content_hash("\n".join(lines))

    @property
    def file_count(self) -> int:
        return len(self.files)


class Workspace:
    """A rooted, path-validated view of a directory tree on disk.

    Every method that accepts a path routes through `_safe_path`, so there is a
    single place to audit the containment argument rather than a check repeated
    at each call site and eventually forgotten at one of them.
    """

    def __init__(self, root: Path | str) -> None:
        """Open (and create) the sandbox directory.

        The root is resolved immediately and the canonical form is what gets
        stored. Resolving once up front matters: `is_relative_to` compares
        lexically, so comparing a resolved candidate against an unresolved root
        would produce false rejections on any platform that symlinks its temp
        directory, macOS being the obvious one.
        """
        root_path = Path(root).expanduser()
        root_path.mkdir(parents=True, exist_ok=True)
        self.root: Path = root_path.resolve()

    def __repr__(self) -> str:
        return f"Workspace(root={str(self.root)!r})"

    # ----------------------------------------------------------------
    # Containment
    # ----------------------------------------------------------------

    def _safe_path(self, relative_path: str) -> Path:
        """Resolve a caller-supplied relative path, or refuse it.

        Three defences, layered on purpose:

        - Lexical rejection of absolute paths and `..` segments. Cheap, and it
          gives a precise error message naming what was wrong.
        - `Path.resolve()`, which collapses `.`/`..` and follows symlinks, so a
          symlink planted inside the workspace that points outside resolves to
          its real target rather than to its innocent-looking name.
        - `is_relative_to(self.root)` on the resolved result, which is the
          check that actually decides. The lexical pass is a nicety; this is
          the guarantee.
        """
        if not isinstance(relative_path, str):
            raise WorkspaceError(f"path must be a string, got {type(relative_path).__name__}")
        raw = relative_path.strip()
        if not raw:
            raise WorkspaceError("path must not be empty")
        if "\x00" in raw:
            raise WorkspaceError("path must not contain a null byte")

        posix = PurePosixPath(raw)
        windows = PureWindowsPath(raw)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise WorkspaceError(f"absolute paths are not allowed inside the workspace: {raw!r}")
        if ".." in posix.parts or ".." in windows.parts:
            raise WorkspaceError(f"path traversal is not allowed: {raw!r}")

        candidate = (self.root / raw).resolve()
        if candidate == self.root:
            raise WorkspaceError(f"path must name a file, not the workspace root: {raw!r}")
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes the workspace root: {raw!r}")
        return candidate

    def _relative(self, path: Path) -> str:
        """Canonical relative form used as the key in snapshots and diffs."""
        return path.relative_to(self.root).as_posix()

    # ----------------------------------------------------------------
    # File access
    # ----------------------------------------------------------------

    def write(
        self,
        relative_path: str,
        content: str,
        *,
        produced_by: str = "",
        name: str | None = None,
        media_type: str | None = None,
    ) -> Artifact:
        """Write a text file and describe it as an Artifact.

        Returning an Artifact rather than a bare path means the caller gets the
        content hash for free, and the lineage tracker can tell later that this
        file changed under a downstream consumer.
        """
        target = self._safe_path(relative_path)
        self._write_text(target, content)
        rel = self._relative(target)
        return Artifact(
            name=name or rel,
            content=content,
            produced_by=produced_by,
            path=rel,
            media_type=media_type or _MEDIA_TYPES.get(target.suffix, "text/plain"),
        )

    def read(self, relative_path: str) -> str:
        """Read a text file. Missing files raise rather than returning ''.

        A silent empty string here would let a gate pass on a file that was
        never written, which is exactly the failure the exit gates exist to
        catch.
        """
        target = self._safe_path(relative_path)
        if not target.is_file():
            raise WorkspaceError(f"no such file in workspace: {relative_path!r}")
        return self._read_text(target)

    def exists(self, relative_path: str) -> bool:
        """True if the path names an existing file or directory in the sandbox.

        A path that fails validation is reported as not existing rather than
        raising, because callers use this to branch, and "outside the
        workspace" and "not in the workspace" are the same answer to that
        question.
        """
        try:
            return self._safe_path(relative_path).exists()
        except WorkspaceError:
            return False

    def list_files(self) -> list[str]:
        """Every real file under the root, relative and sorted.

        Symlinks are skipped rather than followed. A link is not content: its
        target may sit outside the root, and copying a target back through a
        link during restore would write outside the sandbox. Skipping them
        keeps that whole class of escape out of snapshot and restore.
        """
        found: list[str] = []
        for path in self.root.rglob("*"):
            if any(part in IGNORED_DIRS for part in path.relative_to(self.root).parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            found.append(self._relative(path))
        return sorted(found)

    @staticmethod
    def _write_text(target: Path, content: str) -> None:
        """Write a file and drop any bytecode cached from its previous version.

        Not a nicety, a correctness fix. CPython decides a `.pyc` is current by
        comparing the source mtime (whole seconds) and size. A retried node
        rewrites the same module within the same second, and a one-character
        edit often leaves the size identical, so the interpreter happily
        imports the previous attempt's bytecode. The test gate then reports a
        pass on code that no longer exists. Rollback has the same exposure from
        the other direction: `restore` puts the old source back at the same
        size and second as the version that replaced it.

        Every `.pyc` for this module is cleared, not just the running
        interpreter's, since the subprocess under test may be a different
        Python than the one hosting the orchestrator.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if target.suffix != ".py":
            return
        cache_dir = target.parent / "__pycache__"
        if cache_dir.is_dir():
            for stale in cache_dir.glob(f"{target.stem}.*.pyc"):
                stale.unlink(missing_ok=True)
        legacy = target.with_suffix(".pyc")  # pre-PEP 3147 layout, still legal
        legacy.unlink(missing_ok=True)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                f"workspace holds generated source, expected UTF-8 text: {path.name}"
            ) from exc

    # ----------------------------------------------------------------
    # Snapshot and rollback
    # ----------------------------------------------------------------

    def snapshot(self, label: str = "") -> Snapshot:
        """Capture the whole tree so a failed node can be undone.

        Taken before a node runs, discarded once the node's exit gate passes.
        """
        files = {rel: self.read(rel) for rel in self.list_files()}
        return Snapshot(files=files, label=label)

    def restore(self, snapshot: Snapshot) -> list[str]:
        """Put the workspace back exactly as the snapshot found it.

        Three kinds of repair, and the third is the one naive rollback forgets:
        rewrite files whose contents drifted, recreate files that were deleted,
        and delete files that appeared after the snapshot. Without that last
        step a failed implement node leaves its half-written module behind, the
        test node imports it, and the run fails somewhere far from the cause.

        Returns the relative paths it had to change, sorted. An empty list is
        a real answer: nothing moved, so the rollback was a no-op and the audit
        log should say so.
        """
        changed: set[str] = set()

        for rel, content in snapshot.files.items():
            target = self._safe_path(rel)
            if target.is_file() and self._read_text(target) == content:
                continue
            self._write_text(target, content)
            changed.add(rel)

        for rel in self.list_files():
            if rel in snapshot.files:
                continue
            self._safe_path(rel).unlink()
            changed.add(rel)

        self._prune_empty_dirs()
        return sorted(changed)

    def restore_paths(self, snapshot: Snapshot, paths: Iterable[str]) -> list[str]:
        """Undo one writer's changes without touching anything else.

        `restore` puts the whole workspace back, which is correct for a single
        writer and wrong the moment two nodes run concurrently: reverting a
        failed node would also delete a sibling's committed output, because the
        sibling's files did not exist when this node took its snapshot. A live
        run hit exactly that, wiping a finished README when a parallel test
        stage rolled back.

        So rollback is scoped to the paths the node actually wrote. Each one is
        put back to its snapshot contents, or deleted if the snapshot did not
        have it. Files the node never touched are left alone whoever owns them.

        Returns the relative paths it changed, sorted.
        """
        changed: set[str] = set()

        for rel in paths:
            target = self._safe_path(rel)
            original = snapshot.files.get(rel)

            if original is None:
                if target.is_file():
                    target.unlink()
                    changed.add(rel)
                continue

            if not target.is_file() or self._read_text(target) != original:
                self._write_text(target, original)
                changed.add(rel)

        self._prune_empty_dirs()
        return sorted(changed)

    def diff(self, snapshot: Snapshot) -> dict[str, str]:
        """Classify every path that moved since the snapshot.

        Feeds the run report and the change-control review: a human signing off
        on a high-impact node wants the list of files this run added, changed
        and removed, not a wall of content.
        """
        current = set(self.list_files())
        before = set(snapshot.files)
        result: dict[str, str] = {}

        for rel in current - before:
            result[rel] = "added"
        for rel in before - current:
            result[rel] = "deleted"
        for rel in sorted(current & before):
            if self.read(rel) != snapshot.files[rel]:
                result[rel] = "modified"
        return dict(sorted(result.items()))

    def _prune_empty_dirs(self) -> None:
        """Remove directories a rollback emptied out.

        Cosmetic but load-bearing for the diff: a leftover empty package
        directory makes the restored tree look unequal to the snapshot when a
        human eyeballs it.
        """
        for path in sorted(self.root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_symlink() or not path.is_dir():
                continue
            if any(part in IGNORED_DIRS for part in path.relative_to(self.root).parts):
                continue
            if not any(path.iterdir()):
                path.rmdir()

    # ----------------------------------------------------------------
    # Subprocess execution
    # ----------------------------------------------------------------

    def run_command(self, cmd: list[str], timeout: float = 120) -> tuple[int, str, str]:
        """Run a command with the workspace as cwd. Returns (code, out, err).

        This is what makes the test gate an actual gate: pytest runs against
        the generated code and the exit code decides, instead of a model being
        asked whether it thinks its own code passes.

        Never `shell=True`. The argv comes from generated plans, and a shell
        would turn a filename with a semicolon in it into a second command.
        A string argument is rejected outright, since `shell=False` with a
        string silently means "execute a file with this exact name" and that
        failure mode is confusing at 2am.

        Timeouts and missing binaries come back as ordinary results with
        conventional exit codes rather than exceptions. A gate that crashes the
        orchestrator cannot record why it failed; a gate that returns 124 and a
        message ends up in the audit log like every other failure.
        """
        if isinstance(cmd, str):
            raise WorkspaceError("run_command takes an argv list, not a shell string")
        if not cmd or not all(isinstance(part, str) for part in cmd):
            raise WorkspaceError("run_command needs a non-empty list of string arguments")
        if timeout <= 0:
            raise WorkspaceError("run_command timeout must be positive")

        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell is never used
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = _as_text(exc.stdout)
            err = _as_text(exc.stderr)
            note = f"command timed out after {timeout}s: {' '.join(cmd)}"
            return TIMEOUT_EXIT_CODE, out, (err + "\n" + note).strip()
        except FileNotFoundError as exc:
            return NOT_FOUND_EXIT_CODE, "", f"command not found: {cmd[0]} ({exc.strerror})"
        except PermissionError as exc:
            return NOT_FOUND_EXIT_CODE, "", f"command not executable: {cmd[0]} ({exc.strerror})"

        return completed.returncode, completed.stdout or "", completed.stderr or ""

    # ----------------------------------------------------------------
    # Teardown
    # ----------------------------------------------------------------

    def clear(self) -> None:
        """Empty the workspace, keeping the root itself.

        Guarded twice. The root must not be a filesystem root or the user's
        home directory, because this method deletes recursively and a
        misconfigured root would make it a very effective mistake. Then each
        entry is re-validated against the root before removal, and symlinks are
        unlinked rather than walked, so a link pointing at real work outside
        the sandbox loses the link and not the work.
        """
        self._guard_destructive_root()
        for entry in self.root.iterdir():
            resolved = entry if entry.is_symlink() else entry.resolve()
            if not entry.is_symlink() and not resolved.is_relative_to(self.root):
                raise WorkspaceError(f"refusing to delete outside the workspace: {entry}")
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)

    def _guard_destructive_root(self) -> None:
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace root is missing: {self.root}")
        if self.root == Path(self.root.anchor) or self.root.parent == self.root:
            raise WorkspaceError(f"refusing to operate on a filesystem root: {self.root}")
        if self.root == Path.home().resolve():
            raise WorkspaceError("refusing to operate on the home directory")
        if self.root == Path(os.getcwd()).resolve().parent:
            # Cheap sanity check: the sandbox should be a leaf, never an
            # ancestor of the process it was launched from.
            raise WorkspaceError(f"refusing to operate on an ancestor directory: {self.root}")


def _as_text(value: str | bytes | None) -> str:
    """Partial output from a timed-out process arrives as str, bytes, or None."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
