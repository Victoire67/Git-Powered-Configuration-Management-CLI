"""End-to-end tests of the CLI with Click's CliRunner and real repositories."""

import json
from collections.abc import Callable
from pathlib import Path

import git
import pytest
from click.testing import CliRunner, Result
from pytest_mock import MockerFixture

from gitconfig_cli import __version__
from gitconfig_cli.cli import AppContext, ManagerFactory, _default_clone_dir, cli, main
from gitconfig_cli.exceptions import GitOperationError
from gitconfig_cli.output import EXIT_CONFLICT, EXIT_ERROR

Run = Callable[..., Result]


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    return tmp_path / "configs"


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


@pytest.fixture
def run(repo_dir: Path, audit_log: Path) -> Run:
    runner = CliRunner()

    def _run(*args: str, input: str | None = None) -> Result:
        return runner.invoke(
            cli,
            ["--repo", str(repo_dir), "--log-file", str(audit_log), *args],
            input=input,
            catch_exceptions=False,
        )

    return _run


@pytest.fixture
def initialized(run: Run) -> Run:
    assert run("init").exit_code == 0
    return run


@pytest.fixture
def with_app(initialized: Run) -> Run:
    """main holds app.json = {"server": {"host": "localhost", "port": 8080}}."""
    data = '{"server": {"host": "localhost", "port": 8080}}'
    assert (
        initialized("create", "app.json", "--draft", "base", "--data", data).exit_code
        == 0
    )
    assert initialized("publish", "base").exit_code == 0
    return initialized


def _main_json(repo_dir: Path, name: str = "app.json") -> object:
    blob = git.Repo(repo_dir).heads["main"].commit.tree / name
    return json.loads(blob.data_stream.read())


# -- global behaviour -------------------------------------------------------------


def test_version() -> None:
    result = CliRunner().invoke(cli, ["--version"])

    assert __version__ in result.output


def test_help_lists_workflow_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    for command in ("init", "draft", "create", "set", "publish", "rollback", "resolve"):
        assert command in result.output


def test_main_entry_point(mocker: MockerFixture) -> None:
    command = mocker.patch("gitconfig_cli.cli.cli")

    main()

    command.assert_called_once_with(prog_name="gitconfig-cli")


def test_error_outside_a_repository(run: Run, repo_dir: Path) -> None:
    repo_dir.mkdir()

    result = run("list")

    assert result.exit_code == EXIT_ERROR
    assert "not a Git repository" in result.output
    assert "Hint: Run 'gitconfig-cli init'" in result.output


def test_every_command_is_audited(initialized: Run, audit_log: Path) -> None:
    initialized("list")

    events = [json.loads(line) for line in audit_log.read_text("utf-8").splitlines()]
    commands = [e for e in events if e.get("action") == "cli.command"]
    assert commands[-1]["event"] == "succeeded"
    assert commands[-1]["argv"][-1] == "list"
    assert all("user" in e and "repo" in e for e in commands)


def test_failed_command_is_audited(run: Run, audit_log: Path) -> None:
    run("list")

    last = json.loads(audit_log.read_text("utf-8").splitlines()[-1])
    assert last["event"] == "failed"
    assert last["error_type"] == "InvalidRepoError"


def test_no_audit_log_flag(repo_dir: Path, tmp_path: Path) -> None:
    log_file = tmp_path / "unused.log"

    CliRunner().invoke(
        cli,
        [
            "--repo",
            str(repo_dir),
            "--log-file",
            str(log_file),
            "--no-audit-log",
            "init",
        ],
    )

    assert not log_file.exists()


def test_injected_factory_is_used(repo_dir: Path, mocker: MockerFixture) -> None:
    factory = mocker.create_autospec(ManagerFactory, instance=True)
    factory.open.return_value.list_configs.return_value = ["fake.json"]
    app = AppContext(repo_path=Path("ignored"), factory=factory)

    result = CliRunner().invoke(
        cli, ["--no-audit-log", "--repo", str(repo_dir), "list"], obj=app
    )

    assert result.output == "fake.json\n"
    factory.open.assert_called_once_with(repo_dir)


# -- repository commands ----------------------------------------------------------


def test_init_creates_repository(run: Run, repo_dir: Path) -> None:
    result = run("init")

    assert result.exit_code == 0
    assert "on branch 'main'" in result.output
    assert (repo_dir / ".gitignore").exists()


