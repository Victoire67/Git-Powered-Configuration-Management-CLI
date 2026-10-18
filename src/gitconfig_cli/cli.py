"""Command-line interface built with Click.

The commands are thin: they parse arguments, call
:class:`~gitconfig_cli.manager.ConfigManager` and print results through
:mod:`gitconfig_cli.output`. The manager is created by a
:class:`ManagerFactory`, so the CLI never constructs Git objects directly
and tests can inject their own factory through Click's ``obj``.

Every invocation is recorded in the audit log (``cli.command``), and every
:class:`~gitconfig_cli.exceptions.GitConfigError` is turned into a short
error message with a hint and a non-zero exit code.
"""

import getpass
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import click

from gitconfig_cli import __version__, output
from gitconfig_cli.abstractions import ConfigData
from gitconfig_cli.config_parser import ConfigParser, parse_value
from gitconfig_cli.exceptions import (
    ConfigFormatError,
    GitConfigError,
    MergeConflictError,
)
from gitconfig_cli.git_repository import GitRepository
from gitconfig_cli.keypath import set_value
from gitconfig_cli.logging_config import (
    DEFAULT_LOG_FILE,
    audit,
    bind_context,
    configure_logging,
)
from gitconfig_cli.manager import ConfigManager, Side


class ManagerFactory:
    """Creates :class:`ConfigManager` instances wired to real Git repositories."""

    def __init__(self, main_branch: str = "main") -> None:
        """Initialize the factory.

        Args:
            main_branch: Branch that holds published configurations.
        """
        self.main_branch = main_branch
        self.parser = ConfigParser()

    def open(self, path: Path) -> ConfigManager:
        """Open an existing configuration repository.

        Args:
            path: Repository root.

        Returns:
            A manager for the repository.
        """
        return ConfigManager(GitRepository.open(path), self.parser, self.main_branch)

    def init(self, path: Path) -> ConfigManager:
        """Create and initialize a new configuration repository.

        Args:
            path: Directory for the repository.

        Returns:
            A manager for the new repository.
        """
        repo = GitRepository.init(path, self.main_branch)
        manager = ConfigManager(repo, self.parser, self.main_branch)
        manager.initialize()
        return manager

    def clone(self, url: str, path: Path) -> ConfigManager:
        """Clone a configuration repository.

        Args:
            url: URL or path of the repository to clone.
            path: Destination directory.

        Returns:
            A manager for the clone.
        """
        repo = GitRepository.clone(url, path)
        return ConfigManager(repo, self.parser, self.main_branch)


@dataclass
class AppContext:
    """State shared by all commands of one invocation.

    Attributes:
        repo_path: Configuration repository selected with ``--repo``.
        factory: Creates managers; replaceable in tests.
    """

    repo_path: Path
    factory: ManagerFactory = field(default_factory=ManagerFactory)
    _manager: ConfigManager | None = None

    def manager(self) -> ConfigManager:
        """Return the manager for :attr:`repo_path`, opening it on first use."""
        if self._manager is None:
            self._manager = self.factory.open(self.repo_path)
        return self._manager


pass_app = click.make_pass_decorator(AppContext)


def _echo(lines: Sequence[str]) -> None:
    """Print several lines to stdout."""
    for line in lines:
        click.echo(line)


def _fail(
    message: str, hints: Sequence[str] = (), code: int = output.EXIT_ERROR
) -> NoReturn:
    """Print an error with hints to stderr and exit."""
    click.secho(f"Error: {message}", fg="red", err=True)
    for hint in hints:
        click.secho(f"Hint: {hint}", fg="yellow", err=True)
    raise SystemExit(code)


def _report(error: GitConfigError, main_branch: str) -> NoReturn:
    """Render a tool error (with conflict details if any) and exit."""
    if isinstance(error, MergeConflictError):
        click.secho(f"Error: {error}", fg="red", err=True)
        for line in output.format_conflicts(
            error.details, error.target, error.source, main_branch
        ):
            click.echo(line, err=True)
        for line in output.conflict_guidance(error, main_branch):
            click.secho(line, fg="yellow", err=True)
        raise SystemExit(output.EXIT_CONFLICT)
    _fail(str(error), output.hints_for(error))


