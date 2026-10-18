from pathlib import Path

import pytest

from gitconfig_cli.exceptions import (
    ConfigAlreadyExists,
    ConfigFormatError,
    ConfigNotFound,
    DirtyWorkingTreeError,
    DraftAlreadyExists,
    DraftNotFound,
    GitConfigError,
    GitOperationError,
    InvalidNameError,
    InvalidRepoError,
    KeyNotFound,
    MergeConflictError,
    MergeInProgressError,
    RevisionNotFound,
)


@pytest.mark.parametrize(
    "error_class",
    [InvalidRepoError, ConfigNotFound, MergeConflictError, GitOperationError],
)
def test_all_errors_inherit_from_base(error_class: type[Exception]) -> None:
    assert issubclass(error_class, GitConfigError)


def test_invalid_repo_error_stores_details_and_message() -> None:
    error = InvalidRepoError(Path("configs"), "not a Git repository")

    assert error.path == Path("configs")
    assert error.reason == "not a Git repository"
    assert str(error) == "Invalid repository at 'configs': not a Git repository"


def test_invalid_repo_error_can_be_caught_as_base_error() -> None:
    with pytest.raises(GitConfigError, match="not a Git repository"):
        raise InvalidRepoError(Path("configs"), "not a Git repository")


def test_config_not_found_without_branch() -> None:
    error = ConfigNotFound("app.json")

    assert error.name == "app.json"
    assert error.branch is None
    assert str(error) == "Configuration 'app.json' not found"


def test_config_not_found_with_branch() -> None:
    error = ConfigNotFound("app.json", branch="draft/new-port")

    assert error.branch == "draft/new-port"
    assert str(error) == (
        "Configuration 'app.json' not found on branch 'draft/new-port'"
    )


def test_merge_conflict_error_stores_details_and_message() -> None:
    error = MergeConflictError("draft/new-port", "main", ["app.json", "db.yaml"])

    assert error.source == "draft/new-port"
    assert error.target == "main"
    assert error.conflicted_files == ["app.json", "db.yaml"]
    assert str(error) == (
        "Merge conflict merging 'draft/new-port' into 'main': app.json, db.yaml"
    )


def test_merge_conflict_error_keeps_its_own_copy_of_files() -> None:
    files = ["app.json"]
    error = MergeConflictError("draft/new-port", "main", files)

    files.append("db.yaml")

    assert error.conflicted_files == ["app.json"]


def test_merge_conflict_error_accepts_a_tuple() -> None:
    error = MergeConflictError("draft/new-port", "main", ("app.json",))

    assert error.conflicted_files == ["app.json"]


def test_git_operation_error_stores_details_and_message() -> None:
    error = GitOperationError("push", "remote rejected")

    assert error.operation == "push"
    assert error.details == "remote rejected"
    assert str(error) == "Git 'push' failed: remote rejected"


def test_git_operation_error_keeps_the_original_cause() -> None:
    original = RuntimeError("network unreachable")

    with pytest.raises(GitOperationError) as exc_info:
        raise GitOperationError("push", str(original)) from original

    assert exc_info.value.__cause__ is original


@pytest.mark.parametrize(
    "error_class",
    [
        ConfigAlreadyExists,
        ConfigFormatError,
        KeyNotFound,
        DraftNotFound,
        DraftAlreadyExists,
        DirtyWorkingTreeError,
    ],
)
def test_workflow_errors_inherit_from_base(error_class: type[Exception]) -> None:
    assert issubclass(error_class, GitConfigError)


def test_config_already_exists_message() -> None:
    error = ConfigAlreadyExists("app.json", "draft/new-port")

    assert error.name == "app.json"
    assert error.branch == "draft/new-port"
    assert str(error) == (
        "Configuration 'app.json' already exists on branch 'draft/new-port'"
    )


def test_config_format_error_message() -> None:
    error = ConfigFormatError("app.json", "expecting value")

    assert error.name == "app.json"
    assert error.reason == "expecting value"
    assert str(error) == "Invalid configuration 'app.json': expecting value"


def test_key_not_found_message() -> None:
    error = KeyNotFound("app.json", "db.port")

    assert error.name == "app.json"
    assert error.key == "db.port"
    assert str(error) == "Key 'db.port' not found in configuration 'app.json'"


def test_draft_errors_messages() -> None:
    assert DraftNotFound("new-port").draft == "new-port"
    assert str(DraftNotFound("new-port")) == "Draft 'new-port' does not exist"
    assert DraftAlreadyExists("new-port").draft == "new-port"
    assert str(DraftAlreadyExists("new-port")) == "Draft 'new-port' already exists"


def test_dirty_working_tree_error_lists_files() -> None:
    error = DirtyWorkingTreeError(Path("configs"), ["app.json", "notes.txt"])

    assert error.files == ["app.json", "notes.txt"]
    assert str(error) == (
        "Repository at 'configs' has uncommitted changes: app.json, notes.txt"
    )


def test_dirty_working_tree_error_without_files() -> None:
    error = DirtyWorkingTreeError(Path("configs"), [])

    assert str(error).endswith("uncommitted changes: unknown files")


def test_invalid_name_error_message() -> None:
    error = InvalidNameError("draft", "bad name", "spaces are not allowed")

    assert (error.kind, error.name) == ("draft", "bad name")
    assert error.reason == "spaces are not allowed"
    assert str(error) == "Invalid draft name 'bad name': spaces are not allowed"


def test_merge_in_progress_error_message() -> None:
    error = MergeInProgressError("draft/new-port", ["app.json"])

    assert error.branch == "draft/new-port"
    assert error.conflicted_files == ["app.json"]
    assert str(error) == (
        "A merge is in progress on branch 'draft/new-port' "
        "(conflicted files: app.json)"
    )


def test_merge_in_progress_error_when_all_resolved() -> None:
    assert "none left" in str(MergeInProgressError("draft/x", []))


def test_revision_not_found_message() -> None:
    error = RevisionNotFound("app.json", "abc1234")

    assert (error.name, error.revision) == ("app.json", "abc1234")
    assert str(error) == "Revision 'abc1234' of configuration 'app.json' not found"
