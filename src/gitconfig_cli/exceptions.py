"""Custom exceptions for gitconfig-cli.

Every exception inherits from GitConfigError, so callers (especially the
CLI) can catch any tool-specific error with a single ``except`` clause.
"""

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
