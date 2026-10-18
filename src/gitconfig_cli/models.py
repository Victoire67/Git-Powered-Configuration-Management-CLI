"""Plain data objects passed between the Git layer, the manager and the CLI.

They are immutable dataclasses with no behaviour, so every layer can
produce or consume them without depending on GitPython.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final


class _Absent(Enum):
    """Marker for "this key or file does not exist on that side"."""

    ABSENT = "absent"

    def __repr__(self) -> str:
        """Show as ``<absent>`` in reports."""
        return "<absent>"


ABSENT: Final = _Absent.ABSENT
"""Sentinel used in conflict reports for a missing key or file.

It is distinct from ``None``, which is a legitimate configuration value
(JSON ``null`` / YAML ``~``).
"""


@dataclass(frozen=True)
class CommitInfo:
    """Summary of one commit.

    Attributes:
        sha: Full commit hash.
        short_sha: Abbreviated hash (7 characters).
        author: Author name.
        date: Commit date in ISO 8601 format.
        message: First line of the commit message.
    """

    sha: str
    short_sha: str
    author: str
    date: str
    message: str


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a successful merge.

    Attributes:
        commit: Commit at the tip of the target branch after merging.
        files: Files changed by the merge.
        up_to_date: ``True`` when there was nothing to merge.
        auto_resolved: Files whose conflicts were resolved automatically.
    """

    commit: str
    files: list[str] = field(default_factory=list)
    up_to_date: bool = False
    auto_resolved: list[str] = field(default_factory=list)


ROOT_KEY: Final = "(entire file)"
"""Key reported when the conflict is about the whole file (e.g. deleted)."""


@dataclass(frozen=True)
class KeyConflict:
    """One configuration key changed differently on both sides of a merge.

    Attributes:
        key: Dotted key path, or :data:`ROOT_KEY` for whole-file conflicts.
        base: Value at the common ancestor (:data:`ABSENT` if missing).
        target: Value on the branch being merged into (e.g. ``main``).
        source: Value on the branch being merged (e.g. the draft).
    """

    key: str
    base: Any
    target: Any
    source: Any


@dataclass(frozen=True)
class FileConflict:
    """Conflict details for one configuration file.

    Attributes:
        path: Conflicted file.
        keys: Keys that were changed differently on both branches. Empty
            when the file could not be compared key by key.
    """

    path: str
    keys: list[KeyConflict] = field(default_factory=list)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of publishing a draft into the main branch.

    Attributes:
        draft: Name of the published draft.
        target: Branch that received the changes.
        commit: Resulting commit on the target branch.
        files: Configuration files changed by the publish.
        up_to_date: ``True`` when the draft had nothing new to publish.
        draft_deleted: Whether the draft branch was removed afterwards.
        auto_resolved: Files whose textual conflicts were resolved
            automatically, key by key.
    """

    draft: str
    target: str
    commit: str
    files: list[str] = field(default_factory=list)
    up_to_date: bool = False
    draft_deleted: bool = False
    auto_resolved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PublishPreview:
    """What publishing a draft would do, computed without touching main.

    Attributes:
        draft: Name of the draft.
        files: Configuration files the publish would change.
        conflicts: Conflicts that would stop the publish.
        auto_resolved: Files whose textual conflicts would be resolved
            automatically because different keys were changed.
        problems: Other blocking problems (e.g. a merge producing invalid
            JSON/YAML).
        up_to_date: ``True`` if the draft has nothing to publish.
    """

    draft: str
    files: list[str] = field(default_factory=list)
    conflicts: list[FileConflict] = field(default_factory=list)
    auto_resolved: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    up_to_date: bool = False

    @property
    def can_publish(self) -> bool:
        """Whether ``publish`` would succeed with the same options."""
        return not self.conflicts and not self.problems


@dataclass(frozen=True)
class DraftInfo:
    """A draft branch and how it relates to the main branch.

    Attributes:
        name: Draft name (without the ``draft/`` prefix).
        branch: Full branch name.
        ahead: Commits on the draft that are not on main.
        behind: Commits on main that are not on the draft.
    """

    name: str
    branch: str
    ahead: int
    behind: int


@dataclass(frozen=True)
class RepoStatus:
    """Snapshot of the configuration repository state.

    Attributes:
        branch: Currently checked-out branch.
        dirty_files: Files with uncommitted changes.
        merging: Whether a merge is in progress.
        conflicts: Files with unresolved conflicts.
        drafts: Existing drafts.
    """

    branch: str
    dirty_files: list[str]
    merging: bool
    conflicts: list[str]
    drafts: list[DraftInfo]