def test_init_with_custom_main_branch(run: Run, repo_dir: Path) -> None:
    result = run("--main-branch", "trunk", "init")

    assert "on branch 'trunk'" in result.output
    assert git.Repo(repo_dir).active_branch.name == "trunk"


def test_init_twice_fails(initialized: Run) -> None:
    result = initialized("init")

    assert result.exit_code == EXIT_ERROR
    assert "already a Git repository" in result.output


def test_clone(with_app: Run, repo_dir: Path, tmp_path: Path, audit_log: Path) -> None:
    with_app("draft", "create", "wip")
    target = tmp_path / "copy"

    result = with_app("clone", str(repo_dir), str(target))

    assert result.exit_code == 0
    assert "Drafts: wip" in result.output
    assert (target / "app.json").exists()


def test_clone_default_directory(
    with_app: Run, repo_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = with_app("clone", str(repo_dir))

    assert result.exit_code == 0
    assert "Drafts" not in result.output
    assert (workdir / "configs" / "app.json").exists()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/team/app-config.git", "app-config"),
        ("git@github.com:team/app-config.git", "app-config"),
        ("git@host:app-config.git", "app-config"),
        ("C:\\work\\configs\\", "configs"),
        ("/srv/git/configs.git/", "configs"),
        (".git", "configs"),
    ],
)
def test_default_clone_dir(url: str, expected: str) -> None:
    assert _default_clone_dir(url) == expected


def test_status(with_app: Run) -> None:
    with_app("draft", "create", "wip")

    result = with_app("status")

    assert "On branch:  main" in result.output
    assert "wip" in result.output
    assert "0 ahead, 0 behind main" in result.output


def test_status_without_drafts_and_with_local_changes(
    with_app: Run, repo_dir: Path
) -> None:
    (repo_dir / "notes.txt").write_text("x")

    result = with_app("status")

    assert "Drafts: none" in result.output
    assert "Uncommitted changes: notes.txt" in result.output


# -- drafts --------------------------------------------------------------------------


def test_draft_lifecycle(with_app: Run, repo_dir: Path) -> None:
    assert "Created draft 'wip'" in with_app("draft", "create", "wip").output
    assert "wip" in with_app("draft", "list").output
    assert "has no changes" in with_app("draft", "diff", "wip").output

    with_app("set", "app.json", "server.port", "1", "--draft", "wip")
    assert '+    "port": 1' in with_app("draft", "diff", "wip").output

    result = with_app("draft", "delete", "wip")
    assert result.exit_code == EXIT_ERROR
    assert "not fully merged" in result.output

    assert with_app("draft", "delete", "wip", "--force").exit_code == 0
    assert with_app("draft", "list").output == "No drafts.\n"


def test_draft_create_from_ref(with_app: Run) -> None:
    result = with_app("draft", "create", "hotfix", "--from", "main")

    assert result.exit_code == 0


def test_draft_create_duplicate(with_app: Run) -> None:
    with_app("draft", "create", "wip")

    result = with_app("draft", "create", "wip")

    assert "already exists" in result.output
    assert "draft delete wip" in result.output


def test_invalid_draft_name(with_app: Run) -> None:
    result = with_app("draft", "create", "bad name")

    assert result.exit_code == EXIT_ERROR
    assert "Invalid draft name" in result.output


def test_unknown_draft_has_hint(with_app: Run) -> None:
    result = with_app("publish", "ghost")

    assert "Draft 'ghost' does not exist" in result.output
    assert "draft create ghost" in result.output


# -- creating, reading, updating ---------------------------------------------------


def test_create_from_yaml_file_into_json(
    initialized: Run, repo_dir: Path, tmp_path: Path
) -> None:
    source = tmp_path / "input.yaml"
    source.write_text("server:\n  port: 1\n", encoding="utf-8")

    result = initialized("create", "app.json", "-d", "x", "--from-file", str(source))
    initialized("publish", "x")

    assert result.exit_code == 0
    assert _main_json(repo_dir) == {"server": {"port": 1}}


def test_create_with_set_assignments(initialized: Run) -> None:
    initialized(
        "create", "db.yaml", "-d", "x", "--set", "host=db", "--set", "pool.size=5"
    )

    result = initialized("show", "db.yaml", "--draft", "x")

    assert result.output == "host: db\npool:\n  size: 5\n"