class AuditedGroup(click.Group):
    """Root command group that configures logging, audits and reports errors."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Remember the raw arguments for the audit trail."""
        ctx.meta["argv"] = list(args)
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> Any:
        """Run the selected command inside an audit record."""
        params = ctx.params
        log_file = None if params.get("no_audit_log") else params.get("log_file")
        configure_logging(log_file=log_file, verbosity=params.get("verbose", 0))
        bind_context(
            repo=str(Path(params.get("repo") or ".").resolve()),
            user=getpass.getuser(),
        )
        main_branch = params.get("main_branch") or "main"
        try:
            with audit("cli.command", argv=ctx.meta.get("argv", [])):
                return super().invoke(ctx)
        except GitConfigError as error:
            _report(error, main_branch)


def _read_source(
    from_file: Path | None, data: str | None, assignments: Sequence[str]
) -> ConfigData:
    """Build configuration data from --from-file / --data / --set options."""
    if from_file is not None and data is not None:
        raise click.UsageError("use either --from-file or --data, not both")
    content: ConfigData = {}
    if from_file is not None:
        content = ConfigParser().load_file(from_file)
    elif data is not None:
        try:
            content = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ConfigFormatError("--data", f"invalid JSON: {exc}") from exc
        if not isinstance(content, dict):
            raise ConfigFormatError("--data", "must be a JSON object")
    for assignment in assignments:
        key, sep, raw = assignment.partition("=")
        if not sep or not key:
            raise click.BadParameter(
                f"'{assignment}' is not KEY=VALUE", param_hint="--set"
            )
        try:
            content = set_value(content, key, parse_value(raw))
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--set") from exc
    return content


def _check_source(draft: str | None, ref: str | None) -> None:
    """Reject using --draft and --ref together."""
    if draft is not None and ref is not None:
        raise click.UsageError("use either --draft or --ref, not both")


def _committed(sha: str | None, what: str) -> None:
    """Print the outcome of a change that may have been a no-op."""
    if sha is None:
        click.echo(f"No change: {what} already had that content.")
    else:
        click.echo(f"Committed {sha[:7]}: {what}")


draft_option = click.option(
    "--draft",
    "-d",
    required=True,
    help="Draft to change (created from main if needed).",
)
message_option = click.option("--message", "-m", help="Commit message.")
read_draft_option = click.option("--draft", "-d", help="Read from this draft.")
ref_option = click.option("--ref", "-r", help="Read from this branch or commit.")
prefer_option = click.option(
    "--prefer",
    type=click.Choice(["draft", "main"]),
    help="Resolve keys changed on both sides in favour of this side.",
)


@click.group(cls=AuditedGroup)
@click.version_option(__version__, prog_name=output.PROG)
@click.option(
    "--repo",
    "-C",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    envvar="GITCONFIG_REPO",
    show_default=True,
    help="Configuration repository to operate on.",
)
@click.option(
    "--main-branch",
    default="main",
    envvar="GITCONFIG_MAIN_BRANCH",
    show_default=True,
    help="Branch that holds published configurations.",
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_LOG_FILE,
    envvar="GITCONFIG_LOG_FILE",
    show_default=True,
    help="Where to append the JSON audit trail.",
)
@click.option("--no-audit-log", is_flag=True, help="Do not write the audit file.")
@click.option("--verbose", "-v", count=True, help="Show log events (-vv: Git calls).")
@click.pass_context
def cli(
    ctx: click.Context,
    repo: Path,
    main_branch: str,
    log_file: Path,
    no_audit_log: bool,
    verbose: int,
) -> None:
    """Manage JSON/YAML configurations with Git as a versioned backend.

    Changes are made on draft branches (draft/<name>), reviewed, then
    published by merging them into the main branch. Every change is a
    commit, so any version can be inspected or rolled back.
    """
    if not isinstance(ctx.obj, AppContext):
        ctx.obj = AppContext(repo_path=repo, factory=ManagerFactory(main_branch))
    else:
        ctx.obj.repo_path = repo


# -- repository --------------------------------------------------------------


@cli.command()
@click.argument(
    "path", required=False, type=click.Path(file_okay=False, path_type=Path)
)
@pass_app
def init(app: AppContext, path: Path | None) -> None:
    """Create a configuration repository (default: the --repo directory)."""
    target = path or app.repo_path
    manager = app.factory.init(target)
    click.echo(
        f"Initialized configuration repository in {target.resolve()} "
        f"on branch '{manager.main_branch}'."
    )


def _default_clone_dir(url: str) -> str:
    """Directory name Git would use: last path component without ``.git``."""
    last = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
    return last.removesuffix(".git") or "configs"


