"""Custom exceptions for gitconfig-cli.

Every exception inherits from GitConfigError, so callers (especially the
CLI) can catch any tool-specific error with a single ``except`` clause.
"""

from collections.abc import Sequence
from pathlib import Path


class GitConfigError(Exception):
    """Base class for all gitconfig-cli errors."""


class InvalidRepoError(GitConfigError):
    """Raised when a path is not a usable Git repository."""

    def __init__(self, path: Path, reason: str) -> None:
        """Initialize the error.

        Args:
            path: The path that was checked.
            reason: Why the repository cannot be used.
        """
        self.path = path
        self.reason = reason
        super().__init__(f"Invalid repository at '{path}': {reason}")


class ConfigNotFound(GitConfigError):
    """Raised when a configuration file does not exist."""

    def __init__(self, name: str, branch: str | None = None) -> None:
        """Initialize the error.

        Args:
            name: Name of the missing configuration file.
            branch: Branch that was searched, if relevant.
        """
        self.name = name
        self.branch = branch
        location = f" on branch '{branch}'" if branch else ""
        super().__init__(f"Configuration '{name}' not found{location}")


class MergeConflictError(GitConfigError):
    """Raised when merging a draft branch produces conflicts."""

    def __init__(
        self, source: str, target: str, conflicted_files: Sequence[str]
    ) -> None:
        """Initialize the error.

        Args:
            source: Branch being merged (the draft).
            target: Branch being merged into (usually ``main``).
            conflicted_files: Files that contain conflicts.
        """
        self.source = source
        self.target = target
        self.conflicted_files = list(conflicted_files)
        files = ", ".join(self.conflicted_files)
        super().__init__(f"Merge conflict merging '{source}' into '{target}': {files}")


class GitOperationError(GitConfigError):
    """Raised when an underlying Git command fails unexpectedly."""

    def __init__(self, operation: str, details: str) -> None:
        """Initialize the error.

        Args:
            operation: The Git operation that failed (e.g. ``"push"``).
            details: Error output from Git.
        """
        self.operation = operation
        self.details = details
        super().__init__(f"Git '{operation}' failed: {details}")
