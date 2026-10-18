from gitconfig_cli.exceptions import GitConfigError, MergeConflictError
from gitconfig_cli.models import (
    ABSENT,
    FileConflict,
    KeyConflict,
    MergeResult,
    PublishPreview,
    PublishResult,
)
from gitconfig_cli.output import (
    conflict_guidance,
    format_conflicts,
    format_preview,
    format_publish,
    format_sync,
    format_value,
    hints_for,
)


def test_format_value() -> None:
    assert format_value(ABSENT) == "<absent>"
    assert format_value("text") == "text"
    assert format_value(None) == "null"
    assert format_value({"a": [1, True]}) == '{"a": [1, true]}'


def test_format_conflicts_labels_sides_and_whole_files() -> None:
    details = [
        FileConflict("app.json", [KeyConflict("db.port", 1, 2, ABSENT)]),
        FileConflict("README.md"),
    ]

    lines = format_conflicts(details, "main", "draft/x", "main")

    assert lines == [
        "  CONFLICT in app.json",
        "    db.port: base=1  main=2  draft=<absent>",
        "  CONFLICT in README.md",
        "    (cannot be compared key by key; resolve it manually)",
    ]


def test_guidance_for_aborted_and_in_progress_merges() -> None:
    aborted = conflict_guidance(MergeConflictError("draft/x", "main", ["a"]), "main")
    kept = conflict_guidance(
        MergeConflictError("main", "draft/x", ["a"], in_progress=True), "main"
    )

    assert any("publish x --prefer draft" in line for line in aborted)
    assert any("left in progress on 'draft/x'" in line for line in kept)
    assert any("publish x'" in line for line in kept)


def test_no_hint_for_generic_errors() -> None:
    assert hints_for(GitConfigError("boom")) == []


def test_publish_without_config_changes_or_deletion() -> None:
    result = PublishResult(draft="x", target="main", commit="abcdef0123", files=[])

    assert format_publish(result) == ["Published draft 'x' to 'main' (commit abcdef0)."]


def test_preview_clean_without_files() -> None:
    assert format_preview(PublishPreview(draft="x"), "main") == [
        "Dry run: draft 'x' can be published cleanly."
    ]


def test_sync_without_auto_merge() -> None:
    lines = format_sync(MergeResult(commit="abcdef0123"), "x", "main")

    assert lines == ["Merged 'main' into draft 'x' (commit abcdef0)."]
