"""Abstract interfaces that the application logic depends on.

Following the Dependency Inversion Principle, :class:`~gitconfig_cli.manager.
ConfigManager` (and therefore the CLI) only knows about these interfaces.
The concrete implementations live in :mod:`gitconfig_cli.config_parser`
and :mod:`gitconfig_cli.git_repository`, and tests can substitute fakes or
mocks without touching Git or the file system.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal

from gitconfig_cli.models import CommitInfo, MergeResult

ConfigData = dict[str, Any]
"""A parsed configuration document: always a mapping at the top level."""

MergePreference = Literal["ours", "theirs"]
"""Git's ``-X`` strategy option: which side wins conflicting hunks."""


class VersionControl(ABC):
    """Version-control operations needed to manage configurations.

    The vocabulary is Git's (branches, commits, merges) but nothing here
    exposes GitPython types, so the manager can be tested with fakes.
    """

    @property
    @abstractmethod
    def root(self) -> Path:
        """Root directory of the working tree."""

    # -- branches -------------------------------------------------------

    @abstractmethod
    def current_branch(self) -> str:
        """Return the checked-out branch name.

        Raises:
            InvalidRepoError: If HEAD is detached.
        """

    @abstractmethod
    def branch_exists(self, name: str) -> bool:
        """Tell whether a local branch exists.

        Args:
            name: Branch name.
        """

    @abstractmethod
    def list_branches(self, prefix: str = "") -> list[str]:
        """List local branches, optionally only those starting with ``prefix``.

        Args:
            prefix: Branch name prefix such as ``"draft/"``.
        """

    @abstractmethod
    def create_branch(self, name: str, start_point: str) -> None:
        """Create a branch without checking it out.

        Args:
            name: New branch name.
            start_point: Branch or commit to start from.

        Raises:
            GitOperationError: If Git refuses (e.g. invalid name).
        """

    @abstractmethod
    def delete_branch(self, name: str, force: bool = False) -> None:
        """Delete a local branch.

        Args:
            name: Branch to delete.
            force: Delete even if it is not fully merged.

        Raises:
            GitOperationError: If Git refuses (e.g. unmerged and not forced).
        """

    @abstractmethod
    def checkout(self, name: str) -> None:
        """Check out a branch.

        Args:
            name: Branch to check out.

        Raises:
            GitOperationError: If the checkout fails.
        """

    @abstractmethod
    def checked_out(self, name: str) -> AbstractContextManager[None]:
        """Temporarily check out a branch, restoring the original on exit.

        If the body raises, uncommitted changes made inside it are discarded
        before switching back, so the repository is never left half-edited.

        Args:
            name: Branch to check out for the duration of the block.
        """

    @abstractmethod
    def ahead_behind(self, base: str, head: str) -> tuple[int, int]:
        """Count commits unique to each side.

        Args:
            base: Reference branch (e.g. ``main``).
            head: Compared branch (e.g. a draft).

        Returns:
            ``(ahead, behind)``: commits only on ``head``, commits only on
            ``base``.
        """

    # -- working tree and commits --------------------------------------

    @abstractmethod
    def dirty_files(self) -> list[str]:
        """List modified, staged or untracked files (ignored files excluded)."""

    @abstractmethod
    def ensure_clean(self) -> None:
        """Fail if the working tree has uncommitted changes.

        Raises:
            DirtyWorkingTreeError: If :meth:`dirty_files` is not empty.
        """

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write a file in the working tree (UTF-8, ``\\n`` newlines).

        Args:
            path: Path relative to :attr:`root`.
            content: New file content.
        """

    @abstractmethod
    def read_working_file(self, path: str) -> str:
        """Read a file from the working tree.

        Args:
            path: Path relative to :attr:`root`.

        Raises:
            FileNotFoundError: If the file does not exist.
        """

    @abstractmethod
    def remove_file(self, path: str) -> None:
        """Delete a tracked file from the working tree and stage the removal.

        Args:
            path: Path relative to :attr:`root`.
        """

    @abstractmethod
    def stage(self, paths: Sequence[str]) -> None:
        """Stage files for the next commit (also marks conflicts resolved).

        Args:
            paths: Paths relative to :attr:`root`.
        """

    @abstractmethod
    def commit(self, message: str, paths: Sequence[str] = ()) -> str:
        """Stage ``paths`` (if any) and commit everything staged.

        Args:
            message: Commit message.
            paths: Files to stage before committing.

        Returns:
            The new commit hash.

        Raises:
            GitOperationError: If the commit fails.
        """

    # -- reading history -----------------------------------------------

    @abstractmethod
    def read_file(self, path: str, ref: str) -> str:
        """Read a file as it exists at a branch or commit.

        Args:
            path: Path relative to :attr:`root`.
            ref: Branch name or commit hash.

        Raises:
            FileNotFoundError: If the file does not exist at ``ref``.
            GitOperationError: If ``ref`` cannot be resolved.
        """

    @abstractmethod
    def list_files(self, ref: str) -> list[str]:
        """List all files tracked at a branch or commit.

        Args:
            ref: Branch name or commit hash.

        Raises:
            GitOperationError: If ``ref`` cannot be resolved.
        """

    @abstractmethod
    def resolve_commit(self, ref: str) -> str:
        """Resolve a branch, tag or (abbreviated) hash to a full commit hash.

        Args:
            ref: Reference to resolve.

        Raises:
            GitOperationError: If ``ref`` does not name a commit.
        """

    @abstractmethod
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
        """

    @abstractmethod
    def diff(self, base: str, head: str, paths: Sequence[str] = ()) -> str:
        """Unified diff of what ``head`` changed since it diverged from ``base``.

        Args:
            base: Reference branch (e.g. ``main``).
            head: Compared branch (e.g. a draft).
            paths: Restrict the diff to these files.
        """

    @abstractmethod
    def changed_files(self, old: str, new: str) -> list[str]:
        """List files that differ between two commits.

        Args:
            old: Earlier commit.
            new: Later commit.
        """

    @abstractmethod
    def merge_base(self, first: str, second: str) -> str | None:
        """Return the best common ancestor of two refs, if any.

        Args:
            first: A branch or commit.
            second: Another branch or commit.
        """

    # -- merging ---------------------------------------------------------

    @abstractmethod
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

        Args:
            source: Branch to merge in.
            message: Message of the merge (or squash) commit.
            prefer: Resolve conflicting hunks in favour of one side.
            squash: Combine the source's changes into a single commit.
            abort_on_conflict: Restore the pre-merge state on conflict. When
                ``False`` the conflicts are left in the working tree.
            commit: When ``False`` the merge is prepared but not committed
                (useful for dry runs).

        Returns:
            The merge outcome.

        Raises:
            MergeConflictError: If the merge produced conflicts.
            GitOperationError: If the merge failed for another reason.
        """

    @abstractmethod
    def is_merging(self) -> bool:
        """Tell whether a merge is in progress (``MERGE_HEAD`` exists)."""

    @abstractmethod
    def conflicted_files(self) -> list[str]:
        """List files with unresolved merge conflicts."""

    @abstractmethod
    def abort_merge(self) -> None:
        """Abort the merge in progress and restore the pre-merge state.

        Raises:
            GitOperationError: If no merge is in progress.
        """

    @abstractmethod
    def reset_hard(self, ref: str) -> None:
        """Move the current branch to ``ref``, discarding working tree changes.

        Args:
            ref: Commit to reset to.
        """

    # -- remotes and scratch copies -----------------------------------

    @abstractmethod
    def push(self, branch: str, remote: str = "origin") -> None:
        """Push a branch to a remote.

        Args:
            branch: Local branch to push.
            remote: Remote name.

        Raises:
            GitOperationError: If the push fails or the remote is missing.
        """

    @abstractmethod
    def pull(self, branch: str, remote: str = "origin") -> None:
        """Fast-forward a local branch from a remote.

        Args:
            branch: Local branch to update.
            remote: Remote name.

        Raises:
            GitOperationError: If the pull fails or is not a fast-forward.
        """

    @abstractmethod
    def temporary_clone(self) -> AbstractContextManager["VersionControl"]:
        """Clone this repository into a temporary directory.

        The clone has the same local branches. It is deleted when the
        context exits, even on error, so experiments (such as a dry-run
        merge) never touch the real working tree.
        """

    # -- .gitignore ------------------------------------------------------

    @abstractmethod
    def ignored_patterns(self) -> list[str]:
        """Patterns in the tool-managed block of ``.gitignore``."""

    @abstractmethod
    def set_ignored_patterns(self, patterns: Iterable[str]) -> bool:
        """Rewrite the tool-managed block of ``.gitignore`` (not committed).

        Args:
            patterns: Patterns the block should contain.

        Returns:
            ``True`` if the file content changed.
        """


class ConfigSerializer(ABC):
    """Converts configuration documents between text and Python data."""

    @property
    @abstractmethod
    def supported_extensions(self) -> tuple[str, ...]:
        """File extensions (with leading dot) this serializer understands."""

    @abstractmethod
    def supports(self, name: str) -> bool:
        """Tell whether a file name has a supported configuration extension.

        Args:
            name: File name or relative path, e.g. ``"app.json"``.

        Returns:
            ``True`` if :meth:`loads` / :meth:`dumps` can handle the file.
        """

    @abstractmethod
    def loads(self, name: str, text: str) -> ConfigData:
        """Parse configuration text.

        Args:
            name: File name; its extension selects the format.
            text: Raw file content.

        Returns:
            The parsed configuration mapping.

        Raises:
            ConfigFormatError: If the format is unsupported, the text is
                malformed, or the top-level value is not a mapping.
        """

    @abstractmethod
    def dumps(self, name: str, data: ConfigData) -> str:
        """Serialize configuration data to text.

        Args:
            name: File name; its extension selects the format.
            data: The configuration mapping to serialize.

        Returns:
            The file content, ending with a newline.

        Raises:
            ConfigFormatError: If the format is unsupported or the data
                cannot be represented in it.
        """

    @abstractmethod
    def load_file(self, path: Path) -> ConfigData:
        """Read and parse a configuration file from disk.

        Args:
            path: Path of the file; its extension selects the format.

        Returns:
            The parsed configuration mapping.

        Raises:
            ConfigNotFound: If the file does not exist.
            ConfigFormatError: If the file cannot be parsed.
        """
