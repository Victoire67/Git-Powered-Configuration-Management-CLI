from pathlib import Path

import git
import pytest
from git.exc import GitCommandError
from pytest_mock import MockerFixture

from gitconfig_cli.abstractions import VersionControl
from gitconfig_cli.exceptions import (
    DirtyWorkingTreeError,
    GitOperationError,
    InvalidRepoError,
    MergeConflictError,
)
from gitconfig_cli.git_repository import GitRepository, _error_details
from tests.conftest import TEST_NAME, CommitFile

# -- construction --------------------------------------------------------------


def test_git_repository_implements_the_abstraction(repo: GitRepository) -> None:
    assert isinstance(repo, VersionControl)


def test_init_creates_repository_on_main(tmp_path: Path) -> None:
    created = GitRepository.init(tmp_path / "new", main_branch="main")

    assert (tmp_path / "new" / ".git").is_dir()
    assert created.root == (tmp_path / "new").resolve()
    assert created.current_branch() == "main"
    created.close()


def test_init_uses_custom_main_branch(tmp_path: Path) -> None:
    created = GitRepository.init(tmp_path / "new", main_branch="trunk")

    assert created.current_branch() == "trunk"
    created.close()


def test_init_refuses_existing_repository(empty_repo_path: Path) -> None:
    with pytest.raises(InvalidRepoError, match="already a Git repository"):
        GitRepository.init(empty_repo_path)


def test_init_wraps_git_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(
        git.Repo, "init", side_effect=GitCommandError("init", 128, b"fatal: nope")
    )

    with pytest.raises(GitOperationError, match="Git 'init' failed: fatal: nope"):
        GitRepository.init(tmp_path / "new")


