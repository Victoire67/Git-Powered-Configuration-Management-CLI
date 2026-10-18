"""Human-readable rendering of results and errors for the CLI.

Everything here turns data objects or exceptions into plain text; no
function performs Git or file operations. Keeping presentation separate
from :mod:`gitconfig_cli.cli` keeps the commands short and lets the
formatting be tested on its own.
"""

import json
from collections.abc import Sequence
from typing import Any

from gitconfig_cli.exceptions import (
    ConfigNotFound,
    DirtyWorkingTreeError,
    DraftAlreadyExists,
    DraftNotFound,
    GitConfigError,
    GitOperationError,
    InvalidRepoError,
    KeyNotFound,
    MergeConflictError,
    MergeInProgressError,
    NoMergeInProgress,
    RevisionNotFound,
)
from gitconfig_cli.models import (
    ABSENT,
    CommitInfo,
    FileConflict,
    MergeResult,
    PublishPreview,
    PublishResult,
    RepoStatus,
)

PROG = "gitconfig-cli"

EXIT_ERROR = 1
EXIT_CONFLICT = 3


def format_value(value: Any) -> str:
    """Render a configuration value compactly (JSON style).

    Args:
        value: Any configuration value, or ``ABSENT``.

    Returns:
        ``<absent>`` for missing values, the string itself for strings,
        and compact JSON for everything else.
    """
    if value is ABSENT:
        return "<absent>"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _side(branch: str, main_branch: str) -> str:
    """Short label for a branch in conflict tables: 'main' or 'draft'."""
    return "main" if branch == main_branch else "draft"


def format_conflicts(
    details: Sequence[FileConflict], target: str, source: str, main_branch: str
) -> list[str]:
    """Describe conflicting keys file by file.

    Args:
        details: Conflict details from :class:`MergeConflictError`.
        target: Branch that was being merged into.
        source: Branch that was being merged in.
        main_branch: Name of the main branch (used to label sides).

    Returns:
        Output lines.
    """
    target_label = _side(target, main_branch)
    source_label = _side(source, main_branch)
    lines: list[str] = []
    for conflict in details:
        lines.append(f"  CONFLICT in {conflict.path}")
        if not conflict.keys:
            lines.append("    (cannot be compared key by key; resolve it manually)")
        for key in conflict.keys:
            lines.append(
                f"    {key.key}: base={format_value(key.base)}  "
                f"{target_label}={format_value(key.target)}  "
                f"{source_label}={format_value(key.source)}"
            )
    return lines


def _draft_name(branch: str) -> str:
    """Strip the ``draft/`` prefix from a branch name."""
    return branch.split("/", 1)[1] if branch.startswith("draft/") else branch


def conflict_guidance(error: MergeConflictError, main_branch: str) -> list[str]:
    """Step-by-step instructions for resolving a merge conflict.

    Args:
        error: The conflict that occurred.
        main_branch: Name of the main branch.

    Returns:
        Output lines.
    """
    if error.in_progress:
        return [
            f"The merge was left in progress on '{error.target}' so you can fix it:",
            "  1. Edit each conflicted file and remove the <<<<<<< ======= >>>>>>>",
            "     markers, keeping the values you want.",
            f"  2. Run '{PROG} resolve' to validate the files and commit the merge.",
            f"  3. Run '{PROG} publish {_draft_name(error.target)}' to publish.",
            f"Or run '{PROG} abort' to cancel the merge.",
        ]
    draft = _draft_name(error.source)
    return [
        f"'{main_branch}' was not changed. To resolve the conflict, either:",
        "  a) keep one side for the conflicting keys:",
        f"       {PROG} publish {draft} --prefer draft   (use the draft's values)",
        f"       {PROG} publish {draft} --prefer main    (keep {main_branch}'s values)",
        "  b) or resolve by hand:",
        f"       {PROG} draft sync {draft}     (bring {main_branch} into the draft)",
        "       edit the conflicted files, then",
        f"       {PROG} resolve",
        f"       {PROG} publish {draft}",
    ]


