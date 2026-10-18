"""GitPython-backed implementation of :class:`~gitconfig_cli.abstractions.VersionControl`.

This is the only module that imports GitPython. Every Git failure is
translated into the tool's own exceptions, and every operation is logged
at debug level so ``-vv`` shows exactly what was run.
"""

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import git
from git.exc import BadName, BadObject, GitCommandError, GitCommandNotFound

from gitconfig_cli import gitignore
from gitconfig_cli.abstractions import MergePreference, VersionControl
from gitconfig_cli.exceptions import (
    DirtyWorkingTreeError,
    GitOperationError,
    InvalidRepoError,
    MergeConflictError,
)
from gitconfig_cli.logging_config import get_logger
from gitconfig_cli.models import CommitInfo, MergeResult

_log = get_logger("git")

GITIGNORE = ".gitignore"


def _error_details(exc: GitCommandError) -> str:
    """Extract a readable message from a GitPython command error."""
    raw = str(exc.stderr or exc.stdout or exc).strip()
    for prefix in ("stderr:", "stdout:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
    return raw.strip("'").strip() or str(exc)


def _force_remove(func: Callable[..., Any], path: str, *_: Any) -> None:
    """Retry a failed delete after clearing the read-only flag (Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(path: Path) -> None:
    """Delete a directory tree, coping with read-only Git object files."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:  # pragma: no cover - exercised only on Python 3.11
        shutil.rmtree(path, onerror=_force_remove)


class GitRepository(VersionControl):
    """A configuration repository on disk, operated through GitPython.

    Create instances with :meth:`open`, :meth:`init` or :meth:`clone`
    rather than calling the constructor directly.

    Example:
        >>> repo = GitRepository.init(Path("configs"))  # doctest: +SKIP
        >>> with repo.checked_out("draft/new-port"):  # doctest: +SKIP
        ...     repo.write_file("app.json", "{}\\n")
        ...     repo.commit("Add app.json", ["app.json"])
    """

    def __init__(self, repo: git.Repo) -> None:
        """Wrap an existing GitPython repository.

        Args:
            repo: A non-bare GitPython ``Repo``.
        """
        self._repo = repo
        self._root = Path(repo.working_tree_dir or ".").resolve()

    # -- construction ----------------------------------------------------

    @classmethod
    def open(cls, path: Path) -> "GitRepository":
        """Open an existing repository rooted exactly at ``path``.

        Args:
            path: Root directory of the working tree.

        Returns:
            The opened repository.

        Raises:
            InvalidRepoError: If ``path`` does not exist, is not the root of
                a Git repository, or is a bare repository.
        """
        try:
            repo = git.Repo(path)
        except git.NoSuchPathError:
            raise InvalidRepoError(path, "directory does not exist") from None
        except git.InvalidGitRepositoryError:
            raise InvalidRepoError(
                path, "not a Git repository (run 'gitconfig-cli init' first)"
            ) from None
        if repo.bare:
            repo.close()
            raise InvalidRepoError(path, "bare repositories have no working tree")
        _log.debug("repo.opened", path=str(path))
        return cls(repo)

    @classmethod
    def init(cls, path: Path, main_branch: str = "main") -> "GitRepository":
        """Create a new, empty repository (creating the directory if needed).

        Args:
            path: Directory for the new repository.
            main_branch: Name of the initial branch.

        Returns:
            The new repository (it has no commits yet).

        Raises:
            InvalidRepoError: If ``path`` already contains a Git repository.
            GitOperationError: If ``git init`` fails.
        """
        if (path / ".git").exists():
            raise InvalidRepoError(path, "already a Git repository")
        try:
            repo = git.Repo.init(path, mkdir=True, initial_branch=main_branch)
        except (GitCommandError, OSError) as exc:
            details = (
                _error_details(exc) if isinstance(exc, GitCommandError) else str(exc)
            )
            raise GitOperationError("init", details) from exc
        _log.debug("repo.initialized", path=str(path), branch=main_branch)
        return cls(repo)

    @classmethod
    def clone(cls, url: str, path: Path) -> "GitRepository":
        """Clone a remote repository and create local copies of its branches.

        Args:
            url: URL or local path of the repository to clone.
            path: Destination directory (must not exist or be empty).

        Returns:
            The cloned repository.

        Raises:
            GitOperationError: If cloning fails.
        """
        try:
            repo = git.Repo.clone_from(url, path)
        except GitCommandError as exc:
            raise GitOperationError("clone", _error_details(exc)) from exc
        cloned = cls(repo)
        cloned._track_remote_branches()
        _log.debug("repo.cloned", url=url, path=str(path))
        return cloned

    def close(self) -> None:
        """Release the Git subprocesses and file handles held by GitPython."""
        self._repo.close()

    # -- helpers -----------------------------------------------------------

    def _run(self, operation: str, *args: str) -> str:
        """Run ``git <operation> <args>`` and translate failures."""
        _log.debug("git.run", command=["git", operation, *args])
        try:
            output: str = self._repo.git.execute(["git", operation, *args])
        except GitCommandError as exc:
            raise GitOperationError(operation, _error_details(exc)) from exc
        except GitCommandNotFound as exc:  # pragma: no cover - needs git missing
            raise GitOperationError(
                operation, "the git executable was not found"
            ) from exc
        return output

    def _commit(self, ref: str) -> Any:
        """Return the GitPython commit object for ``ref``."""
        try:
            return self._repo.commit(ref)
        except (BadName, BadObject, ValueError):
            raise GitOperationError("rev-parse", f"unknown revision '{ref}'") from None

    def _head_sha(self) -> str:
        """Return the hash of the current HEAD commit."""
        return self._run("rev-parse", "HEAD")

    def _track_remote_branches(self, remote: str = "origin") -> None:
        """Create a local branch for every branch of ``remote`` not yet local."""
        if remote not in [r.name for r in self._repo.remotes]:
            return
        local = set(self.list_branches())
        for ref in self._repo.remotes[remote].refs:
            name = ref.remote_head
            if name != "HEAD" and name not in local:
                self._run("branch", "--quiet", "--track", name, f"{remote}/{name}")

    def _copy_identity_from(self, other: "GitRepository") -> None:
        """Give this repository the commit identity effective in ``other``.

        A clone does not inherit repository-local settings, so without this
        a user whose name/e-mail is configured only in the configuration
        repository could not commit in a temporary clone.
        """
        reader = other._repo.config_reader()
        with self._repo.config_writer() as writer:
            for key in ("name", "email"):
                value = reader.get_value("user", key, default="")
                if value:
                    writer.set_value("user", key, str(value))

    def _full_path(self, path: str) -> Path:
        """Resolve a repository-relative path, refusing to escape the root."""
        full = (self._root / path).resolve()
        if full != self._root and self._root not in full.parents:
            raise ValueError(f"path '{path}' is outside the repository")
        return full

    # -- branches ------------------------------------------------------------

    @property
    def root(self) -> Path:
        """Root directory of the working tree."""
        return self._root

    def current_branch(self) -> str:
        """Return the checked-out branch name.

        Returns:
            The branch name.

        Raises:
            InvalidRepoError: If HEAD is detached.
        """
        try:
            name: str = self._repo.active_branch.name
        except TypeError:
            raise InvalidRepoError(
                self._root, "HEAD is detached; check out a branch first"
            ) from None
        return name

    def branch_exists(self, name: str) -> bool:
        """Tell whether a local branch exists.

        Args:
            name: Branch name.

        Returns:
            ``True`` if ``refs/heads/<name>`` exists.
        """
        return name in self.list_branches()

    def list_branches(self, prefix: str = "") -> list[str]:
        """List local branches, optionally only those starting with ``prefix``.

        Args:
            prefix: Branch name prefix such as ``"draft/"``.

        Returns:
            Sorted branch names.
        """
        return sorted(h.name for h in self._repo.heads if h.name.startswith(prefix))

    def create_branch(self, name: str, start_point: str) -> None:
        """Create a branch without checking it out.

        Args:
            name: New branch name.
            start_point: Branch or commit to start from.

        Raises:
            GitOperationError: If Git refuses (e.g. invalid name).
        """
        self._run("branch", "--quiet", name, start_point)
        _log.debug("branch.created", branch=name, start_point=start_point)

    def delete_branch(self, name: str, force: bool = False) -> None:
        """Delete a local branch.

        Args:
            name: Branch to delete.
            force: Delete even if it is not fully merged.

        Raises:
            GitOperationError: If Git refuses (e.g. unmerged and not forced).
        """
        self._run("branch", "--quiet", "-D" if force else "-d", name)
        _log.debug("branch.deleted", branch=name, force=force)

    def checkout(self, name: str) -> None:
        """Check out a branch.

        Args:
            name: Branch to check out.

        Raises:
            GitOperationError: If the checkout fails.
        """
        self._run("checkout", "--quiet", name)

    @contextmanager
    def checked_out(self, name: str) -> Iterator[None]:
        """Temporarily check out a branch, restoring the original on exit.

        If the body raises, uncommitted changes made inside it are discarded
        before switching back, so the repository is never left half-edited.

        Args:
            name: Branch to check out for the duration of the block.

        Yields:
            Nothing; the branch is checked out while the block runs.

        Raises:
            DirtyWorkingTreeError: If the tree is not clean on entry, since
                the rollback could otherwise destroy the user's own edits.
        """
        self.ensure_clean()
        original = self.current_branch()
        if original != name:
            self.checkout(name)
        try:
            yield
        except BaseException:
            _log.debug("checkout.rollback", branch=name)
            if self.is_merging():
                self._run("merge", "--abort")
            self._run("reset", "--quiet", "--hard", "HEAD")
            self._run("clean", "--quiet", "-fd")
            raise
        finally:
            if self.current_branch() != original:
                self.checkout(original)

    def ahead_behind(self, base: str, head: str) -> tuple[int, int]:
        """Count commits unique to each side.

        Args:
            base: Reference branch (e.g. ``main``).
            head: Compared branch (e.g. a draft).

        Returns:
            ``(ahead, behind)``: commits only on ``head``, commits only on
            ``base``.
        """
        output = self._run("rev-list", "--left-right", "--count", f"{base}...{head}")
        behind, ahead = (int(part) for part in output.split())
        return ahead, behind

    # -- working tree and commits -----------------------------------------

    def dirty_files(self) -> list[str]:
        """List modified, staged or untracked files (ignored files excluded).

        Returns:
            Sorted repository-relative paths.
        """
        output = self._run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries = output.split("\0")
        files: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            files.append(entry[3:])
            if entry[0] in "RC":  # renames/copies are followed by the old path
                index += 1
        return sorted(files)

    def ensure_clean(self) -> None:
        """Fail if the working tree has uncommitted changes.

        Raises:
            DirtyWorkingTreeError: If there are uncommitted changes.
        """
        files = self.dirty_files()
        if files:
            raise DirtyWorkingTreeError(self._root, files)

    def write_file(self, path: str, content: str) -> None:
        """Write a file in the working tree (UTF-8, ``\\n`` newlines).

        Args:
            path: Path relative to :attr:`root`.
            content: New file content.

        Raises:
            ValueError: If ``path`` points outside the repository.
        """
        full = self._full_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        _log.debug("file.written", path=path, bytes=len(content.encode("utf-8")))

    def read_working_file(self, path: str) -> str:
        """Read a file from the working tree.

        Args:
            path: Path relative to :attr:`root`.

        Returns:
            The file content.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If ``path`` points outside the repository.
        """
        return self._full_path(path).read_text(encoding="utf-8")

    def remove_file(self, path: str) -> None:
        """Delete a tracked file from the working tree and stage the removal.

        Args:
            path: Path relative to :attr:`root`.

        Raises:
            GitOperationError: If the file is not tracked.
        """
        self._run("rm", "--quiet", "--", path)

    def stage(self, paths: Sequence[str]) -> None:
        """Stage files for the next commit (also marks conflicts resolved).

        Args:
            paths: Paths relative to :attr:`root`.
        """
        if paths:
            self._run("add", "--", *paths)

    def commit(self, message: str, paths: Sequence[str] = ()) -> str:
        """Stage ``paths`` (if any) and commit everything staged.

        Args:
            message: Commit message.
            paths: Files to stage before committing.

        Returns:
            The new commit hash.

        Raises:
            GitOperationError: If staging or committing fails (for example
                when nothing changed or no Git identity is configured).
        """
        self.stage(paths)
        self._run("commit", "--quiet", "-m", message)
        sha = self._head_sha()
        _log.debug("commit.created", commit=sha, message=message, paths=list(paths))
        return sha

    # -- reading history ------------------------------------------------------

    def read_file(self, path: str, ref: str) -> str:
        """Read a file as it exists at a branch or commit.

        Args:
            path: Path relative to :attr:`root`, with ``/`` separators.
            ref: Branch name or commit hash.

        Returns:
            The file content decoded as UTF-8.

        Raises:
            FileNotFoundError: If the file does not exist at ``ref``.
            GitOperationError: If ``ref`` cannot be resolved.
        """
        tree = self._commit(ref).tree
        try:
            blob = tree / path
        except KeyError:
            raise FileNotFoundError(path) from None
        if blob.type != "blob":
            raise FileNotFoundError(path)
        content: bytes = blob.data_stream.read()
        return content.decode("utf-8")

    def list_files(self, ref: str) -> list[str]:
        """List all files tracked at a branch or commit.

        Args:
            ref: Branch name or commit hash.

        Returns:
            Sorted paths with ``/`` separators.

        Raises:
            GitOperationError: If ``ref`` cannot be resolved.
        """
        tree = self._commit(ref).tree
        return sorted(item.path for item in tree.traverse() if item.type == "blob")

    def resolve_commit(self, ref: str) -> str:
        """Resolve a branch, tag or (abbreviated) hash to a full commit hash.

        Args:
            ref: Reference to resolve.

        Returns:
            The full commit hash.

        Raises:
            GitOperationError: If ``ref`` does not name a commit.
        """
        sha: str = self._commit(ref).hexsha
        return sha

    def history(
        self,
        ref: str,
        path: str | None = None,
        limit: int | None = None,
        first_parent: bool = False,
    ) -> list[CommitInfo]:
        """List commits reachable from ``ref``, newest first.

        Args:
            ref: Branch name or commit hash to start from.
            path: Only include commits that touched this file.
            limit: Maximum number of commits.
            first_parent: Follow only the first parent of merge commits, so
                each merged branch appears as its single merge commit.

        Returns:
            Commit summaries, newest first.

        Raises:
            GitOperationError: If ``ref`` cannot be resolved.
        """
        self._commit(ref)
        kwargs: dict[str, Any] = {}
        if path is not None:
            kwargs["paths"] = path
        if limit is not None:
            kwargs["max_count"] = limit
        if first_parent:
            kwargs["first_parent"] = True
        return [
            CommitInfo(
                sha=c.hexsha,
                short_sha=c.hexsha[:7],
                author=str(c.author.name),
                date=c.committed_datetime.isoformat(),
                message=str(c.summary),
            )
            for c in self._repo.iter_commits(ref, **kwargs)
        ]

    def diff(self, base: str, head: str, paths: Sequence[str] = ()) -> str:
        """Unified diff of what ``head`` changed since it diverged from ``base``.

        Args:
            base: Reference branch (e.g. ``main``).
            head: Compared branch (e.g. a draft).
            paths: Restrict the diff to these files.

        Returns:
            The diff text (empty when there are no changes).
        """
        return self._run("diff", f"{base}...{head}", "--", *paths)

    def changed_files(self, old: str, new: str) -> list[str]:
        """List files that differ between two commits.

        Args:
            old: Earlier commit.
            new: Later commit.

        Returns:
            Sorted repository-relative paths.
        """
        output = self._run("diff", "--name-only", old, new)
        return sorted(line for line in output.splitlines() if line)

    def merge_base(self, first: str, second: str) -> str | None:
        """Return the best common ancestor of two refs, if any.

        Args:
            first: A branch or commit.
            second: Another branch or commit.

        Returns:
            The ancestor's hash, or ``None`` for unrelated histories.
        """
        bases = self._repo.merge_base(first, second)
        return str(bases[0].hexsha) if bases else None

    # -- merging ---------------------------------------------------------

    def merge(
        self,
        source: str,
        message: str,
        *,
        prefer: MergePreference | None = None,
        squash: bool = False,
        abort_on_conflict: bool = True,
        commit: bool = True,
    ) -> MergeResult:
        """Merge ``source`` into the checked-out branch.

        Non-squash merges always create a merge commit (``--no-ff``) so the
        history shows exactly when each draft was published.

        Args:
            source: Branch to merge in.
            message: Message of the merge (or squash) commit.
            prefer: Resolve conflicting hunks in favour of one side
                (``"ours"`` = checked-out branch, ``"theirs"`` = ``source``).
            squash: Combine the source's changes into a single commit.
            abort_on_conflict: Restore the pre-merge state on conflict. When
                ``False`` the conflicts are left in the working tree.
            commit: When ``False`` the merge is prepared but not committed.

        Returns:
            The merge outcome.

        Raises:
            MergeConflictError: If the merge produced conflicts.
            GitOperationError: If the merge failed for another reason.
        """
        target = self.current_branch()
        before = self._head_sha()
        args = ["--squash"] if squash else ["--no-ff", "-m", message]
        if not commit and not squash:
            args.append("--no-commit")
        if prefer is not None:
            args += ["-X", prefer]
        _log.debug("merge.started", source=source, target=target, args=args)

        try:
            self._run("merge", *args, source)
        except GitOperationError:
            conflicts = self.conflicted_files()
            if not conflicts:
                raise
            if abort_on_conflict:
                if self.is_merging():
                    self.abort_merge()
                else:
                    self.reset_hard(before)
            _log.debug("merge.conflict", source=source, target=target, files=conflicts)
            raise MergeConflictError(source, target, conflicts) from None

        staged = self._run("diff", "--cached", "--name-only", before).splitlines()
        if squash or not commit:
            if not staged and not self.is_merging():
                return MergeResult(commit=before, up_to_date=True)
            if squash and commit:
                self._run("commit", "--quiet", "-m", message)
                return self._merge_result(before)
            return MergeResult(commit=before, files=sorted(staged))

        if self._head_sha() == before:
            return MergeResult(commit=before, up_to_date=True)
        return self._merge_result(before)

    def _merge_result(self, before: str) -> MergeResult:
        """Build a :class:`MergeResult` for the commits created since ``before``."""
        after = self._head_sha()
        return MergeResult(commit=after, files=self.changed_files(before, after))

    def is_merging(self) -> bool:
        """Tell whether a merge is in progress (``MERGE_HEAD`` exists).

        Returns:
            ``True`` while a merge awaits resolution or commit.
        """
        return (Path(self._repo.git_dir) / "MERGE_HEAD").exists()

    def conflicted_files(self) -> list[str]:
        """List files with unresolved merge conflicts.

        Returns:
            Sorted repository-relative paths.
        """
        output = self._run("diff", "--name-only", "--diff-filter=U")
        return sorted(line for line in output.splitlines() if line)

    def abort_merge(self) -> None:
        """Abort the merge in progress and restore the pre-merge state.

        Raises:
            GitOperationError: If no merge is in progress.
        """
        self._run("merge", "--abort")
        _log.debug("merge.aborted")

    def reset_hard(self, ref: str) -> None:
        """Move the current branch to ``ref``, discarding working tree changes.

        Args:
            ref: Commit to reset to.
        """
        self._run("reset", "--quiet", "--hard", ref)

    # -- remotes and scratch copies -----------------------------------

    def _require_remote(self, operation: str, remote: str) -> None:
        """Raise unless ``remote`` is configured."""
        if remote not in [r.name for r in self._repo.remotes]:
            raise GitOperationError(operation, f"no remote named '{remote}'")

    def push(self, branch: str, remote: str = "origin") -> None:
        """Push a branch to a remote and set it as upstream.

        Args:
            branch: Local branch to push.
            remote: Remote name.

        Raises:
            GitOperationError: If the push fails or the remote is missing.
        """
        self._require_remote("push", remote)
        self._run("push", "--quiet", "--set-upstream", remote, branch)

    def pull(self, branch: str, remote: str = "origin") -> None:
        """Fast-forward a local branch from a remote.

        Args:
            branch: Local branch to update.
            remote: Remote name.

        Raises:
            GitOperationError: If the pull fails or is not a fast-forward.
        """
        self._require_remote("pull", remote)
        with self.checked_out(branch):
            self._run("pull", "--quiet", "--ff-only", remote, branch)

    @contextmanager
    def temporary_clone(self) -> Iterator["GitRepository"]:
        """Clone this repository into a temporary directory.

        The clone has the same local branches. It is deleted when the
        context exits, even on error, so experiments (such as a dry-run
        merge) never touch the real working tree.

        Yields:
            The temporary clone.
        """
        workdir = Path(tempfile.mkdtemp(prefix="gitconfig-cli-"))
        _log.debug("clone.temporary.created", path=str(workdir))
        clone: GitRepository | None = None
        try:
            clone = GitRepository.clone(str(self._root), workdir / "repo")
            clone._copy_identity_from(self)
            yield clone
        finally:
            if clone is not None:
                clone.close()
            _remove_tree(workdir)
            _log.debug("clone.temporary.removed", path=str(workdir))

    # -- .gitignore ------------------------------------------------------

    def _gitignore_text(self) -> str:
        """Return the current ``.gitignore`` content, or ``""``."""
        try:
            return self.read_working_file(GITIGNORE)
        except FileNotFoundError:
            return ""

    def ignored_patterns(self) -> list[str]:
        """Patterns in the tool-managed block of ``.gitignore``.

        Returns:
            The managed patterns, in file order.
        """
        return gitignore.managed_patterns(self._gitignore_text())

    def set_ignored_patterns(self, patterns: Iterable[str]) -> bool:
        """Rewrite the tool-managed block of ``.gitignore`` (not committed).

        Args:
            patterns: Patterns the block should contain.

        Returns:
            ``True`` if the file content changed.
        """
        current = self._gitignore_text()
        updated = gitignore.render(current, patterns)
        if updated == current:
            return False
        self.write_file(GITIGNORE, updated)
        return True
