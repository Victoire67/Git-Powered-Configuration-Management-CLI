"""Key-by-key three-way merge of configuration data.

Git merges files line by line, so two people editing *different* keys
that sit on neighbouring lines of a JSON/YAML file get a "conflict" even
though their changes are compatible. This module merges the *parsed* data
instead: a key only conflicts when both sides changed it to different
values.

The algorithm, for each key, compares the common ancestor (``base``) with
both sides (``ours`` = the branch being merged into, ``theirs`` = the
branch being merged in):

* both sides equal          -> keep that value
* only one side changed it  -> take the changed side
* both changed, both mappings -> merge recursively
* both changed differently  -> conflict (or the preferred side wins)

Lists and scalars are compared as whole values. A key missing on a side
is represented by :data:`~gitconfig_cli.models.ABSENT`, so deletions merge
like any other change.
"""

from typing import Any

from gitconfig_cli.abstractions import MergePreference
from gitconfig_cli.models import ABSENT, ROOT_KEY, KeyConflict


def three_way_merge(
    base: Any,
    ours: Any,
    theirs: Any,
    prefer: MergePreference | None = None,
    path: str = "",
) -> tuple[Any, list[KeyConflict]]:
    """Merge two versions of a value that share a common ancestor.

    Example:
        >>> base = {"port": 80, "debug": False}
        >>> ours = {"port": 8080, "debug": False}
        >>> theirs = {"port": 80, "debug": True}
        >>> three_way_merge(base, ours, theirs)
        ({'port': 8080, 'debug': True}, [])

    Args:
        base: Value at the common ancestor (``ABSENT`` if it did not exist).
        ours: Value on the branch being merged into.
        theirs: Value on the branch being merged in.
        prefer: On a real conflict, take this side instead of reporting it.
        path: Dotted path of the value (used for reporting; leave empty).

    Returns:
        ``(merged, conflicts)``. ``merged`` may be ``ABSENT`` if the value
        should be deleted. When there are conflicts, ``merged`` holds
        "ours" at each conflicting key.
    """
    if ours == theirs:
        return ours, []
    if base == ours:
        return theirs, []
    if base == theirs:
        return ours, []

    if isinstance(ours, dict) and isinstance(theirs, dict):
        base_map = base if isinstance(base, dict) else {}
        merged: dict[str, Any] = {}
        conflicts: list[KeyConflict] = []
        keys = [*ours, *(key for key in theirs if key not in ours)]
        for key in keys:
            value, found = three_way_merge(
                base_map.get(key, ABSENT),
                ours.get(key, ABSENT),
                theirs.get(key, ABSENT),
                prefer,
                f"{path}.{key}" if path else str(key),
            )
            conflicts.extend(found)
            if value is not ABSENT:
                merged[key] = value
        return merged, conflicts

    if prefer == "ours":
        return ours, []
    if prefer == "theirs":
        return theirs, []
    return ours, [KeyConflict(path or ROOT_KEY, base, ours, theirs)]
