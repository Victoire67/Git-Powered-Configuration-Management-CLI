"""Management of the ``.gitignore`` file in a configuration repository.

The tool owns a clearly delimited block inside ``.gitignore``. Lines the
user writes outside the block are always preserved; only the block is
rewritten. All functions here are pure (text in, text out).
"""

from collections.abc import Iterable

BLOCK_START = "# >>> managed by gitconfig-cli >>>"
BLOCK_END = "# <<< managed by gitconfig-cli <<<"

DEFAULT_PATTERNS: tuple[str, ...] = (
    # Editor swap / backup files
    "*.swp",
    "*~",
    "*.bak",
    "*.tmp",
    # Leftovers from manual merge-conflict resolution
    "*.orig",
    "*.rej",
    # OS metadata
    ".DS_Store",
    "Thumbs.db",
    # Secrets and machine-local overrides must never be versioned
    ".env",
    "*.local.json",
    "*.local.yaml",
    "*.local.yml",
    # Tool-local state
    ".gitconfig-cli/",
)


def managed_patterns(text: str) -> list[str]:
    """Extract the patterns inside the managed block.

    Args:
        text: Current ``.gitignore`` content.

    Returns:
        The patterns in the managed block, in order (empty if no block).
    """
    patterns: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == BLOCK_START:
            inside = True
        elif stripped == BLOCK_END:
            inside = False
        elif inside and stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def render(text: str, patterns: Iterable[str]) -> str:
    """Return ``.gitignore`` content whose managed block holds ``patterns``.

    Duplicate and blank patterns are dropped. User lines outside the block
    are kept in place; if no block exists yet it is appended.

    Args:
        text: Current ``.gitignore`` content (may be empty).
        patterns: Patterns the managed block should contain.

    Returns:
        The new file content, ending with a newline.
    """
    unique = list(dict.fromkeys(p.strip() for p in patterns if p.strip()))
    block = [BLOCK_START, *unique, BLOCK_END]

    outside_before: list[str] = []
    outside_after: list[str] = []
    seen_block = False
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == BLOCK_START:
            inside, seen_block = True, True
        elif stripped == BLOCK_END:
            inside = False
        elif not inside:
            (outside_after if seen_block else outside_before).append(line)

    while outside_before and not outside_before[-1].strip():
        outside_before.pop()
    lines = [*outside_before, ""] if outside_before else []
    lines += block
    if any(line.strip() for line in outside_after):
        lines += (
            outside_after if outside_after[0].strip() == "" else ["", *outside_after]
        )
    return "\n".join(lines).rstrip("\n") + "\n"