def test_init_wraps_os_error(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch.object(git.Repo, "init", side_effect=PermissionError("denied"))

    with pytest.raises(GitOperationError, match="denied"):
        GitRepository.init(tmp_path / "new")


def test_open_existing_empty_repository(empty_repo_path: Path) -> None:
    opened = GitRepository.open(empty_repo_path)

    assert opened.root == empty_repo_path.resolve()
    opened.close()


def test_open_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(InvalidRepoError, match="directory does not exist"):
        GitRepository.open(tmp_path / "missing")


def test_open_plain_directory(tmp_path: Path) -> None:
    with pytest.raises(InvalidRepoError, match="not a Git repository") as exc_info:
        GitRepository.open(tmp_path)

    assert exc_info.value.path == tmp_path


def test_open_bare_repository(tmp_path: Path) -> None:
    git.Repo.init(tmp_path / "bare.git", bare=True).close()

    with pytest.raises(InvalidRepoError, match="bare repositories"):
        GitRepository.open(tmp_path / "bare.git")


def test_clone_copies_all_branches(repo: GitRepository, tmp_path: Path) -> None:
    repo.create_branch("draft/feature", "main")

    cloned = GitRepository.clone(str(repo.root), tmp_path / "clone")

    assert cloned.list_branches() == ["draft/feature", "main"]
    assert cloned.read_file("README.md", "main") == "# configs\n"
    cloned.close()


def test_clone_failure_is_wrapped(tmp_path: Path) -> None:
    with pytest.raises(GitOperationError, match="Git 'clone' failed"):
        GitRepository.clone(str(tmp_path / "does-not-exist"), tmp_path / "clone")


# -- branches ------------------------------------------------------------------


def test_create_list_and_delete_branches(repo: GitRepository) -> None:
    repo.create_branch("draft/a", "main")
    repo.create_branch("draft/b", "main")

    assert repo.list_branches() == ["draft/a", "draft/b", "main"]
    assert repo.list_branches("draft/") == ["draft/a", "draft/b"]
    assert repo.branch_exists("draft/a")

    repo.delete_branch("draft/a")

    assert not repo.branch_exists("draft/a")


def test_create_branch_with_invalid_name(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="Git 'branch' failed"):
        repo.create_branch("bad..name", "main")


def test_delete_unmerged_branch_requires_force(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("app.json", "{}\n", branch="draft/a")

    with pytest.raises(GitOperationError, match="not fully merged"):
        repo.delete_branch("draft/a")

    repo.delete_branch("draft/a", force=True)
    assert not repo.branch_exists("draft/a")


def test_current_branch_detached_head(repo: GitRepository) -> None:
    repo.checkout(repo.resolve_commit("main"))

    with pytest.raises(InvalidRepoError, match="HEAD is detached"):
        repo.current_branch()


def test_checked_out_switches_and_restores(repo: GitRepository) -> None:
    repo.create_branch("draft/a", "main")

    with repo.checked_out("draft/a"):
        assert repo.current_branch() == "draft/a"

    assert repo.current_branch() == "main"


def test_checked_out_same_branch_is_a_no_op(repo: GitRepository) -> None:
    with repo.checked_out("main"):
        assert repo.current_branch() == "main"

    assert repo.current_branch() == "main"


def test_checked_out_discards_partial_changes_on_error(repo: GitRepository) -> None:
    repo.create_branch("draft/a", "main")

    with pytest.raises(RuntimeError), repo.checked_out("draft/a"):
        repo.write_file("README.md", "half-edited\n")
        repo.write_file("new.json", "{}\n")
        raise RuntimeError("commit failed")

    assert repo.current_branch() == "main"
    assert repo.dirty_files() == []
    assert repo.read_file("README.md", "draft/a") == "# configs\n"


def test_checked_out_refuses_dirty_tree(repo: GitRepository) -> None:
    repo.write_file("README.md", "user edit\n")

    with pytest.raises(DirtyWorkingTreeError), repo.checked_out("main"):
        pass  # pragma: no cover - never entered

    assert repo.read_working_file("README.md") == "user edit\n"


def test_ahead_behind(repo: GitRepository, commit_file: CommitFile) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("a.json", "{}\n", branch="draft/a")
    commit_file("b.json", "{}\n", branch="draft/a")
    commit_file("c.json", "{}\n", branch="main")

    assert repo.ahead_behind("main", "draft/a") == (2, 1)


# -- working tree and commits ----------------------------------------------------


def test_write_and_commit(repo: GitRepository) -> None:
    repo.write_file("services/api.json", '{"port": 1}\n')

    sha = repo.commit("Add api config", ["services/api.json"])

    assert repo.resolve_commit("main") == sha
    assert repo.read_file("services/api.json", "main") == '{"port": 1}\n'
    assert repo.history("main", limit=1)[0].message == "Add api config"


def test_write_file_uses_unix_newlines(repo: GitRepository) -> None:
    repo.write_file("a.yaml", "a: 1\nb: 2\n")

    assert (repo.root / "a.yaml").read_bytes() == b"a: 1\nb: 2\n"


def test_write_file_refuses_paths_outside_repo(repo: GitRepository) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        repo.write_file("../escape.json", "{}")


def test_commit_without_changes_fails(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="Git 'commit' failed"):
        repo.commit("Nothing changed")


def test_remove_file_stages_deletion(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    commit_file("app.json", "{}\n")

    repo.remove_file("app.json")
    repo.commit("Remove app.json")

    assert "app.json" not in repo.list_files("main")
    assert not (repo.root / "app.json").exists()


def test_dirty_files_reports_modified_untracked_and_renamed(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    commit_file("old.json", "{}\n")
    repo.write_file("README.md", "changed\n")
    repo.write_file("new.json", "{}\n")
    git.Repo(repo.root).git.mv("old.json", "renamed.json")

    assert repo.dirty_files() == ["README.md", "new.json", "renamed.json"]
    with pytest.raises(DirtyWorkingTreeError) as exc_info:
        repo.ensure_clean()
    assert exc_info.value.files == ["README.md", "new.json", "renamed.json"]


def test_dirty_files_ignores_gitignored_files(repo: GitRepository) -> None:
    repo.set_ignored_patterns(["*.tmp"])
    repo.commit("Ignore temp files", [".gitignore"])
    repo.write_file("scratch.tmp", "x")

    assert repo.dirty_files() == []
    repo.ensure_clean()


# -- reading history -------------------------------------------------------------


def test_read_file_at_older_commit(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    first = commit_file("app.json", '{"v": 1}\n')
    commit_file("app.json", '{"v": 2}\n')

    assert repo.read_file("app.json", first) == '{"v": 1}\n'
    assert repo.read_file("app.json", "main") == '{"v": 2}\n'


def test_read_file_from_other_branch_without_checkout(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("app.json", "{}\n", branch="draft/a")

    assert repo.read_file("app.json", "draft/a") == "{}\n"
    assert repo.current_branch() == "main"


def test_read_missing_file(repo: GitRepository) -> None:
    with pytest.raises(FileNotFoundError):
        repo.read_file("missing.json", "main")


def test_read_directory_is_not_a_file(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    commit_file("services/api.json", "{}\n")

    with pytest.raises(FileNotFoundError):
        repo.read_file("services", "main")


def test_read_file_unknown_ref(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="unknown revision 'nope'"):
        repo.read_file("README.md", "nope")


def test_read_working_file_missing(repo: GitRepository) -> None:
    with pytest.raises(FileNotFoundError):
        repo.read_working_file("missing.json")


def test_list_files(repo: GitRepository, commit_file: CommitFile) -> None:
    commit_file("services/api.yaml", "a: 1\n")

    assert repo.list_files("main") == ["README.md", "services/api.yaml"]


def test_resolve_commit_accepts_short_sha(repo: GitRepository) -> None:
    full = repo.resolve_commit("main")

    assert repo.resolve_commit(full[:7]) == full


def test_resolve_commit_unknown(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="unknown revision"):
        repo.resolve_commit("deadbeef")


def test_history_filters_by_path_and_limit(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    commit_file("app.json", '{"v": 1}\n', message="app v1")
    commit_file("db.json", "{}\n", message="db v1")
    commit_file("app.json", '{"v": 2}\n', message="app v2")

    app_history = repo.history("main", path="app.json")
    latest = repo.history("main", limit=2)

    assert [c.message for c in app_history] == ["app v2", "app v1"]
    assert [c.message for c in latest] == ["app v2", "db v1"]
    assert app_history[0].author == TEST_NAME
    assert app_history[0].short_sha == app_history[0].sha[:7]
    assert "T" in app_history[0].date


def test_history_unknown_ref(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError):
        repo.history("nope")


def test_diff_shows_only_draft_changes(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("app.json", '{"v": 1}\n', branch="draft/a")
    commit_file("main-only.json", "{}\n", branch="main")

    diff = repo.diff("main", "draft/a")

    assert '+{"v": 1}' in diff
    assert "main-only.json" not in diff
    assert repo.diff("main", "draft/a", ["other.json"]) == ""


def test_merge_base(repo: GitRepository, commit_file: CommitFile) -> None:
    base = repo.resolve_commit("main")
    repo.create_branch("draft/a", "main")
    commit_file("a.json", "{}\n", branch="draft/a")
    commit_file("b.json", "{}\n", branch="main")

    assert repo.merge_base("main", "draft/a") == base


def test_merge_base_unrelated_histories(repo: GitRepository) -> None:
    repo._run("checkout", "--quiet", "--orphan", "orphan")
    repo._run("rm", "-rf", "--quiet", ".")
    repo.write_file("x.json", "{}\n")
    repo.commit("Unrelated root", ["x.json"])

    assert repo.merge_base("main", "orphan") is None


# -- merging -----------------------------------------------------------------------


@pytest.fixture
def diverged(repo: GitRepository, commit_file: CommitFile) -> GitRepository:
    """main and draft/a both changed app.json in incompatible ways."""
    commit_file("app.json", '{\n  "port": 8080\n}\n')
    repo.create_branch("draft/a", "main")
    commit_file("app.json", '{\n  "port": 9000\n}\n', branch="draft/a")
    commit_file("app.json", '{\n  "port": 7000\n}\n', branch="main")
    return repo


def test_merge_creates_merge_commit(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("app.json", "{}\n", branch="draft/a")

    result = repo.merge("draft/a", "Publish draft a")

    head = git.Repo(repo.root).head.commit
    assert result.commit == head.hexsha
    assert result.files == ["app.json"]
    assert not result.up_to_date
    assert len(head.parents) == 2
    assert head.summary == "Publish draft a"


def test_merge_up_to_date(repo: GitRepository) -> None:
    repo.create_branch("draft/a", "main")
    before = repo.resolve_commit("main")

    result = repo.merge("draft/a", "Publish")

    assert result.up_to_date
    assert result.commit == before


def test_squash_merge_creates_single_parent_commit(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("a.json", "{}\n", branch="draft/a")
    commit_file("b.json", "{}\n", branch="draft/a")

    result = repo.merge("draft/a", "Publish a (squashed)", squash=True)

    head = git.Repo(repo.root).head.commit
    assert len(head.parents) == 1
    assert head.summary == "Publish a (squashed)"
    assert result.files == ["a.json", "b.json"]


def test_squash_merge_up_to_date(repo: GitRepository) -> None:
    repo.create_branch("draft/a", "main")

    assert repo.merge("draft/a", "x", squash=True).up_to_date


def test_merge_without_commit_stages_changes(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("a.json", "{}\n", branch="draft/a")
    before = repo.resolve_commit("main")

    result = repo.merge("draft/a", "dry run", commit=False)

    assert result.commit == before
    assert result.files == ["a.json"]
    assert repo.is_merging()
    repo.abort_merge()
    assert not repo.is_merging()


def test_merge_without_commit_up_to_date(repo: GitRepository) -> None:
    repo.create_branch("draft/a", "main")

    assert repo.merge("draft/a", "dry run", commit=False).up_to_date


def test_merge_conflict_is_reported_and_aborted(diverged: GitRepository) -> None:
    before = diverged.resolve_commit("main")

    with pytest.raises(MergeConflictError) as exc_info:
        diverged.merge("draft/a", "Publish")

    error = exc_info.value
    assert error.source == "draft/a"
    assert error.target == "main"
    assert error.conflicted_files == ["app.json"]
    assert not diverged.is_merging()
    assert diverged.dirty_files() == []
    assert diverged.resolve_commit("main") == before


def test_squash_conflict_is_aborted(diverged: GitRepository) -> None:
    with pytest.raises(MergeConflictError):
        diverged.merge("draft/a", "Publish", squash=True)

    assert diverged.dirty_files() == []


def test_merge_conflict_can_be_left_for_manual_resolution(
    diverged: GitRepository,
) -> None:
    with pytest.raises(MergeConflictError):
        diverged.merge("draft/a", "Publish", abort_on_conflict=False)

    assert diverged.is_merging()
    assert diverged.conflicted_files() == ["app.json"]
    content = diverged.read_working_file("app.json")
    assert "<<<<<<<" in content
    assert ">>>>>>> draft/a" in content


@pytest.mark.parametrize(("prefer", "port"), [("theirs", 9000), ("ours", 7000)])
def test_merge_with_preference_resolves_conflicts(
    diverged: GitRepository, prefer: str, port: int
) -> None:
    diverged.merge("draft/a", "Publish", prefer=prefer)  # type: ignore[arg-type]

    assert f'"port": {port}' in diverged.read_file("app.json", "main")


def test_merge_failure_without_conflicts_is_wrapped(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="Git 'merge' failed"):
        repo.merge("no-such-branch", "Publish")


def test_abort_merge_without_merge_fails(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError):
        repo.abort_merge()


def test_checked_out_aborts_merge_on_error(diverged: GitRepository) -> None:
    with pytest.raises(MergeConflictError), diverged.checked_out("main"):
        diverged.merge("draft/a", "Publish", abort_on_conflict=False)

    assert not diverged.is_merging()
    assert diverged.dirty_files() == []


def test_reset_hard(repo: GitRepository, commit_file: CommitFile) -> None:
    first = repo.resolve_commit("main")
    commit_file("a.json", "{}\n")

    repo.reset_hard(first)

    assert repo.resolve_commit("main") == first


# -- remotes ---------------------------------------------------------------------


@pytest.fixture
def remote_pair(tmp_path: Path, repo: GitRepository) -> tuple[Path, GitRepository]:
    """A bare 'origin' seeded from ``repo`` and a fresh clone of it."""
    bare = tmp_path / "origin.git"
    git.Repo.init(bare, bare=True, initial_branch="main").close()
    git.Repo(repo.root).create_remote("origin", str(bare))
    repo.push("main")
    clone = GitRepository.clone(str(bare), tmp_path / "clone")
    return bare, clone


def test_push_and_pull_between_clones(
    repo: GitRepository, remote_pair: tuple[Path, GitRepository]
) -> None:
    _, clone = remote_pair
    clone.write_file("app.json", "{}\n")
    clone.commit("Add app", ["app.json"])

    clone.push("main")
    repo.pull("main")

    assert repo.read_file("app.json", "main") == "{}\n"
    clone.close()


def test_push_without_remote(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="no remote named 'origin'"):
        repo.push("main")


def test_pull_without_remote(repo: GitRepository) -> None:
    with pytest.raises(GitOperationError, match="Git 'pull' failed"):
        repo.pull("main")


def test_failed_push_is_reported_without_network(
    repo: GitRepository, mocker: MockerFixture
) -> None:
    """Simulate a rejected push: no real Git push is performed."""
    mocker.patch.object(repo, "_require_remote")
    execute = mocker.patch.object(
        git.cmd.Git,
        "execute",
        side_effect=GitCommandError(
            ["git", "push"], 1, b"! [rejected] main -> main (fetch first)"
        ),
    )

    with pytest.raises(GitOperationError) as exc_info:
        repo.push("main")

    assert exc_info.value.operation == "push"
    assert "rejected" in exc_info.value.details
    execute.assert_called_once_with(
        ["git", "push", "--quiet", "--set-upstream", "origin", "main"]
    )


def test_error_details_falls_back_to_message() -> None:
    error = GitCommandError(["git", "x"], 1)

    assert _error_details(error)


# -- temporary clones ------------------------------------------------------------


def test_temporary_clone_is_isolated_and_removed(
    repo: GitRepository, commit_file: CommitFile
) -> None:
    repo.create_branch("draft/a", "main")
    commit_file("app.json", "{}\n", branch="draft/a")

    with repo.temporary_clone() as clone:
        clone_root = clone.root
        assert clone.list_branches() == ["draft/a", "main"]
        clone.merge("draft/a", "Trial merge")
        assert "app.json" in clone.list_files("main")

    assert not clone_root.exists()
    assert "app.json" not in repo.list_files("main")


def test_temporary_clone_is_removed_on_error(repo: GitRepository) -> None:
    with pytest.raises(RuntimeError), repo.temporary_clone() as clone:
        clone_root = clone.root
        raise RuntimeError("boom")

    assert not clone_root.exists()


# -- .gitignore ------------------------------------------------------------------


def test_set_ignored_patterns(repo: GitRepository) -> None:
    assert repo.ignored_patterns() == []

    assert repo.set_ignored_patterns(["*.tmp", ".env"]) is True
    assert repo.set_ignored_patterns(["*.tmp", ".env"]) is False
    assert repo.ignored_patterns() == ["*.tmp", ".env"]


def test_temporary_clone_cleans_up_when_clone_fails(
    repo: GitRepository, mocker: MockerFixture
) -> None:
    mocker.patch.object(
        GitRepository, "clone", side_effect=GitOperationError("clone", "disk full")
    )
    remove = mocker.patch("gitconfig_cli.git_repository._remove_tree")

    with (
        pytest.raises(GitOperationError, match="disk full"),
        repo.temporary_clone(),
    ):
        pass  # pragma: no cover - never entered

    remove.assert_called_once()


def test_track_remote_branches_without_remote_is_a_no_op(repo: GitRepository) -> None:
    repo._track_remote_branches()

    assert repo.list_branches() == ["main"]
