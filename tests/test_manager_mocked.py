"""ConfigManager tests with a mocked VersionControl.

Because the manager depends on the VersionControl abstraction, failure
scenarios (a rejected push, a commit that fails, a broken checkout) can be
simulated without running Git at all.
"""

from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from structlog.testing import capture_logs

from gitconfig_cli.abstractions import VersionControl
from gitconfig_cli.config_parser import ConfigParser
from gitconfig_cli.exceptions import (
    DirtyWorkingTreeError,
    GitOperationError,
    MergeConflictError,
)
from gitconfig_cli.manager import ConfigManager
from gitconfig_cli.models import MergeResult


@pytest.fixture
def vcs(mocker: MockerFixture) -> MagicMock:
    mock = mocker.create_autospec(VersionControl, instance=True)
    mock.is_merging.return_value = False
    mock.current_branch.return_value = "main"
    mock.branch_exists.return_value = True
    mock.checked_out.side_effect = lambda _branch: nullcontext()
    return mock


@pytest.fixture
def manager(vcs: MagicMock) -> ConfigManager:
    return ConfigManager(vcs, ConfigParser())


def test_failed_push_is_reported_and_audited(
    manager: ConfigManager, vcs: MagicMock
) -> None:
    vcs.push.side_effect = GitOperationError("push", "remote rejected")

    with capture_logs() as logs, pytest.raises(GitOperationError, match="rejected"):
        manager.push()

    vcs.push.assert_called_once_with("main", "origin")
    failed = logs[-1]
    assert failed["action"] == "remote.push"
    assert failed["event"] == "failed"
    assert failed["error_type"] == "GitOperationError"


def test_failed_pull_is_reported(manager: ConfigManager, vcs: MagicMock) -> None:
    vcs.pull.side_effect = GitOperationError("pull", "not a fast-forward")

    with pytest.raises(GitOperationError, match="fast-forward"):
        manager.pull("upstream")

    vcs.pull.assert_called_once_with("main", "upstream")


def test_push_draft_uses_draft_branch(manager: ConfigManager, vcs: MagicMock) -> None:
    assert manager.push(draft="new-port") == "draft/new-port"

    vcs.push.assert_called_once_with("draft/new-port", "origin")


def test_failed_commit_propagates(manager: ConfigManager, vcs: MagicMock) -> None:
    vcs.list_files.return_value = []
    vcs.read_file.side_effect = FileNotFoundError("app.json")
    vcs.commit.side_effect = GitOperationError("commit", "Please tell me who you are")

    with pytest.raises(GitOperationError, match="who you are"):
        manager.create_config("app.json", {"a": 1}, draft="d")

    vcs.write_file.assert_called_once_with("app.json", '{\n  "a": 1\n}\n')


def test_dirty_tree_stops_before_any_write(
    manager: ConfigManager, vcs: MagicMock
) -> None:
    vcs.ensure_clean.side_effect = DirtyWorkingTreeError(vcs.root, ["x"])

    with pytest.raises(DirtyWorkingTreeError):
        manager.set_value("app.json", "a", 1, draft="d")

    vcs.write_file.assert_not_called()
    vcs.commit.assert_not_called()


def test_publish_conflict_is_reported_with_key_details(
    manager: ConfigManager, vcs: MagicMock
) -> None:
    vcs.merge.side_effect = MergeConflictError("draft/d", "main", ["app.json"])
    vcs.resolve_commit.return_value = "head"
    vcs.merge_base.return_value = "base"
    versions = {"base": '{"a": 1}', "head": '{"a": 2}', "draft/d": '{"a": 3}'}
    vcs.read_file.side_effect = lambda _path, ref: versions[ref]

    with pytest.raises(MergeConflictError) as exc_info:
        manager.publish("d")

    [conflict] = exc_info.value.details
    assert [(k.key, k.base, k.target, k.source) for k in conflict.keys] == [
        ("a", 1, 2, 3)
    ]
    vcs.reset_hard.assert_called_once_with("head")  # squash-safe abort
    vcs.delete_branch.assert_not_called()


def test_preference_is_not_delegated_to_git_hunks(
    manager: ConfigManager, vcs: MagicMock
) -> None:
    vcs.merge.return_value = MergeResult(commit="abc", files=[])

    manager.publish("d", prefer="draft")

    assert vcs.merge.call_args.kwargs.get("prefer") is None