def test_create_empty(initialized: Run) -> None:
    initialized("create", "empty.json", "-d", "x")

    assert initialized("show", "empty.json", "-d", "x").output == "{}\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--data", "[1]"], "must be a JSON object"),
        (["--data", "{bad"], "invalid JSON"),
        (["--set", "novalue"], "is not KEY=VALUE"),
        (["--set", "a..b=1"], "invalid key path"),
    ],
)
def test_create_rejects_bad_input(
    initialized: Run, args: list[str], message: str
) -> None:
    result = initialized("create", "app.json", "-d", "x", *args)

    assert result.exit_code != 0
    assert message in result.output


def test_create_rejects_file_and_data_together(
    initialized: Run, tmp_path: Path
) -> None:
    source = tmp_path / "a.json"
    source.write_text("{}")

    result = initialized(
        "create", "a.json", "-d", "x", "--from-file", str(source), "--data", "{}"
    )

    assert "either --from-file or --data" in result.output


def test_create_unsupported_extension(initialized: Run) -> None:
    result = initialized("create", "notes.txt", "-d", "x")

    assert "unsupported file type" in result.output


def test_show_formats_and_keys(with_app: Run) -> None:
    assert '"port": 8080' in with_app("show", "app.json").output
    assert with_app("show", "app.json", "--key", "server.port").output == "8080\n"
    assert with_app("show", "app.json", "-k", "server.host").output == "localhost\n"
    assert (
        with_app("show", "app.json", "--format", "yaml").output
        == "server:\n  host: localhost\n  port: 8080\n"
    )
    assert json.loads(with_app("show", "app.json", "--format", "json").output)


def test_show_missing_key_and_config(with_app: Run) -> None:
    missing_key = with_app("show", "app.json", "--key", "nope")
    missing_file = with_app("show", "other.json")

    assert "Key 'nope' not found" in missing_key.output
    assert "Hint: Show the whole file" in missing_key.output
    assert "Configuration 'other.json' not found" in missing_file.output
    assert "gitconfig-cli list" in missing_file.output


def test_show_rejects_draft_and_ref(with_app: Run) -> None:
    result = with_app("show", "app.json", "--draft", "a", "--ref", "main")

    assert result.exit_code == 2
    assert "either --draft or --ref" in result.output


def test_list(with_app: Run) -> None:
    assert with_app("list").output == "app.json\n"
    assert with_app("list", "--ref", "main").output == "app.json\n"


def test_list_empty(initialized: Run) -> None:
    assert initialized("list").output == "No configurations.\n"


def test_set_typed_and_string_values(with_app: Run) -> None:
    with_app("set", "app.json", "server.port", "9090", "-d", "x")
    with_app("set", "app.json", "server.debug", "true", "-d", "x")
    with_app("set", "app.json", "server.tag", "123", "-d", "x", "--string")

    assert (
        with_app("show", "app.json", "-d", "x", "-k", "server.port").output == "9090\n"
    )
    assert (
        with_app("show", "app.json", "-d", "x", "-k", "server.debug").output == "true\n"
    )
    assert with_app("show", "app.json", "-d", "x", "-k", "server.tag").output == "123\n"
    data = json.loads(with_app("show", "app.json", "-d", "x").output)
    assert data["server"]["tag"] == "123"


def test_set_reports_commit_and_no_op(with_app: Run) -> None:
    first = with_app("set", "app.json", "server.port", "1", "-d", "x", "-m", "Port 1")
    second = with_app("set", "app.json", "server.port", "1", "-d", "x")

    assert first.output.startswith("Committed ")
    assert "server.port=1" in first.output
    assert second.output.startswith("No change:")


def test_unset(with_app: Run) -> None:
    result = with_app("unset", "app.json", "server.host", "-d", "x")

    assert result.exit_code == 0
    assert "host" not in with_app("show", "app.json", "-d", "x").output


def test_update_replaces_content(with_app: Run) -> None:
    result = with_app("update", "app.json", "-d", "x", "--data", '{"new": 1}')

    assert result.exit_code == 0
    assert json.loads(with_app("show", "app.json", "-d", "x").output) == {"new": 1}


def test_update_requires_content(with_app: Run) -> None:
    result = with_app("update", "app.json", "-d", "x")

    assert result.exit_code == 2
    assert "--from-file or --data" in result.output


