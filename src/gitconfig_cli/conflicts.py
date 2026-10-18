"""Analysis and automatic resolution of merge conflicts.

When Git reports conflicts, :class:`ConflictResolver` reads the three
versions of every conflicted file (common ancestor, target, source),
merges configuration files key by key with
:func:`~gitconfig_cli.config_merge.three_way_merge`, and reports exactly
which keys still conflict. It depends only on the abstractions, never on
GitPython.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from gitconfig_cli.abstractions import (
    ConfigSerializer,
    MergePreference,
    VersionControl,
)
from gitconfig_cli.config_merge import three_way_merge
from gitconfig_cli.exceptions import ConfigFormatError
from gitconfig_cli.models import ABSENT, FileConflict

CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")
SEPARATOR_MARKER = "======="


@dataclass(frozen=True)
class Resolution:
    """Result of analysing a set of conflicted files.

    Attributes:
        resolved: New content for each file that could be resolved
            (``None`` means the file should be deleted).
        unresolved: Files that still need a human decision.
    """

    resolved: dict[str, str | None] = field(default_factory=dict)
    unresolved: list[FileConflict] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """``True`` when every conflicted file was resolved."""
        return not self.unresolved


def has_conflict_markers(text: str) -> bool:
    """Tell whether text still contains Git conflict markers.

    Args:
        text: File content.

    Returns:
        ``True`` if a line starts with ``<<<<<<<`` or ``>>>>>>>`` or is
        exactly ``=======``.
    """
    for line in text.splitlines():
        if line.startswith(CONFLICT_MARKERS) or line.rstrip() == SEPARATOR_MARKER:
            return True
    return False


class ConflictResolver:
    """Resolve textual conflicts in configuration files semantically."""

    def __init__(self, repo: VersionControl, parser: ConfigSerializer) -> None:
        """Initialize the resolver.

        Args:
            repo: Repository in which a merge is being performed.
            parser: Serializer used to parse and rewrite configuration files.
        """
        self._repo = repo
        self._parser = parser

    def _read(self, path: str, ref: str | None) -> str | None:
        """Return a file's text at ``ref``, or ``None`` if it does not exist."""
        if ref is None:
            return None
        try:
            return self._repo.read_file(path, ref)
        except FileNotFoundError:
            return None

    def _parse(self, path: str, text: str | None) -> Any:
        """Parse text, mapping a missing file to ``ABSENT``."""
        return ABSENT if text is None else self._parser.loads(path, text)

    def analyze(
        self,
        files: Iterable[str],
        target: str,
        source: str,
        prefer: MergePreference | None = None,
    ) -> Resolution:
        """Work out how each conflicted file can be resolved.

        Args:
            files: Files Git reported as conflicted.
            target: Ref of the branch being merged into (``"ours"``).
            source: Ref of the branch being merged in (``"theirs"``).
            prefer: Side that wins when both changed the same key.

        Returns:
            Content for every resolvable file and details for the rest.
        """
        base = self._repo.merge_base(target, source)
        resolved: dict[str, str | None] = {}
        unresolved: list[FileConflict] = []
        for path in files:
            texts = [self._read(path, ref) for ref in (base, target, source)]
            if not self._parser.supports(path):
                if prefer is None:
                    unresolved.append(FileConflict(path))
                else:
                    resolved[path] = texts[1] if prefer == "ours" else texts[2]
                continue
            try:
                base_data, ours, theirs = (self._parse(path, t) for t in texts)
            except ConfigFormatError:
                unresolved.append(FileConflict(path))
                continue
            merged, conflicts = three_way_merge(base_data, ours, theirs, prefer)
            if conflicts:
                unresolved.append(FileConflict(path, conflicts))
            elif merged is ABSENT:
                resolved[path] = None
            else:
                resolved[path] = self._parser.dumps(path, merged)
        return Resolution(resolved=resolved, unresolved=unresolved)

    def apply(self, resolution: Resolution) -> list[str]:
        """Write resolved files into the working tree.

        Deleted files are removed and staged; written files are returned so
        the caller can stage them in the merge commit.

        Args:
            resolution: Output of :meth:`analyze`.

        Returns:
            Paths that were written and need staging.
        """
        written: list[str] = []
        for path, text in resolution.resolved.items():
            if text is None:
                self._repo.remove_file(path)
            else:
                self._repo.write_file(path, text)
                written.append(path)
        return written