@cli.command()
@click.argument("url")
@click.argument(
    "path", required=False, type=click.Path(file_okay=False, path_type=Path)
)
@pass_app
def clone(app: AppContext, url: str, path: Path | None) -> None:
    """Clone an existing configuration repository from URL."""
    target = path or Path(_default_clone_dir(url))
    manager = app.factory.clone(url, target)
    drafts = [d.name for d in manager.list_drafts()]
    click.echo(f"Cloned {url} into {target.resolve()}.")
    if drafts:
        click.echo("Drafts: " + ", ".join(drafts))


@cli.command()
@pass_app
def status(app: AppContext) -> None:
    """Show the current branch, drafts and any merge in progress."""
    manager = app.manager()
    _echo(
        output.format_status(
            manager.status(), str(app.repo_path.resolve()), manager.main_branch
        )
    )


# -- drafts ------------------------------------------------------------------


@cli.group()
def draft() -> None:
    """Create, list, compare, sync and delete drafts."""


@draft.command("create")
@click.argument("name")
@click.option("--from", "base", help="Branch or commit to start from (default: main).")
@pass_app
def draft_create(app: AppContext, name: str, base: str | None) -> None:
    """Create draft NAME as branch draft/NAME."""
    branch = app.manager().create_draft(name, base)
    click.echo(f"Created draft '{name}' (branch {branch}).")


@draft.command("list")
@pass_app
def draft_list(app: AppContext) -> None:
    """List drafts and how far they are from main."""
    manager = app.manager()
    drafts = manager.list_drafts()
    if not drafts:
        click.echo("No drafts.")
    for item in drafts:
        click.echo(
            f"{item.name:<24} {item.ahead} ahead, {item.behind} behind "
            f"{manager.main_branch}"
        )


@draft.command("delete")
@click.argument("name")
@click.option("--force", is_flag=True, help="Delete even with unpublished changes.")
@pass_app
def draft_delete(app: AppContext, name: str, force: bool) -> None:
    """Delete draft NAME."""
    app.manager().delete_draft(name, force=force)
    click.echo(f"Deleted draft '{name}'.")


@draft.command("diff")
@click.argument("name")
@pass_app
def draft_diff(app: AppContext, name: str) -> None:
    """Show what draft NAME would change in main."""
    diff = app.manager().diff_draft(name)
    click.echo(diff if diff else f"Draft '{name}' has no changes.")


@draft.command("sync")
@click.argument("name")
@prefer_option
@pass_app
def draft_sync(app: AppContext, name: str, prefer: Side | None) -> None:
    """Merge the latest main into draft NAME.

    Edits to different keys are merged automatically. If the same key was
    changed on both sides, the merge is left in progress for you to edit;
    finish with 'resolve' or cancel with 'abort'.
    """
    manager = app.manager()
    result = manager.sync_draft(name, prefer=prefer)
    _echo(output.format_sync(result, name, manager.main_branch))


# -- configurations ----------------------------------------------------------


@cli.command("list")
@read_draft_option
@ref_option
@pass_app
def list_configs(app: AppContext, draft: str | None, ref: str | None) -> None:
    """List configuration files (on main unless --draft/--ref is given)."""
    _check_source(draft, ref)
    names = app.manager().list_configs(draft=draft, ref=ref)
    _echo(names or ["No configurations."])


@cli.command()
@click.argument("name")
@read_draft_option
@ref_option
@click.option("--key", "-k", help="Only show this dotted key, e.g. server.port.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["raw", "json", "yaml"]),
    default="raw",
    show_default=True,
    help="Output format.",
)
@pass_app
def show(
    app: AppContext,
    name: str,
    draft: str | None,
    ref: str | None,
    key: str | None,
    fmt: str,
) -> None:
    """Show configuration NAME (from main unless --draft/--ref is given)."""
    _check_source(draft, ref)
    manager = app.manager()
    if key is not None:
        value = manager.get_value(name, key, draft=draft, ref=ref)
        click.echo(output.format_value(value))
        return
    if fmt == "raw":
        click.echo(manager.read_text(name, draft=draft, ref=ref), nl=False)
        return
    data = manager.read_config(name, draft=draft, ref=ref)
    click.echo(ConfigParser().dumps(f"out.{fmt}", data), nl=False)


