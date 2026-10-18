from gitconfig_cli.gitignore import (
    BLOCK_END,
    BLOCK_START,
    DEFAULT_PATTERNS,
    managed_patterns,
    render,
)


def test_render_into_empty_file() -> None:
    assert render("", ["*.tmp", ".env"]) == f"{BLOCK_START}\n*.tmp\n.env\n{BLOCK_END}\n"


def test_render_drops_duplicates_and_blanks() -> None:
    text = render("", ["*.tmp", " *.tmp ", "", ".env"])

    assert managed_patterns(text) == ["*.tmp", ".env"]


def test_render_appends_block_after_user_lines() -> None:
    text = render("secrets/\n\n", ["*.tmp"])

    assert text == f"secrets/\n\n{BLOCK_START}\n*.tmp\n{BLOCK_END}\n"


def test_render_replaces_existing_block_and_keeps_user_lines() -> None:
    original = f"user-before\n\n{BLOCK_START}\nold\n{BLOCK_END}\n\nuser-after\n"

    text = render(original, ["new"])

    assert text == f"user-before\n\n{BLOCK_START}\nnew\n{BLOCK_END}\n\nuser-after\n"


def test_render_separates_user_lines_after_block() -> None:
    original = f"{BLOCK_START}\nold\n{BLOCK_END}\nuser-after\n"

    assert render(original, ["new"]).endswith(f"{BLOCK_END}\n\nuser-after\n")


def test_render_is_idempotent() -> None:
    once = render("custom\n", DEFAULT_PATTERNS)

    assert render(once, DEFAULT_PATTERNS) == once


def test_managed_patterns_skip_comments_and_outside_lines() -> None:
    text = f"outside\n{BLOCK_START}\n# comment\n*.bak\n\n{BLOCK_END}\n"

    assert managed_patterns(text) == ["*.bak"]


def test_managed_patterns_without_block() -> None:
    assert managed_patterns("*.log\n") == []


def test_default_patterns_cover_merge_leftovers_and_secrets() -> None:
    assert "*.orig" in DEFAULT_PATTERNS
    assert ".env" in DEFAULT_PATTERNS