def test_edit_commits_editor_result(with_app: Run, mocker: MockerFixture) -> None:
    mocker.patch("click.edit", return_value='{"edited": true}\n')

    result = with_app("edit", "app.json", "-d", "x")

    assert result.exit_code == 0
    assert with_app("show", "app.json", "-d", "x", "-k", "edited").output == "true\n"


def test_edit_without_changes(with_app: Run, mocker: MockerFixture) -> None:
    mocker.patch("click.edit", return_value=None)

    assert with_app("edit", "app.json", "-d", "x").output == "No changes made.\n"


def test_edit_with_invalid_result(with_app: Run, mocker: MockerFixture) -> None:
    mocker.patch("click.edit", return_value="{not json")

    result = with_app("edit", "app.json", "-d", "x")

    assert result.exit_code == EXIT_ERROR
    assert "malformed json" in result.output


def test_delete_asks_for_confirmation(with_app: Run) -> None:
    declined = with_app("delete", "app.json", "-d", "x", input="n\n")
    accepted = with_app("delete", "app.json", "-d", "x", "--yes")

    assert declined.exit_code == 1
    assert "Aborted" in declined.output
    assert "Deleted app.json" in accepted.output


def test_dirty_working_tree_is_refused(with_app: Run, repo_dir: Path) -> None:
    (repo_dir / "app.json").write_text("local edit")

    result = with_app("set", "app.json", "a", "1", "-d", "x")

    assert result.exit_code == EXIT_ERROR
    assert "uncommitted changes: app.json" in result.output
    assert "git stash" in result.output
    assert (repo_dir / "app.json").read_text() == "local edit"


# -- history and rollback -----------------------------------------------------------


@pytest.fixture
def three_versions(with_app: Run) -> Run:
    for port in ("1", "2"):
        with_app("set", "app.json", "server.port", port, "-d", f"v{port}")
        with_app("publish", f"v{port}")
    return with_app


def test_history(three_versions: Run) -> None:
    lines = three_versions("history", "app.json").output.splitlines()

    assert len(lines) == 3
    assert lines[0].endswith("Publish draft 'v2'")
    assert (
        len(three_versions("history", "app.json", "-n", "1").output.splitlines()) == 1
    )
    assert "Set server.port" in three_versions("history", "app.json", "--all").output


def test_rollback_steps(three_versions: Run, repo_dir: Path) -> None:
    result = three_versions("rollback", "app.json", "--steps", "2")

    assert result.output.startswith("Committed ")
    assert _main_json(repo_dir) == {"server": {"host": "localhost", "port": 8080}}


def test_rollback_to_commit(three_versions: Run, repo_dir: Path) -> None:
    sha = three_versions("history", "app.json").output.splitlines()[1].split()[0]

    three_versions("rollback", "app.json", "--to", sha)

    assert _main_json(repo_dir)["server"]["port"] == 1  # type: ignore[index]


def test_rollback_needs_one_target(three_versions: Run) -> None:
    result = three_versions("rollback", "app.json")

    assert result.exit_code == 2
    assert "exactly one of --to or --steps" in result.output


def test_rollback_unknown_revision(three_versions: Run) -> None:
    result = three_versions("rollback", "app.json", "--to", "nope")

    assert "Revision 'nope'" in result.output
    assert "gitconfig-cli history app.json" in result.output


# -- publishing and merge conflicts --------------------------------------------------


@pytest.fixture
def conflict(with_app: Run) -> Run:
    """Drafts 'a' and 'b' both changed server.port; 'a' is published."""
    with_app("draft", "create", "a")
    with_app("draft", "create", "b")
    with_app("set", "app.json", "server.port", "9000", "-d", "a")
    with_app("set", "app.json", "server.port", "7000", "-d", "b")
    with_app("publish", "a")
    return with_app


def test_publish_output(with_app: Run, repo_dir: Path) -> None:
    with_app("set", "app.json", "server.port", "1", "-d", "x")

    result = with_app("publish", "x")

    assert "Published draft 'x' to 'main'" in result.output
    assert "Changed: app.json" in result.output
    assert "Draft branch 'draft/x' was deleted." in result.output