def hints_for(error: GitConfigError) -> list[str]:
    """Suggest what to do next after an error.

    Args:
        error: The error that stopped the command.

    Returns:
        Hint lines (possibly empty).
    """
    if isinstance(error, InvalidRepoError):
        return [
            (
                f"Run '{PROG} init' to create a configuration repository here, "
                "or point to one with --repo PATH."
            )
        ]
    if isinstance(error, DirtyWorkingTreeError):
        return [
            (
                "Commit, stash ('git stash') or discard these changes, then retry. "
                "The tool refuses to run so it never overwrites your edits."
            )
        ]
    if isinstance(error, MergeInProgressError):
        return [
            (
                f"Finish the merge with '{PROG} resolve' after fixing the files, "
                f"or cancel it with '{PROG} abort'."
            )
        ]
    if isinstance(error, NoMergeInProgress):
        return [f"Use '{PROG} status' to see the repository state."]
    if isinstance(error, DraftNotFound):
        return [
            (
                f"See existing drafts with '{PROG} draft list' or create one with "
                f"'{PROG} draft create {error.draft}'."
            )
        ]
    if isinstance(error, DraftAlreadyExists):
        return [
            f"Pick another name or delete it with '{PROG} draft delete {error.draft}'."
        ]
    if isinstance(error, ConfigNotFound):
        return [
            (
                f"List configurations with '{PROG} list', or create it with "
                f"'{PROG} create {error.name} --draft NAME'."
            )
        ]
    if isinstance(error, KeyNotFound):
        return [f"Show the whole file with '{PROG} show {error.name}'."]
    if isinstance(error, RevisionNotFound):
        return [f"List available versions with '{PROG} history {error.name}'."]
    if isinstance(error, GitOperationError) and "who you are" in error.details:
        return [
            (
                "Configure your Git identity: git config --global user.name 'Your Name' "
                "&& git config --global user.email you@example.com"
            )
        ]
    return []


def format_history(commits: Sequence[CommitInfo]) -> list[str]:
    """Render commits as aligned rows.

    Args:
        commits: Commits, newest first.

    Returns:
        One line per commit: short hash, date, author, message.
    """
    return [
        f"{c.short_sha}  {c.date[:19].replace('T', ' ')}  {c.author:<16}  {c.message}"
        for c in commits
    ]


def format_status(status: RepoStatus, root: str, main_branch: str) -> list[str]:
    """Describe the repository state.

    Args:
        status: State snapshot.
        root: Repository root path.
        main_branch: Name of the main branch.

    Returns:
        Output lines.
    """
    lines = [f"Repository: {root}", f"On branch:  {status.branch}"]
    if status.merging:
        files = ", ".join(status.conflicts) or "none left - run 'resolve'"
        lines.append(f"MERGE IN PROGRESS - conflicted files: {files}")
    if status.dirty_files:
        lines.append("Uncommitted changes: " + ", ".join(status.dirty_files))
    if status.drafts:
        lines.append("Drafts:")
        lines += [
            f"  {d.name:<24} {d.ahead} ahead, {d.behind} behind {main_branch}"
            for d in status.drafts
        ]
    else:
        lines.append("Drafts: none")
    return lines


def format_publish(result: PublishResult) -> list[str]:
    """Summarize a publish.

    Args:
        result: Outcome of the publish.

    Returns:
        Output lines.
    """
    if result.up_to_date:
        return [f"Nothing to publish: draft '{result.draft}' has no new changes."]
    lines = [
        (
            f"Published draft '{result.draft}' to '{result.target}' "
            f"(commit {result.commit[:7]})."
        )
    ]
    if result.files:
        lines.append("Changed: " + ", ".join(result.files))
    if result.auto_resolved:
        lines.append(
            "Automatically merged edits to different keys in: "
            + ", ".join(result.auto_resolved)
        )
    if result.draft_deleted:
        lines.append(f"Draft branch 'draft/{result.draft}' was deleted.")
    return lines


def format_preview(preview: PublishPreview, main_branch: str) -> list[str]:
    """Summarize a dry-run publish.

    Args:
        preview: Outcome of :meth:`ConfigManager.preview_publish`.
        main_branch: Name of the main branch.

    Returns:
        Output lines.
    """
    if preview.up_to_date:
        return [f"Dry run: draft '{preview.draft}' has nothing to publish."]
    if preview.can_publish:
        lines = [f"Dry run: draft '{preview.draft}' can be published cleanly."]
        if preview.files:
            lines.append("Would change: " + ", ".join(preview.files))
        if preview.auto_resolved:
            lines.append("Would auto-merge: " + ", ".join(preview.auto_resolved))
        return lines
    lines = [f"Dry run: publishing draft '{preview.draft}' would fail."]
    lines += format_conflicts(
        preview.conflicts, main_branch, f"draft/{preview.draft}", main_branch
    )
    lines += [f"  {problem}" for problem in preview.problems]
    return lines


def format_sync(result: MergeResult, draft: str, main_branch: str) -> list[str]:
    """Summarize a draft sync.

    Args:
        result: Outcome of the merge of main into the draft.
        draft: Draft name.
        main_branch: Name of the main branch.

    Returns:
        Output lines.
    """
    if result.up_to_date:
        return [f"Draft '{draft}' is already up to date with '{main_branch}'."]
    lines = [
        f"Merged '{main_branch}' into draft '{draft}' (commit {result.commit[:7]})."
    ]
    if result.auto_resolved:
        lines.append(
            "Automatically merged edits to different keys in: "
            + ", ".join(result.auto_resolved)
        )
    return lines
