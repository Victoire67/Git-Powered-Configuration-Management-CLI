"""Tests for ConfigManager against real temporary Git repositories.

Each test drives the manager and then inspects the resulting Git state
(branches, commits, parents, file content) directly with GitPython.
"""

import json

import git
import pytest
from structlog.testing import capture_logs

from gitconfig_cli.config_parser import ConfigParser
from gitconfig_cli.exceptions import (
    ConfigAlreadyExists,
    ConfigFormatError,
    ConfigNotFound,
    DirtyWorkingTreeError,
    DraftAlreadyExists,
    DraftNotFound,
    GitOperationError,
    InvalidNameError,
    KeyNotFound,
    MergeConflictError,
    MergeInProgressError,
    RevisionNotFound,
)
from gitconfig_cli.git_repository import GitRepository
from gitconfig_cli.gitignore import DEFAULT_PATTERNS
from gitconfig_cli.manager import ConfigManager


def _git(repo: GitRepository) -> git.Repo:
    return git.Repo(repo.root)


def _json(repo: GitRepository, name: str, ref: str = "main") -> object:
    return json.loads(repo.read_file(name, ref))


# -- setup -----------------------------------------------------------------------


def test_initialize_creates_first_commit_with_gitignore(
    config_repo: GitRepository,
) -> None:
    raw = _git(config_repo)

    assert raw.active_branch.name == "main"
    assert [c.summary for c in raw.iter_commits()] == [
        "Initialize configuration repository"
    ]
    assert config_repo.list_files("main") == [".gitignore", "README.md"]
    assert config_repo.ignored_patterns() == list(DEFAULT_PATTERNS)


def test_main_branch_property(manager: ConfigManager) -> None:
    assert manager.main_branch == "main"


def test_update_ignored_commits_on_main(manager: ConfigManager) -> None:
    sha = manager.update_ignored(add=["*.secret"], remove=[".env"])

    assert sha is not None
    patterns = manager.ignored_patterns()
    assert "*.secret" in patterns
    assert ".env" not in patterns


def test_update_ignored_without_change(manager: ConfigManager) -> None:
    assert manager.update_ignored(add=["*.tmp"]) is None


def test_ignored_patterns_without_gitignore(repo: GitRepository) -> None:
    assert ConfigManager(repo, ConfigParser()).ignored_patterns() == []


def test_status_reports_branch_and_drafts(manager: ConfigManager) -> None:
    manager.create_draft("new-port")

    status = manager.status()

    assert status.branch == "main"
    assert status.dirty_files == []
    assert not status.merging
    assert [d.name for d in status.drafts] == ["new-port"]


# -- names -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["new-port", "team/feature.v2", "a_b", "x1"])
def test_valid_draft_names(name: str) -> None:
    ConfigManager.validate_draft_name(name)


@pytest.mark.parametrize(
    "name", ["", "has space", "-leading", "a..b", "trailing/", "x.lock", "a//b", "~x"]
)
def test_invalid_draft_names(name: str) -> None:
    with pytest.raises(InvalidNameError):
        ConfigManager.validate_draft_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/abs.json",
        "..\\x.json",
        "../up.json",
        "a/../b.json",
        ".git/x.json",
        "C:x.json",
    ],
)
def test_invalid_config_names(manager: ConfigManager, name: str) -> None:
    with pytest.raises(InvalidNameError):
        manager.validate_config_name(name)


def test_unsupported_config_extension(manager: ConfigManager) -> None:
    with pytest.raises(ConfigFormatError, match="unsupported file type"):
        manager.validate_config_name("notes.txt")


# -- drafts ------------------------------------------------------------------------


def test_create_draft_branches_from_main(manager: ConfigManager) -> None:
    branch = manager.create_draft("new-port")

    raw = _git(manager._repo)  # type: ignore[arg-type]
    assert branch == "draft/new-port"
    assert raw.heads["draft/new-port"].commit == raw.heads["main"].commit
    assert raw.active_branch.name == "main"


def test_create_draft_twice_fails(manager: ConfigManager) -> None:
    manager.create_draft("new-port")

    with pytest.raises(DraftAlreadyExists):
        manager.create_draft("new-port")


def test_create_draft_from_other_base(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {"a": 1}, draft="first")

    manager.create_draft("second", base="draft/first")

    assert "app.json" in config_repo.list_files("draft/second")


