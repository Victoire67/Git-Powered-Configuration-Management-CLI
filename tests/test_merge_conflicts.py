"""Merge-conflict scenarios, created on purpose in temporary repositories.

Two teammates work on drafts started from the same version of main. The
tests check that the tool:
* auto-resolves conflicts Git reports only because edits touch
  neighbouring lines,
* reports genuine conflicts key by key and leaves main untouched,
* offers a sync -> fix -> resolve workflow and a dry-run preview.
"""

import json

import git
import pytest

from gitconfig_cli.exceptions import (
    ConfigFormatError,
    MergeConflictError,
    MergeInProgressError,
    NoMergeInProgress,
)
from gitconfig_cli.git_repository import GitRepository
from gitconfig_cli.manager import ConfigManager
from gitconfig_cli.models import ABSENT, ROOT_KEY, KeyConflict

BASE = {"server": {"host": "localhost", "port": 8080}, "debug": False}


@pytest.fixture
def team(manager: ConfigManager) -> ConfigManager:
    """main has app.json; drafts 'alice' and 'bob' both start from it."""
    manager.create_config("app.json", BASE, draft="base")
    manager.publish("base")
    manager.create_draft("alice")
    manager.create_draft("bob")
    return manager


def _main_json(repo: GitRepository) -> object:
    return json.loads(repo.read_file("app.json", "main"))


# -- automatic, key-level resolution --------------------------------------------


def test_neighbouring_line_edits_are_auto_resolved(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "server.host", "alice.example", draft="alice")
    team.set_value("app.json", "server.port", 9000, draft="bob")
    team.publish("alice")

    # Git alone would conflict: "host" and "port" are adjacent lines.
    with config_repo.checked_out("main"), pytest.raises(MergeConflictError):
        config_repo.merge("draft/bob", "plain git merge")

    result = team.publish("bob")

    assert result.auto_resolved == ["app.json"]
    assert _main_json(config_repo) == {
        "server": {"host": "alice.example", "port": 9000},
        "debug": False,
    }
    head = git.Repo(config_repo.root).heads["main"].commit
    assert len(head.parents) == 2
    assert not config_repo.branch_exists("draft/bob")


def test_auto_resolution_with_squash(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "server.host", "a", draft="alice")
    team.set_value("app.json", "server.port", 1, draft="bob")
    team.publish("alice")

    result = team.publish("bob", squash=True)

    assert result.auto_resolved == ["app.json"]
    assert _main_json(config_repo)["server"] == {"host": "a", "port": 1}  # type: ignore[index]


# -- genuine conflicts ---------------------------------------------------------------


def test_same_key_conflict_is_reported_key_by_key(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "server.port", 9000, draft="alice")
    team.set_value("app.json", "server.port", 7000, draft="bob")
    team.set_value("app.json", "debug", True, draft="bob")
    team.publish("alice")
    before = config_repo.resolve_commit("main")

    with pytest.raises(MergeConflictError) as exc_info:
        team.publish("bob")

    error = exc_info.value
    assert error.source == "draft/bob"
    assert error.target == "main"
    assert error.conflicted_files == ["app.json"]
    assert not error.in_progress
    [file_conflict] = error.details
    assert file_conflict.path == "app.json"
    assert file_conflict.keys == [KeyConflict("server.port", 8080, 9000, 7000)]
    # main is untouched and the working tree is clean
    assert config_repo.resolve_commit("main") == before
    assert config_repo.dirty_files() == []
    assert config_repo.branch_exists("draft/bob")