@cli.command()
@click.argument("name")
@draft_option
@click.option(
    "--from-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Initial content from a JSON/YAML file (converted if needed).",
)
@click.option("--data", help="Initial content as a JSON object.")
@click.option(
    "--set", "assignments", multiple=True, help="KEY=VALUE to set; repeatable."
)
@message_option
@pass_app
def create(
    app: AppContext,
    name: str,
    draft: str,
    from_file: Path | None,
    data: str | None,
    assignments: tuple[str, ...],
    message: str | None,
) -> None:
    """Create a new JSON or YAML configuration NAME on a draft."""
    content = _read_source(from_file, data, assignments)
    sha = app.manager().create_config(name, content, draft=draft, message=message)
    click.echo(f"Created {name} on draft '{draft}' (commit {sha[:7]}).")


@cli.command("set")
@click.argument("name")
@click.argument("key")
@click.argument("value")
@draft_option
@click.option("--string", "as_string", is_flag=True, help="Store VALUE as a string.")
@message_option
@pass_app
def set_command(
    app: AppContext,
    name: str,
    key: str,
    value: str,
    draft: str,
    as_string: bool,
    message: str | None,
) -> None:
    """Set KEY to VALUE in configuration NAME on a draft.

    VALUE is read as JSON when possible (8080, true, null, [1,2]), otherwise
    as text. Use --string to always store text.
    """
    parsed = value if as_string else parse_value(value)
    sha = app.manager().set_value(name, key, parsed, draft=draft, message=message)
    _committed(sha, f"{name} {key}={output.format_value(parsed)}")


@cli.command()
@click.argument("name")
@click.argument("key")
@draft_option
@message_option
@pass_app
def unset(
    app: AppContext, name: str, key: str, draft: str, message: str | None
) -> None:
    """Remove KEY from configuration NAME on a draft."""
    sha = app.manager().unset_value(name, key, draft=draft, message=message)
    _committed(sha, f"{name} without {key}")


@cli.command()
@click.argument("name")
@draft_option
@click.option(
    "--from-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="New content from a JSON/YAML file.",
)
@click.option("--data", help="New content as a JSON object.")
@message_option
@pass_app
def update(
    app: AppContext,
    name: str,
    draft: str,
    from_file: Path | None,
    data: str | None,
    message: str | None,
) -> None:
    """Replace the whole content of configuration NAME on a draft."""
    if from_file is None and data is None:
        raise click.UsageError("give the new content with --from-file or --data")
    content = _read_source(from_file, data, ())
    sha = app.manager().replace_config(name, content, draft=draft, message=message)
    _committed(sha, name)


@cli.command()
@click.argument("name")
@draft_option
@message_option
@pass_app
def edit(app: AppContext, name: str, draft: str, message: str | None) -> None:
    """Open configuration NAME from a draft in your $EDITOR and commit the result."""
    manager = app.manager()
    original = manager.draft_text(name, draft)
    edited = click.edit(original, extension=Path(name).suffix, require_save=True)
    if edited is None or edited == original:
        click.echo("No changes made.")
        return
    content = ConfigParser().loads(name, edited)
    sha = manager.replace_config(name, content, draft=draft, message=message)
    _committed(sha, name)


@cli.command()
@click.argument("name")
@draft_option
@message_option
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@pass_app
def delete(
    app: AppContext, name: str, draft: str, message: str | None, yes: bool
) -> None:
    """Delete configuration NAME on a draft."""
    if not yes:
        click.confirm(f"Delete {name} on draft '{draft}'?", abort=True)
    sha = app.manager().delete_config(name, draft=draft, message=message)
    click.echo(f"Deleted {name} on draft '{draft}' (commit {sha[:7]}).")


# -- history ------------------------------------------------------------------


@cli.command()
@click.argument("name")
@read_draft_option
@ref_option
@click.option(
    "--limit", "-n", type=click.IntRange(min=1), help="Show at most N entries."
)
@click.option("--all", "include_drafts", is_flag=True, help="Include draft commits.")
@pass_app
def history(
    app: AppContext,
    name: str,
    draft: str | None,
    ref: str | None,
    limit: int | None,
    include_drafts: bool,
) -> None:
    """List the versions of configuration NAME, newest first."""
    _check_source(draft, ref)
    commits = app.manager().history(
        name, draft=draft, ref=ref, limit=limit, include_drafts=include_drafts
    )
    _echo(output.format_history(commits))