def test_list_drafts_reports_ahead_and_behind(manager: ConfigManager) -> None:
    manager.create_config("app.json", {"a": 1}, draft="one")
    manager.create_draft("two")
    manager.publish("one")

    drafts = {d.name: d for d in manager.list_drafts()}

    assert set(drafts) == {"two"}
    assert drafts["two"].branch == "draft/two"
    # main gained the draft's commit plus the merge commit
    assert (drafts["two"].ahead, drafts["two"].behind) == (0, 2)


def test_delete_draft(manager: ConfigManager, config_repo: GitRepository) -> None:
    manager.create_draft("tmp")

    manager.delete_draft("tmp")

    assert not config_repo.branch_exists("draft/tmp")


def test_delete_unpublished_draft_needs_force(manager: ConfigManager) -> None:
    manager.create_config("app.json", {}, draft="tmp")

    with pytest.raises(GitOperationError, match="not fully merged"):
        manager.delete_draft("tmp")

    manager.delete_draft("tmp", force=True)


def test_delete_checked_out_draft_switches_to_main(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_draft("tmp")
    config_repo.checkout("draft/tmp")

    manager.delete_draft("tmp")

    assert config_repo.current_branch() == "main"


def test_delete_missing_draft(manager: ConfigManager) -> None:
    with pytest.raises(DraftNotFound):
        manager.delete_draft("ghost")


def test_diff_draft(manager: ConfigManager) -> None:
    manager.create_config("app.json", {"port": 8080}, draft="d")

    diff = manager.diff_draft("d")

    assert "+++ b/app.json" in diff
    assert '+  "port": 8080' in diff


# -- creating and reading -------------------------------------------------------


def test_create_config_commits_on_new_draft(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    sha = manager.create_config("app.json", {"port": 8080}, draft="setup")

    raw = _git(config_repo)
    assert raw.heads["draft/setup"].commit.hexsha == sha
    assert raw.heads["draft/setup"].commit.summary == "Create app.json"
    assert _json(config_repo, "app.json", "draft/setup") == {"port": 8080}
    assert "app.json" not in config_repo.list_files("main")
    assert raw.active_branch.name == "main"


def test_create_config_in_yaml_with_message(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config(
        "services/api.yaml", {"replicas": 2}, draft="api", message="Add API config"
    )

    assert config_repo.read_file("services/api.yaml", "draft/api") == "replicas: 2\n"
    assert config_repo.history("draft/api", limit=1)[0].message == "Add API config"


def test_create_config_requires_existing_draft_when_asked(
    manager: ConfigManager,
) -> None:
    with pytest.raises(DraftNotFound):
        manager.create_config("app.json", {}, draft="ghost", create_draft=False)


def test_create_existing_config_fails(manager: ConfigManager) -> None:
    manager.create_config("app.json", {}, draft="d")

    with pytest.raises(ConfigAlreadyExists):
        manager.create_config("app.json", {}, draft="d")


def test_create_config_refuses_dirty_tree(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    config_repo.write_file("README.md", "local edit\n")

    with pytest.raises(DirtyWorkingTreeError):
        manager.create_config("app.json", {}, draft="d")

    assert config_repo.read_working_file("README.md") == "local edit\n"


def test_list_and_read_configs(manager: ConfigManager) -> None:
    manager.create_config("app.json", {"server": {"port": 1}}, draft="d")
    manager.create_config("db.yaml", {"host": "x"}, draft="d")

    assert manager.list_configs(draft="d") == ["app.json", "db.yaml"]
    assert manager.list_configs() == []
    assert manager.read_config("app.json", draft="d") == {"server": {"port": 1}}
    assert manager.read_text("db.yaml", draft="d") == "host: x\n"
    assert manager.get_value("app.json", "server.port", draft="d") == 1


def test_read_config_at_ref(manager: ConfigManager) -> None:
    sha = manager.create_config("app.json", {"v": 1}, draft="d")
    manager.set_value("app.json", "v", 2, draft="d")

    assert manager.read_config("app.json", ref=sha) == {"v": 1}


def test_read_missing_config(manager: ConfigManager) -> None:
    with pytest.raises(ConfigNotFound, match="on branch 'main'"):
        manager.read_config("app.json")


def test_get_missing_key(manager: ConfigManager) -> None:
    manager.create_config("app.json", {"a": 1}, draft="d")

    with pytest.raises(KeyNotFound):
        manager.get_value("app.json", "b", draft="d")


def test_read_from_missing_draft(manager: ConfigManager) -> None:
    with pytest.raises(DraftNotFound):
        manager.read_config("app.json", draft="ghost")


# -- updating ------------------------------------------------------------------------


def test_set_value_commits_each_change(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {"server": {"port": 8080}}, draft="d")

    sha = manager.set_value("app.json", "server.port", 9090, draft="d")
    manager.set_value("app.json", "server.debug", True, draft="d")

    assert sha is not None
    assert _json(config_repo, "app.json", "draft/d") == {
        "server": {"port": 9090, "debug": True}
    }
    messages = [c.message for c in config_repo.history("draft/d", path="app.json")]
    assert messages == [
        "Set server.debug in app.json",
        "Set server.port in app.json",
        "Create app.json",
    ]


def test_set_same_value_does_not_commit(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {"port": 1}, draft="d")
    before = config_repo.resolve_commit("draft/d")

    with capture_logs() as logs:
        assert manager.set_value("app.json", "port", 1, draft="d") is None

    assert config_repo.resolve_commit("draft/d") == before
    assert any(entry["event"] == "unchanged" for entry in logs)


def test_set_value_through_scalar_fails(manager: ConfigManager) -> None:
    manager.create_config("app.json", {"port": 1}, draft="d")

    with pytest.raises(KeyNotFound):
        manager.set_value("app.json", "port.inner", 2, draft="d")


def test_set_value_on_missing_config(manager: ConfigManager) -> None:
    manager.create_draft("d")

    with pytest.raises(ConfigNotFound):
        manager.set_value("app.json", "a", 1, draft="d")


def test_set_value_requires_existing_draft(manager: ConfigManager) -> None:
    with pytest.raises(DraftNotFound):
        manager.set_value("app.json", "a", 1, draft="ghost")


def test_unset_value(manager: ConfigManager, config_repo: GitRepository) -> None:
    manager.create_config("app.json", {"a": 1, "b": 2}, draft="d")

    manager.unset_value("app.json", "a", draft="d")

    assert _json(config_repo, "app.json", "draft/d") == {"b": 2}


def test_unset_missing_key(manager: ConfigManager) -> None:
    manager.create_config("app.json", {"a": 1}, draft="d")

    with pytest.raises(KeyNotFound):
        manager.unset_value("app.json", "zzz", draft="d")


def test_replace_config(manager: ConfigManager, config_repo: GitRepository) -> None:
    manager.create_config("app.json", {"a": 1}, draft="d")

    manager.replace_config("app.json", {"b": 2}, draft="d", message="Rewrite")

    assert _json(config_repo, "app.json", "draft/d") == {"b": 2}
    assert config_repo.history("draft/d", limit=1)[0].message == "Rewrite"


def test_delete_config(manager: ConfigManager, config_repo: GitRepository) -> None:
    manager.create_config("app.json", {}, draft="d")

    manager.delete_config("app.json", draft="d")

    assert "app.json" not in config_repo.list_files("draft/d")


def test_delete_missing_config(manager: ConfigManager) -> None:
    manager.create_draft("d")

    with pytest.raises(ConfigNotFound):
        manager.delete_config("app.json", draft="d")


def test_writes_are_audited(manager: ConfigManager) -> None:
    with capture_logs() as logs:
        manager.create_config("app.json", {}, draft="d")

    actions = [(e.get("action"), e["event"]) for e in logs if "action" in e]
    assert ("config.create", "started") in actions
    assert ("draft.create", "succeeded") in actions
    assert ("config.create", "committed") in actions
    assert ("config.create", "succeeded") in actions


# -- history and rollback ------------------------------------------------------------


@pytest.fixture
def published(manager: ConfigManager) -> ConfigManager:
    """main has three published versions of app.json: port 1, 2 and 3."""
    manager.create_config("app.json", {"port": 1}, draft="v1")
    manager.publish("v1")
    for port in (2, 3):
        manager.create_draft(f"v{port}")
        manager.set_value("app.json", "port", port, draft=f"v{port}")
        manager.publish(f"v{port}")
    return manager


def test_history_lists_published_versions_newest_first(
    published: ConfigManager,
) -> None:
    history = published.history("app.json")

    assert [c.message for c in history] == [
        "Publish draft 'v3'",
        "Publish draft 'v2'",
        "Publish draft 'v1'",
    ]
    assert len(published.history("app.json", limit=2)) == 2


def test_history_including_draft_commits(published: ConfigManager) -> None:
    messages = [c.message for c in published.history("app.json", include_drafts=True)]

    assert "Create app.json" in messages
    assert "Set port in app.json" in messages


def test_history_of_unknown_config(manager: ConfigManager) -> None:
    with pytest.raises(ConfigNotFound):
        manager.history("app.json")


def test_rollback_by_steps_adds_a_commit(
    published: ConfigManager, config_repo: GitRepository
) -> None:
    commits_before = len(list(_git(config_repo).iter_commits("main")))

    sha = published.rollback("app.json", steps=1)

    assert sha is not None
    assert _json(config_repo, "app.json") == {"port": 2}
    assert len(list(_git(config_repo).iter_commits("main"))) == commits_before + 1
    assert config_repo.history("main", limit=1)[0].message.startswith(
        "Rollback app.json to "
    )


def test_rollback_to_commit(
    published: ConfigManager, config_repo: GitRepository
) -> None:
    first = published.history("app.json")[-1].sha

    published.rollback("app.json", to=first[:7], message="Back to v1")

    assert _json(config_repo, "app.json") == {"port": 1}
    assert config_repo.history("main", limit=1)[0].message == "Back to v1"


def test_rollback_on_draft_leaves_main_untouched(
    published: ConfigManager, config_repo: GitRepository
) -> None:
    published.create_draft("revert")

    published.rollback("app.json", steps=2, draft="revert")

    assert _json(config_repo, "app.json", "draft/revert") == {"port": 1}
    assert _json(config_repo, "app.json") == {"port": 3}


def test_rollback_to_current_content_is_a_no_op(published: ConfigManager) -> None:
    assert published.rollback("app.json", to="main") is None


@pytest.mark.parametrize("steps", [0, 99])
def test_rollback_out_of_range(published: ConfigManager, steps: int) -> None:
    with pytest.raises(RevisionNotFound):
        published.rollback("app.json", steps=steps)


def test_rollback_to_unknown_commit(published: ConfigManager) -> None:
    with pytest.raises(RevisionNotFound, match="'nope'"):
        published.rollback("app.json", to="nope")


def test_rollback_to_commit_without_file(
    published: ConfigManager, config_repo: GitRepository
) -> None:
    root = _git(config_repo).git.rev_list("--max-parents=0", "main")

    with pytest.raises(RevisionNotFound):
        published.rollback("app.json", to=root)


@pytest.mark.parametrize("kwargs", [{}, {"to": "main", "steps": 1}])
def test_rollback_needs_exactly_one_target(
    published: ConfigManager, kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        published.rollback("app.json", **kwargs)  # type: ignore[arg-type]


# -- publishing ----------------------------------------------------------------------


def test_publish_merges_draft_with_merge_commit(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {"port": 8080}, draft="setup")

    result = manager.publish("setup")

    raw = _git(config_repo)
    head = raw.heads["main"].commit
    assert result.commit == head.hexsha
    assert result.files == ["app.json"]
    assert result.draft_deleted
    assert len(head.parents) == 2
    assert head.summary == "Publish draft 'setup'"
    assert _json(config_repo, "app.json") == {"port": 8080}
    assert not config_repo.branch_exists("draft/setup")


def test_publish_squash(manager: ConfigManager, config_repo: GitRepository) -> None:
    manager.create_config("app.json", {"port": 1}, draft="d")
    manager.set_value("app.json", "port", 2, draft="d")

    result = manager.publish("d", squash=True, message="Ship it")

    head = _git(config_repo).heads["main"].commit
    assert len(head.parents) == 1
    assert head.summary == "Ship it"
    assert result.draft_deleted
    assert _json(config_repo, "app.json") == {"port": 2}


def test_publish_keep_draft(manager: ConfigManager, config_repo: GitRepository) -> None:
    manager.create_config("app.json", {}, draft="d")

    result = manager.publish("d", keep_draft=True)

    assert not result.draft_deleted
    assert config_repo.branch_exists("draft/d")


def test_publish_nothing_new(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_draft("empty")

    result = manager.publish("empty")

    assert result.up_to_date
    assert not result.draft_deleted
    assert config_repo.branch_exists("draft/empty")


def test_publish_while_on_the_draft(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {}, draft="d")
    config_repo.checkout("draft/d")

    manager.publish("d")

    assert config_repo.current_branch() == "main"
    assert not config_repo.branch_exists("draft/d")


def test_publish_missing_draft(manager: ConfigManager) -> None:
    with pytest.raises(DraftNotFound):
        manager.publish("ghost")


def test_publish_conflict_leaves_main_unchanged(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {"port": 8080}, draft="base")
    manager.publish("base")
    manager.create_draft("a")
    manager.create_draft("b")
    manager.set_value("app.json", "port", 9000, draft="a")
    manager.set_value("app.json", "port", 7000, draft="b")
    manager.publish("a")
    before = config_repo.resolve_commit("main")

    with pytest.raises(MergeConflictError) as exc_info:
        manager.publish("b")

    assert exc_info.value.conflicted_files == ["app.json"]
    assert config_repo.resolve_commit("main") == before
    assert config_repo.branch_exists("draft/b")
    assert config_repo.dirty_files() == []
    assert not config_repo.is_merging()


@pytest.mark.parametrize(("prefer", "port"), [("draft", 7000), ("main", 9000)])
def test_publish_with_preference(
    manager: ConfigManager, config_repo: GitRepository, prefer: str, port: int
) -> None:
    manager.create_config("app.json", {"port": 8080}, draft="base")
    manager.publish("base")
    manager.create_draft("a")
    manager.create_draft("b")
    manager.set_value("app.json", "port", 9000, draft="a")
    manager.set_value("app.json", "port", 7000, draft="b")
    manager.publish("a")

    manager.publish("b", prefer=prefer)  # type: ignore[arg-type]

    assert _json(config_repo, "app.json") == {"port": port}


def test_publish_rejects_invalid_result_and_resets_main(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_draft("d")
    with config_repo.checked_out("draft/d"):
        config_repo.write_file("broken.json", "{not json")
        config_repo.commit("bad file", ["broken.json"])
    before = config_repo.resolve_commit("main")

    with pytest.raises(ConfigFormatError, match="publishing would produce invalid"):
        manager.publish("d")

    assert config_repo.resolve_commit("main") == before
    assert config_repo.branch_exists("draft/d")


def test_publish_ignores_non_config_files_in_validation(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_draft("d")
    with config_repo.checked_out("draft/d"):
        config_repo.write_file("NOTES.txt", "{not json")
        config_repo.commit("notes", ["NOTES.txt"])

    result = manager.publish("d")

    assert result.files == []
    assert "NOTES.txt" in config_repo.list_files("main")


def test_publish_deleting_a_config(
    published: ConfigManager, config_repo: GitRepository
) -> None:
    published.create_draft("cleanup")
    published.delete_config("app.json", draft="cleanup")

    result = published.publish("cleanup")

    assert result.files == ["app.json"]
    assert "app.json" not in config_repo.list_files("main")


def test_actions_blocked_during_merge(
    manager: ConfigManager, config_repo: GitRepository
) -> None:
    manager.create_config("app.json", {"port": 8080}, draft="base")
    manager.publish("base")
    manager.create_draft("a")
    manager.set_value("app.json", "port", 9000, draft="a")
    with config_repo.checked_out("main"):
        config_repo.write_file("app.json", '{\n  "port": 7000\n}\n')
        config_repo.commit("hotfix", ["app.json"])
    config_repo.checkout("draft/a")
    with pytest.raises(MergeConflictError):
        config_repo.merge("main", "sync", abort_on_conflict=False)

    with pytest.raises(MergeInProgressError) as exc_info:
        manager.create_config("x.json", {}, draft="other")

    assert exc_info.value.branch == "draft/a"
    assert exc_info.value.conflicted_files == ["app.json"]


# -- remotes ---------------------------------------------------------------------------


@pytest.fixture
def with_remote(manager: ConfigManager, config_repo: GitRepository, tmp_path):  # type: ignore[no-untyped-def]
    bare = tmp_path / "origin.git"
    git.Repo.init(bare, bare=True, initial_branch="main").close()
    _git(config_repo).create_remote("origin", str(bare))
    return bare


def test_push_main_and_draft(manager: ConfigManager, with_remote) -> None:  # type: ignore[no-untyped-def]
    manager.create_config("app.json", {}, draft="d")

    assert manager.push() == "main"
    assert manager.push(draft="d") == "draft/d"

    remote_heads = {h.name for h in git.Repo(with_remote).heads}
    assert remote_heads == {"main", "draft/d"}


def test_pull_fast_forwards_main(
    manager: ConfigManager, with_remote, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    manager.push()
    other = GitRepository.clone(str(with_remote), tmp_path / "teammate")
    teammate = ConfigManager(other, ConfigParser())
    teammate.create_config("app.json", {"port": 1}, draft="d")
    teammate.publish("d")
    teammate.push()
    other.close()

    manager.pull()

    assert manager.read_config("app.json") == {"port": 1}