def test_conflict_with_unparseable_side_is_unresolved(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    with config_repo.checked_out("draft/alice"):
        config_repo.write_file("app.json", "{broken\n")
        config_repo.commit("break it", ["app.json"])
    team.set_value("app.json", "debug", True, draft="bob")
    with config_repo.checked_out("main"):
        config_repo.merge("draft/alice", "force broken into main")

    with pytest.raises(MergeConflictError) as exc_info:
        team.publish("bob")

    assert exc_info.value.details[0].keys == []


def test_delete_versus_modify_is_a_whole_file_conflict(
    team: ConfigManager,
) -> None:
    team.delete_config("app.json", draft="alice")
    team.set_value("app.json", "debug", True, draft="bob")
    team.publish("alice")

    with pytest.raises(MergeConflictError) as exc_info:
        team.publish("bob")

    [key] = exc_info.value.details[0].keys
    assert key.key == ROOT_KEY
    assert key.target is ABSENT


def test_conflict_in_non_config_file_needs_manual_resolution(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    for draft, text in (("alice", "alice\n"), ("bob", "bob\n")):
        with config_repo.checked_out(f"draft/{draft}"):
            config_repo.write_file("README.md", text)
            config_repo.commit(f"{draft} readme", ["README.md"])
    team.publish("alice")

    with pytest.raises(MergeConflictError) as exc_info:
        team.publish("bob")

    assert exc_info.value.conflicted_files == ["README.md"]
    assert exc_info.value.details[0].keys == []


def test_squash_conflict_leaves_main_clean(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "server.port", 1, draft="alice")
    team.set_value("app.json", "server.port", 2, draft="bob")
    team.publish("alice")
    before = config_repo.resolve_commit("main")

    with pytest.raises(MergeConflictError):
        team.publish("bob", squash=True)

    assert config_repo.resolve_commit("main") == before
    assert config_repo.dirty_files() == []


@pytest.mark.parametrize(("prefer", "port"), [("draft", 7000), ("main", 9000)])
def test_preference_resolves_genuine_conflicts(
    team: ConfigManager, config_repo: GitRepository, prefer: str, port: int
) -> None:
    team.set_value("app.json", "server.port", 9000, draft="alice")
    team.set_value("app.json", "server.port", 7000, draft="bob")
    team.publish("alice")

    team.publish("bob", prefer=prefer)  # type: ignore[arg-type]

    assert _main_json(config_repo)["server"]["port"] == port  # type: ignore[index]


def test_preference_resolves_non_config_conflicts(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.delete_config("app.json", draft="alice")
    team.set_value("app.json", "debug", True, draft="bob")
    team.publish("alice")

    team.publish("bob", prefer="main")

    assert "app.json" not in config_repo.list_files("main")


# -- dry-run preview -------------------------------------------------------------


def test_preview_reports_conflicts_without_touching_repo(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "server.port", 9000, draft="alice")
    team.set_value("app.json", "server.port", 7000, draft="bob")
    team.publish("alice")
    before = {b: config_repo.resolve_commit(b) for b in config_repo.list_branches()}

    preview = team.preview_publish("bob")

    assert not preview.can_publish
    assert preview.conflicts[0].keys[0].key == "server.port"
    after = {b: config_repo.resolve_commit(b) for b in config_repo.list_branches()}
    assert after == before
    assert config_repo.dirty_files() == []


def test_preview_of_clean_publish(team: ConfigManager) -> None:
    team.set_value("app.json", "server.host", "a", draft="alice")
    team.set_value("app.json", "server.port", 1, draft="bob")
    team.publish("alice")

    preview = team.preview_publish("bob")

    assert preview.can_publish
    assert preview.files == ["app.json"]
    assert preview.auto_resolved == ["app.json"]


def test_preview_works_while_on_a_draft(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "debug", True, draft="alice")
    config_repo.checkout("draft/alice")

    preview = team.preview_publish("alice")

    assert preview.files == ["app.json"]
    assert config_repo.current_branch() == "draft/alice"


def test_preview_of_draft_without_changes(team: ConfigManager) -> None:
    assert team.preview_publish("alice").up_to_date


def test_preview_reports_invalid_result(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    with config_repo.checked_out("draft/alice"):
        config_repo.write_file("broken.yaml", "a: [\n")
        config_repo.commit("broken", ["broken.yaml"])

    preview = team.preview_publish("alice")

    assert not preview.can_publish
    assert "broken.yaml" in preview.problems[0]


# -- sync / resolve / abort ----------------------------------------------------------


@pytest.fixture
def conflicted(team: ConfigManager) -> ConfigManager:
    """alice published port 9000; bob changed port to 7000 and debug to True."""
    team.set_value("app.json", "server.port", 9000, draft="alice")
    team.set_value("app.json", "server.host", "alice.example", draft="alice")
    team.publish("alice")
    team.set_value("app.json", "server.port", 7000, draft="bob")
    return team


def test_sync_without_conflicts_returns_to_original_branch(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    team.set_value("app.json", "debug", True, draft="alice")
    team.publish("alice")

    result = team.sync_draft("bob")

    assert not result.up_to_date
    assert config_repo.current_branch() == "main"
    assert json.loads(config_repo.read_file("app.json", "draft/bob"))["debug"] is True


def test_sync_when_up_to_date(team: ConfigManager) -> None:
    assert team.sync_draft("bob").up_to_date


def test_sync_with_conflict_leaves_merge_for_the_user(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    with pytest.raises(MergeConflictError) as exc_info:
        conflicted.sync_draft("bob")

    assert exc_info.value.in_progress
    assert exc_info.value.source == "main"
    assert exc_info.value.target == "draft/bob"
    assert config_repo.current_branch() == "draft/bob"
    assert config_repo.is_merging()
    assert "<<<<<<<" in config_repo.read_working_file("app.json")
    status = conflicted.status()
    assert status.merging
    assert status.conflicts == ["app.json"]


def test_sync_stages_files_it_could_resolve(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    with config_repo.checked_out("main"):
        config_repo.write_file("db.json", '{\n  "a": 1,\n  "b": 1\n}\n')
        config_repo.commit("db on main", ["db.json"])
    team.sync_draft("bob")
    team.set_value("app.json", "server.port", 9000, draft="alice")
    team.publish("alice")
    team.set_value("app.json", "server.port", 7000, draft="bob")
    team.set_value("db.json", "a", 2, draft="bob")
    with config_repo.checked_out("main"):
        config_repo.write_file("db.json", '{\n  "a": 1,\n  "b": 2\n}\n')
        config_repo.commit("b on main", ["db.json"])

    with pytest.raises(MergeConflictError) as exc_info:
        team.sync_draft("bob")

    # db.json conflicted for Git (adjacent lines) but was resolved and staged
    assert exc_info.value.conflicted_files == ["app.json"]
    assert config_repo.conflicted_files() == ["app.json"]
    assert json.loads(config_repo.read_working_file("db.json")) == {"a": 2, "b": 2}


def test_actions_are_blocked_until_conflict_is_resolved(
    conflicted: ConfigManager,
) -> None:
    with pytest.raises(MergeConflictError):
        conflicted.sync_draft("bob")

    with pytest.raises(MergeInProgressError):
        conflicted.publish("bob")
    with pytest.raises(MergeInProgressError):
        conflicted.preview_publish("bob")


def test_resolve_after_manual_edit_then_publish(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    with pytest.raises(MergeConflictError):
        conflicted.sync_draft("bob")
    resolved = {"server": {"host": "alice.example", "port": 7000}, "debug": False}
    config_repo.write_file("app.json", json.dumps(resolved, indent=2) + "\n")

    sha = conflicted.resolve_conflicts()

    head = git.Repo(config_repo.root).heads["draft/bob"].commit
    assert head.hexsha == sha
    assert len(head.parents) == 2
    assert head.summary == "Merge main into draft/bob (conflicts resolved)"
    assert not config_repo.is_merging()

    result = conflicted.publish("bob")

    assert result.auto_resolved == []
    assert _main_json(config_repo) == resolved


def test_resolve_refuses_remaining_markers(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    with pytest.raises(MergeConflictError):
        conflicted.sync_draft("bob")

    with pytest.raises(ConfigFormatError, match="conflict markers"):
        conflicted.resolve_conflicts()

    assert config_repo.is_merging()


def test_resolve_refuses_invalid_config(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    with pytest.raises(MergeConflictError):
        conflicted.sync_draft("bob")
    config_repo.write_file("app.json", '{"port": }')

    with pytest.raises(ConfigFormatError, match="malformed json"):
        conflicted.resolve_conflicts()


def test_resolve_by_deleting_the_file(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    with pytest.raises(MergeConflictError):
        conflicted.sync_draft("bob")
    (config_repo.root / "app.json").unlink()

    conflicted.resolve_conflicts(message="Drop app.json")

    assert "app.json" not in config_repo.list_files("draft/bob")


def test_abort_restores_the_draft(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    before = config_repo.resolve_commit("draft/bob")
    with pytest.raises(MergeConflictError):
        conflicted.sync_draft("bob")

    assert conflicted.abort_merge() == "draft/bob"

    assert not config_repo.is_merging()
    assert config_repo.dirty_files() == []
    assert config_repo.resolve_commit("draft/bob") == before


def test_resolve_or_abort_without_merge(conflicted: ConfigManager) -> None:
    with pytest.raises(NoMergeInProgress):
        conflicted.resolve_conflicts()
    with pytest.raises(NoMergeInProgress):
        conflicted.abort_merge()


def test_sync_with_preference_keeps_the_other_sides_neighbouring_change(
    conflicted: ConfigManager, config_repo: GitRepository
) -> None:
    """Only the truly conflicting key (port) follows the preference."""
    conflicted.sync_draft("bob", prefer="draft")

    data = json.loads(config_repo.read_file("app.json", "draft/bob"))
    assert data["server"] == {"host": "alice.example", "port": 7000}
    assert config_repo.current_branch() == "main"


def test_sync_restores_branch_when_merge_result_is_invalid(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    with config_repo.checked_out("main"):
        config_repo.write_file("broken.json", "{oops")
        config_repo.commit("broken on main", ["broken.json"])
    before = config_repo.resolve_commit("draft/bob")

    with pytest.raises(ConfigFormatError):
        team.sync_draft("bob")

    assert config_repo.current_branch() == "main"
    assert config_repo.resolve_commit("draft/bob") == before


def test_manual_resolution_of_non_config_file(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    with config_repo.checked_out("draft/alice"):
        config_repo.write_file("README.md", "alice\n")
        config_repo.commit("alice readme", ["README.md"])
    team.publish("alice")
    with config_repo.checked_out("draft/bob"):
        config_repo.write_file("README.md", "bob\n")
        config_repo.commit("bob readme", ["README.md"])

    with pytest.raises(MergeConflictError):
        team.sync_draft("bob")
    config_repo.write_file("README.md", "alice and bob\n")
    team.resolve_conflicts()

    assert config_repo.read_file("README.md", "draft/bob") == "alice and bob\n"


def test_non_config_conflict_with_preference(
    team: ConfigManager, config_repo: GitRepository
) -> None:
    for draft in ("alice", "bob"):
        with config_repo.checked_out(f"draft/{draft}"):
            config_repo.write_file("README.md", f"{draft}\n")
            config_repo.commit(f"{draft} readme", ["README.md"])
    team.publish("alice")

    team.publish("bob", prefer="draft")

    assert config_repo.read_file("README.md", "main") == "bob\n"