@cli.command()
@click.argument("name")
@click.option("--to", "to", help="Commit (or branch) whose version to restore.")
@click.option(
    "--steps", type=click.IntRange(min=1), help="Go back N published versions."
)
@click.option("--draft", "-d", help="Roll back on a draft instead of on main.")
@message_option
@pass_app
def rollback(
    app: AppContext,
    name: str,
    to: str | None,
    steps: int | None,
    draft: str | None,
    message: str | None,
) -> None:
    """Restore an earlier version of NAME as a new commit.

    History is never rewritten: the old content is committed on top.
    """
    if (to is None) == (steps is None):
        raise click.UsageError("give exactly one of --to or --steps")
    sha = app.manager().rollback(name, to=to, steps=steps, draft=draft, message=message)
    _committed(sha, f"{name} rolled back")


# -- publishing and conflicts -------------------------------------------------


@cli.command()
@click.argument("name")
@prefer_option
@click.option("--squash", is_flag=True, help="Publish the draft as a single commit.")
@click.option("--keep-draft", is_flag=True, help="Keep the draft branch afterwards.")
@click.option("--dry-run", is_flag=True, help="Only check, in a temporary clone.")
@message_option
@pass_app
def publish(
    app: AppContext,
    name: str,
    prefer: Side | None,
    squash: bool,
    keep_draft: bool,
    dry_run: bool,
    message: str | None,
) -> None:
    """Publish draft NAME by merging it into main.

    Edits to different keys are merged automatically. If the same key was
    changed on both sides, nothing is published and the conflicting keys
    are listed with instructions.
    """
    manager = app.manager()
    if dry_run:
        preview = manager.preview_publish(name, prefer=prefer, squash=squash)
        _echo(output.format_preview(preview, manager.main_branch))
        if preview.conflicts:
            raise SystemExit(output.EXIT_CONFLICT)
        if preview.problems:
            raise SystemExit(output.EXIT_ERROR)
        return
    result = manager.publish(
        name, prefer=prefer, squash=squash, keep_draft=keep_draft, message=message
    )
    _echo(output.format_publish(result))


@cli.command()
@message_option
@pass_app
def resolve(app: AppContext, message: str | None) -> None:
    """Commit a merge after fixing the conflicted files by hand."""
    sha = app.manager().resolve_conflicts(message)
    click.echo(f"Conflicts resolved (commit {sha[:7]}).")


@cli.command()
@pass_app
def abort(app: AppContext) -> None:
    """Cancel a merge in progress and restore the draft."""
    branch = app.manager().abort_merge()
    click.echo(f"Merge aborted; '{branch}' is back to its previous state.")


# -- remotes ------------------------------------------------------------------


@cli.command()
@click.option("--draft", "-d", help="Push this draft instead of main.")
@click.option("--remote", default="origin", show_default=True)
@pass_app
def push(app: AppContext, draft: str | None, remote: str) -> None:
    """Push main (or a draft) to a remote."""
    branch = app.manager().push(draft=draft, remote=remote)
    click.echo(f"Pushed '{branch}' to '{remote}'.")


@cli.command()
@click.option("--remote", default="origin", show_default=True)
@pass_app
def pull(app: AppContext, remote: str) -> None:
    """Fast-forward main from a remote."""
    manager = app.manager()
    manager.pull(remote=remote)
    click.echo(f"Updated '{manager.main_branch}' from '{remote}'.")


# -- .gitignore ---------------------------------------------------------------


@cli.group()
def ignore() -> None:
    """Manage the tool-owned block of the repository's .gitignore."""


@ignore.command("list")
@pass_app
def ignore_list(app: AppContext) -> None:
    """List the managed ignore patterns."""
    _echo(app.manager().ignored_patterns() or ["No managed patterns."])


@ignore.command("add")
@click.argument("patterns", nargs=-1, required=True)
@pass_app
def ignore_add(app: AppContext, patterns: tuple[str, ...]) -> None:
    """Add PATTERNS to .gitignore (committed on main)."""
    _committed(app.manager().update_ignored(add=patterns), ".gitignore")


@ignore.command("remove")
@click.argument("patterns", nargs=-1, required=True)
@pass_app
def ignore_remove(app: AppContext, patterns: tuple[str, ...]) -> None:
    """Remove PATTERNS from .gitignore (committed on main)."""
    _committed(app.manager().update_ignored(remove=patterns), ".gitignore")


def main() -> None:
    """Console-script entry point."""
    cli(prog_name=output.PROG)
