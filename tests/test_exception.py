import pytest
from pathlib import Path

from gitconfig_cli.exceptions import (
    ConfigNotFound,
    GitConfigError,
    GitOperationError,
    InvalidRepoError,
    MergeConflictError,
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