def test_publish_squash_keep_draft_and_message(with_app: Run, repo_dir: Path) -> None:
    with_app("set", "app.json", "server.port", "1", "-d", "x")

    with_app("publish", "x", "--squash", "--keep-draft", "-m", "Release 1")

    raw = git.Repo(repo_dir)
    assert raw.heads["main"].commit.summary == "Release 1"
    assert "draft/x" in [h.name for h in raw.heads]


def test_publish_nothing(with_app: Run) -> None:
    with_app("draft", "create", "empty")

    assert "Nothing to publish" in with_app("publish", "empty").output


def test_publish_auto_merges_different_keys(with_app: Run, repo_dir: Path) -> None:
    with_app("draft", "create", "a")
    with_app("draft", "create", "b")
    with_app("set", "app.json", "server.host", "example.com", "-d", "a")
    with_app("set", "app.json", "server.port", "1", "-d", "b")
    with_app("publish", "a")

    result = with_app("publish", "b")

    assert "Automatically merged edits to different keys in: app.json" in result.output
    assert _main_json(repo_dir) == {"server": {"host": "example.com", "port": 1}}


def test_publish_conflict_report_and_guidance(conflict: Run, repo_dir: Path) -> None:
    result = conflict("publish", "b")

    assert result.exit_code == EXIT_CONFLICT
    assert "Merge conflict merging 'draft/b' into 'main': app.json" in result.output
    assert "server.port: base=8080  main=9000  draft=7000" in result.output
    assert "publish b --prefer draft" in result.output
    assert "draft sync b" in result.output
    assert _main_json(repo_dir)["server"]["port"] == 9000  # type: ignore[index]


def test_publish_dry_run_conflict(conflict: Run) -> None:
    result = conflict("publish", "b", "--dry-run")

    assert result.exit_code == EXIT_CONFLICT
    assert "would fail" in result.output
    assert "server.port: base=8080  main=9000  draft=7000" in result.output


def test_publish_dry_run_clean(with_app: Run) -> None:
    with_app("set", "app.json", "server.port", "1", "-d", "x")

    result = with_app("publish", "x", "--dry-run")

    assert result.exit_code == 0
    assert "can be published cleanly" in result.output
    assert "Would change: app.json" in result.output
    assert "x" in with_app("draft", "list").output


def test_publish_dry_run_auto_merge(with_app: Run) -> None:
    with_app("draft", "create", "a")
    with_app("draft", "create", "b")
    with_app("set", "app.json", "server.host", "h", "-d", "a")
    with_app("set", "app.json", "server.port", "1", "-d", "b")
    with_app("publish", "a")

    assert "Would auto-merge: app.json" in with_app("publish", "b", "--dry-run").output


def test_publish_dry_run_nothing(with_app: Run) -> None:
    with_app("draft", "create", "empty")

    assert "nothing to publish" in with_app("publish", "empty", "--dry-run").output


def test_publish_dry_run_invalid_result(with_app: Run, repo_dir: Path) -> None:
    raw = git.Repo(repo_dir)
    raw.git.checkout("-b", "draft/broken")
    (repo_dir / "bad.json").write_text("{oops")
    raw.git.add("bad.json")
    raw.git.commit("-m", "broken")
    raw.git.checkout("main")

    result = with_app("publish", "broken", "--dry-run")

    assert result.exit_code == EXIT_ERROR
    assert "bad.json" in result.output


def test_publish_with_preference(conflict: Run, repo_dir: Path) -> None:
    result = conflict("publish", "b", "--prefer", "draft")

    assert result.exit_code == 0
    assert _main_json(repo_dir)["server"]["port"] == 7000  # type: ignore[index]


def test_sync_resolve_publish_workflow(conflict: Run, repo_dir: Path) -> None:
    sync = conflict("draft", "sync", "b")
    assert sync.exit_code == EXIT_CONFLICT
    assert "left in progress on 'draft/b'" in sync.output
    assert "server.port: base=8080  draft=7000  main=9000" in sync.output

    blocked = conflict("set", "app.json", "x", "1", "-d", "other")
    assert "A merge is in progress" in blocked.output
    assert "gitconfig-cli resolve" in blocked.output

    status = conflict("status")
    assert "MERGE IN PROGRESS - conflicted files: app.json" in status.output

    not_fixed = conflict("resolve")
    assert "still contains conflict markers" in not_fixed.output

    resolved = {"server": {"host": "localhost", "port": 7500}}
    (repo_dir / "app.json").write_text(json.dumps(resolved, indent=2) + "\n")
    assert "Conflicts resolved" in conflict("resolve").output

    assert conflict("publish", "b").exit_code == 0
    assert _main_json(repo_dir) == resolved


def test_sync_abort(conflict: Run) -> None:
    conflict("draft", "sync", "b")

    result = conflict("abort")

    assert "Merge aborted; 'draft/b'" in result.output
    assert "MERGE IN PROGRESS" not in conflict("status").output


def test_resolve_without_merge(with_app: Run) -> None:
    result = with_app("resolve")

    assert "There is no merge in progress" in result.output
    assert "gitconfig-cli status" in result.output


def test_sync_clean(with_app: Run) -> None:
    with_app("draft", "create", "a")
    with_app("draft", "create", "b")
    with_app("set", "app.json", "server.host", "h", "-d", "a")
    with_app("set", "app.json", "server.port", "1", "-d", "b")
    with_app("publish", "a")

    result = with_app("draft", "sync", "b")

    assert "Merged 'main' into draft 'b'" in result.output
    assert "Automatically merged" in result.output
    assert "already up to date" in with_app("draft", "sync", "b").output


def test_sync_with_preference(conflict: Run) -> None:
    result = conflict("draft", "sync", "b", "--prefer", "main")

    assert result.exit_code == 0
    assert (
        conflict("show", "app.json", "-d", "b", "-k", "server.port").output == "9000\n"
    )


# -- remotes ---------------------------------------------------------------------------


@pytest.fixture
def remote(with_app: Run, repo_dir: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    git.Repo.init(bare, bare=True, initial_branch="main").close()
    git.Repo(repo_dir).create_remote("origin", str(bare))
    return bare


def test_push_and_pull(with_app: Run, remote: Path) -> None:
    with_app("draft", "create", "wip")

    assert "Pushed 'main' to 'origin'" in with_app("push").output
    assert "Pushed 'draft/wip'" in with_app("push", "--draft", "wip").output
    assert "Updated 'main' from 'origin'" in with_app("pull").output


def test_failed_push_is_reported(with_app: Run, mocker: MockerFixture) -> None:
    """A rejected push is simulated: no network or remote is used."""
    mocker.patch(
        "gitconfig_cli.git_repository.GitRepository.push",
        side_effect=GitOperationError(
            "push", "! [rejected] main -> main (fetch first)"
        ),
    )

    result = with_app("push")

    assert result.exit_code == EXIT_ERROR
    assert "Git 'push' failed: ! [rejected]" in result.output


def test_missing_identity_hint(with_app: Run, mocker: MockerFixture) -> None:
    mocker.patch(
        "gitconfig_cli.git_repository.GitRepository.commit",
        side_effect=GitOperationError("commit", "Please tell me who you are."),
    )

    result = with_app("set", "app.json", "a", "1", "-d", "x")

    assert "git config --global user.name" in result.output


# -- .gitignore ------------------------------------------------------------------------


def test_ignore_commands(initialized: Run, repo_dir: Path) -> None:
    assert "*.orig" in initialized("ignore", "list").output

    added = initialized("ignore", "add", "*.secret")
    again = initialized("ignore", "add", "*.secret")
    removed = initialized("ignore", "remove", "*.secret", ".env")

    assert added.output.startswith("Committed ")
    assert again.output.startswith("No change")
    assert removed.output.startswith("Committed ")
    patterns = initialized("ignore", "list").output.splitlines()
    assert "*.secret" not in patterns
    assert ".env" not in patterns


def test_ignore_list_empty(initialized: Run, repo_dir: Path) -> None:
    raw = git.Repo(repo_dir)
    raw.git.rm(".gitignore")
    raw.git.commit("-m", "drop gitignore")

    assert initialized("ignore", "list").output == "No managed patterns.\n"


def test_app_context_opens_manager_once(mocker: MockerFixture) -> None:
    factory = mocker.create_autospec(ManagerFactory, instance=True)
    app = AppContext(repo_path=Path("x"), factory=factory)

    assert app.manager() is app.manager()
    factory.open.assert_called_once()


def test_python_dash_m_entry_point(mocker: MockerFixture) -> None:
    import runpy

    entry = mocker.patch("gitconfig_cli.cli.main")

    runpy.run_module("gitconfig_cli", run_name="__main__")

    entry.assert_called_once_with()
