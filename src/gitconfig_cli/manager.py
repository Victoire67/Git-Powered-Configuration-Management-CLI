"""Application logic: the draft / publish workflow for configurations.

:class:`ConfigManager` orchestrates the two abstractions defined in
:mod:`gitconfig_cli.abstractions`:

* a :class:`~gitconfig_cli.abstractions.VersionControl` for storage and
  history (in production a :class:`~gitconfig_cli.git_repository.GitRepository`),
* a :class:`~gitconfig_cli.abstractions.ConfigSerializer` for the file
  formats (in production a :class:`~gitconfig_cli.config_parser.ConfigParser`).

Branching strategy:

* ``main`` always holds the *published* configurations.
* Every change is made on a *draft* branch named ``draft/<name>``,
  created from ``main``. Each change is one commit.
* *Publishing* merges the draft into ``main`` with a merge commit (or a
  squash commit) and deletes the draft branch.
* *Rollback* restores an earlier version of a file as a new commit, so
  history is never rewritten.
"""

import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any, Literal

from gitconfig_cli import keypath
from gitconfig_cli.abstractions import (
    ConfigData,
    ConfigSerializer,
    MergePreference,
    VersionControl,
)
from gitconfig_cli.conflicts import ConflictResolver, has_conflict_markers
from gitconfig_cli.exceptions import (
    ConfigAlreadyExists,
    ConfigFormatError,
    ConfigNotFound,
    DraftAlreadyExists,
    DraftNotFound,
    GitOperationError,
    InvalidNameError,
    KeyNotFound,
    MergeConflictError,
    MergeInProgressError,
    NoMergeInProgress,
    RevisionNotFound,
)
from gitconfig_cli.gitignore import DEFAULT_PATTERNS, managed_patterns
from gitconfig_cli.logging_config import audit, bound_context, get_logger
from gitconfig_cli.models import (
    CommitInfo,
    DraftInfo,
    MergeResult,
    PublishPreview,
    PublishResult,
    RepoStatus,
)

log = get_logger("manager")

DRAFT_PREFIX = "draft/"
GITIGNORE = ".gitignore"
README = "README.md"

Side = Literal["draft", "main"]
"""Which branch wins when both changed the same thing."""

_DRAFT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")

_README_TEXT = """# Configuration repository

This repository is managed by **gitconfig-cli**.

* `main` holds the published configurations.
* Changes are made on `draft/<name>` branches and published by merging.

Use `gitconfig-cli --help` to see the available commands.
"""


class ConfigManager:
    """Create, update, publish and roll back versioned configurations.

    Example:
        >>> repo = GitRepository.init(Path("configs"))  # doctest: +SKIP
        >>> manager = ConfigManager(repo, ConfigParser())  # doctest: +SKIP
        >>> manager.initialize()  # doctest: +SKIP
        >>> manager.create_config("app.json", {"port": 80}, draft="setup")
        >>> manager.publish("setup")  # doctest: +SKIP
    """

    def __init__(
        self,
        repo: VersionControl,
        parser: ConfigSerializer,
        main_branch: str = "main",
    ) -> None:
        """Initialize the manager.

        Args:
            repo: Version-control backend.
            parser: Serializer for configuration files.
            main_branch: Branch that holds published configurations.
        """
        self._repo = repo
        self._parser = parser
        self._main = main_branch
        self._resolver = ConflictResolver(repo, parser)

    @property
    def main_branch(self) -> str:
        """Branch that holds published configurations."""
        return self._main

    # -- names -----------------------------------------------------------

    @staticmethod
    def draft_branch(name: str) -> str:
        """Return the branch name used for a draft.

        Args:
            name: Draft name, e.g. ``"new-port"``.

        Returns:
            The branch name, e.g. ``"draft/new-port"``.
        """
        return f"{DRAFT_PREFIX}{name}"

    @staticmethod
    def validate_draft_name(name: str) -> None:
        """Check that a draft name is safe to use as a branch name.

        Args:
            name: Draft name.

        Raises:
            InvalidNameError: If the name is empty or contains characters
                that Git does not allow in branch names.
        """
        if not _DRAFT_NAME.match(name) or ".." in name or name.endswith(".lock"):
            raise InvalidNameError(
                "draft",
                name,
                "use letters, digits, '.', '_', '-' and '/' (e.g. 'new-port')",
            )

    def validate_config_name(self, name: str) -> None:
        """Check that a configuration name is a safe, supported relative path.

        Args:
            name: Path such as ``"app.json"`` or ``"services/api.yaml"``.

        Raises:
            InvalidNameError: If the path is absolute, escapes the repository
                or points inside ``.git``.
            ConfigFormatError: If the extension is not supported.
        """
        path = PurePosixPath(name)
        if not name or "\\" in name or path.is_absolute() or ":" in name:
            raise InvalidNameError(
                "configuration", name, "use a relative path with '/' separators"
            )
        if any(part in ("..", ".", ".git") for part in path.parts):
            raise InvalidNameError(
                "configuration", name, "path must stay inside the repository"
            )
        if not self._parser.supports(name):
            exts = ", ".join(self._parser.supported_extensions)
            raise ConfigFormatError(name, f"unsupported file type; use {exts}")

    # -- guards ----------------------------------------------------------

    def _ensure_no_merge(self) -> None:
        """Refuse to start an action while a merge awaits resolution."""
        if self._repo.is_merging():
            raise MergeInProgressError(
                self._repo.current_branch(), self._repo.conflicted_files()
            )

    def _ensure_ready(self) -> None:
        """Require a clean working tree with no merge in progress."""
        self._ensure_no_merge()
        self._repo.ensure_clean()

    def _require_draft(self, name: str) -> str:
        """Return the branch of an existing draft."""
        self.validate_draft_name(name)
        branch = self.draft_branch(name)
        if not self._repo.branch_exists(branch):
            raise DraftNotFound(name)
        return branch

    def _resolve_ref(self, draft: str | None, ref: str | None) -> str:
        """Pick the ref to read from: a draft, an explicit ref, or main."""
        if draft is not None:
            return self._require_draft(draft)
        return ref or self._main

    # -- repository setup ------------------------------------------------

    def initialize(self) -> str:
        """Create the first commit of a new configuration repository.

        Writes a README and a managed ``.gitignore`` on the main branch.

        Returns:
            The hash of the initial commit.
        """
        with audit("repo.initialize", branch=self._main) as log:
            self._repo.set_ignored_patterns(DEFAULT_PATTERNS)
            self._repo.write_file(README, _README_TEXT)
            sha = self._repo.commit(
                "Initialize configuration repository", [GITIGNORE, README]
            )
            log.info("committed", commit=sha)
            return sha

    def ignored_patterns(self) -> list[str]:
        """Return the patterns in the managed block of ``.gitignore`` on main.

        Returns:
            The managed patterns.
        """
        try:
            text = self._repo.read_file(GITIGNORE, self._main)
        except FileNotFoundError:
            return []
        return managed_patterns(text)

    def update_ignored(
        self, add: Iterable[str] = (), remove: Iterable[str] = ()
    ) -> str | None:
        """Add or remove managed ``.gitignore`` patterns and commit on main.

        Args:
            add: Patterns to add.
            remove: Patterns to remove.

        Returns:
            The commit hash, or ``None`` if nothing changed.
        """
        add, remove = list(add), list(remove)
        with audit("gitignore.update", add=add, remove=remove) as log:
            self._ensure_ready()
            with self._repo.checked_out(self._main):
                current = self._repo.ignored_patterns()
                wanted = [p for p in [*current, *add] if p not in remove]
                if not self._repo.set_ignored_patterns(wanted):
                    log.info("unchanged")
                    return None
                sha = self._repo.commit("Update .gitignore", [GITIGNORE])
            log.info("committed", commit=sha)
            return sha

    def status(self) -> RepoStatus:
        """Describe the current state of the repository.

        Returns:
            Branch, uncommitted files, merge state and drafts.
        """
        return RepoStatus(
            branch=self._repo.current_branch(),
            dirty_files=self._repo.dirty_files(),
            merging=self._repo.is_merging(),
            conflicts=self._repo.conflicted_files(),
            drafts=self.list_drafts(),
        )

    # -- drafts ----------------------------------------------------------

    def create_draft(self, name: str, base: str | None = None) -> str:
        """Create a draft branch.

        Args:
            name: Draft name.
            base: Branch or commit to start from (defaults to main).

        Returns:
            The draft's branch name.

        Raises:
            InvalidNameError: If the name is not valid.
            DraftAlreadyExists: If the draft already exists.
        """
        self.validate_draft_name(name)
        branch = self.draft_branch(name)
        start = base or self._main
        with audit("draft.create", draft=name, base=start) as log:
            if self._repo.branch_exists(branch):
                raise DraftAlreadyExists(name)
            self._repo.create_branch(branch, start)
            log.info("branch.created", branch=branch)
            return branch

    def list_drafts(self) -> list[DraftInfo]:
        """List all drafts with their divergence from main.

        Returns:
            One entry per draft, sorted by name.
        """
        drafts = []
        for branch in self._repo.list_branches(DRAFT_PREFIX):
            ahead, behind = self._repo.ahead_behind(self._main, branch)
            drafts.append(
                DraftInfo(
                    name=branch[len(DRAFT_PREFIX) :],
                    branch=branch,
                    ahead=ahead,
                    behind=behind,
                )
            )
        return drafts

    def delete_draft(self, name: str, force: bool = False) -> None:
        """Delete a draft branch.

        Args:
            name: Draft name.
            force: Delete even if the draft has unpublished changes.

        Raises:
            DraftNotFound: If the draft does not exist.
            GitOperationError: If the draft is unpublished and not forced.
        """
        branch = self._require_draft(name)
        with audit("draft.delete", draft=name, force=force):
            self._ensure_ready()
            if self._repo.current_branch() == branch:
                self._repo.checkout(self._main)
            self._repo.delete_branch(branch, force=force)

    def diff_draft(self, name: str) -> str:
        """Show what a draft changes compared to main.

        Args:
            name: Draft name.

        Returns:
            A unified diff (empty if the draft has no changes).
        """
        return self._repo.diff(self._main, self._require_draft(name))

    # -- reading ---------------------------------------------------------

    def list_configs(
        self, draft: str | None = None, ref: str | None = None
    ) -> list[str]:
        """List configuration files on a draft, a ref, or main.

        Args:
            draft: Read from this draft.
            ref: Read from this branch or commit (ignored if ``draft`` is set).

        Returns:
            Sorted configuration file paths.
        """
        source = self._resolve_ref(draft, ref)
        return [f for f in self._repo.list_files(source) if self._parser.supports(f)]

    def read_text(
        self, name: str, draft: str | None = None, ref: str | None = None
    ) -> str:
        """Return the raw content of a configuration file.

        Args:
            name: Configuration file.
            draft: Read from this draft.
            ref: Read from this branch or commit (ignored if ``draft`` is set).

        Returns:
            The file content.

        Raises:
            ConfigNotFound: If the file does not exist there.
        """
        self.validate_config_name(name)
        source = self._resolve_ref(draft, ref)
        try:
            return self._repo.read_file(name, source)
        except FileNotFoundError:
            raise ConfigNotFound(name, source) from None

    def read_config(
        self, name: str, draft: str | None = None, ref: str | None = None
    ) -> ConfigData:
        """Parse a configuration file.

        Args:
            name: Configuration file.
            draft: Read from this draft.
            ref: Read from this branch or commit (ignored if ``draft`` is set).

        Returns:
            The parsed configuration.

        Raises:
            ConfigNotFound: If the file does not exist there.
            ConfigFormatError: If the stored content is invalid.
        """
        return self._parser.loads(name, self.read_text(name, draft, ref))

    def get_value(
        self, name: str, key: str, draft: str | None = None, ref: str | None = None
    ) -> Any:
        """Read one value from a configuration file.

        Args:
            name: Configuration file.
            key: Dotted key path, e.g. ``"server.port"``.
            draft: Read from this draft.
            ref: Read from this branch or commit (ignored if ``draft`` is set).

        Returns:
            The value.

        Raises:
            KeyNotFound: If the key does not exist.
        """
        data = self.read_config(name, draft, ref)
        try:
            return keypath.get_value(data, key)
        except (KeyError, ValueError):
            raise KeyNotFound(name, key) from None

    def history(
        self,
        name: str,
        draft: str | None = None,
        ref: str | None = None,
        limit: int | None = None,
        include_drafts: bool = False,
    ) -> list[CommitInfo]:
        """List the versions of a configuration file, newest first.

        By default only the commits on the branch itself are listed (each
        publish appears once, as its merge commit). With ``include_drafts``
        the individual commits made inside drafts are listed too.

        Args:
            name: Configuration file.
            draft: Read history from this draft.
            ref: Read history from this branch or commit.
            limit: Maximum number of entries.
            include_drafts: Also list the commits made on merged drafts.

        Returns:
            Commit summaries.

        Raises:
            ConfigNotFound: If the file never existed there.
        """
        self.validate_config_name(name)
        source = self._resolve_ref(draft, ref)
        commits = self._repo.history(
            source, path=name, limit=limit, first_parent=not include_drafts
        )
        if not commits:
            raise ConfigNotFound(name, source)
        return commits

    # -- writing (always on a draft) ---------------------------------------

    def _commit_change(
        self, name: str, branch: str, text: str | None, message: str
    ) -> str | None:
        """Write (or delete, if ``text`` is None) a file on a branch and commit.

        Returns ``None`` without committing when the content is unchanged.
        """
        try:
            current: str | None = self._repo.read_file(name, branch)
        except FileNotFoundError:
            current = None
        if current == text:
            return None
        with self._repo.checked_out(branch):
            if text is None:
                self._repo.remove_file(name)
                return self._repo.commit(message)
            self._repo.write_file(name, text)
            return self._repo.commit(message, [name])

    def _draft_for_write(self, draft: str, create: bool) -> str:
        """Return the draft branch, creating it from main if allowed."""
        self.validate_draft_name(draft)
        branch = self.draft_branch(draft)
        if not self._repo.branch_exists(branch):
            if not create:
                raise DraftNotFound(draft)
            self.create_draft(draft)
        return branch

    def create_config(
        self,
        name: str,
        data: ConfigData,
        draft: str,
        message: str | None = None,
        create_draft: bool = True,
    ) -> str:
        """Add a new configuration file on a draft.

        Args:
            name: New configuration file, e.g. ``"app.json"``.
            data: Initial content.
            draft: Draft to commit to.
            message: Commit message (a default is generated).
            create_draft: Create the draft from main if it does not exist.

        Returns:
            The commit hash.

        Raises:
            ConfigAlreadyExists: If the file already exists on the draft.
            DraftNotFound: If the draft is missing and ``create_draft`` is off.
        """
        self.validate_config_name(name)
        text = self._parser.dumps(name, data)
        with audit("config.create", config=name, draft=draft) as log:
            self._ensure_ready()
            branch = self._draft_for_write(draft, create_draft)
            if name in self._repo.list_files(branch):
                raise ConfigAlreadyExists(name, branch)
            sha = self._commit_change(name, branch, text, message or f"Create {name}")
            assert sha is not None  # a new file always changes the tree
            log.info("committed", commit=sha, branch=branch)
            return sha

    def _update(
        self,
        action: str,
        name: str,
        draft: str,
        message: str,
        change: Any,
        **fields: Any,
    ) -> str | None:
        """Load a config from a draft, apply ``change`` to it, and commit."""
        self.validate_config_name(name)
        with audit(action, config=name, draft=draft, **fields) as log:
            self._ensure_ready()
            branch = self._require_draft(draft)
            data = self.read_config(name, draft=draft)
            text = self._parser.dumps(name, change(data))
            sha = self._commit_change(name, branch, text, message)
            if sha is None:
                log.info("unchanged")
            else:
                log.info("committed", commit=sha, branch=branch)
            return sha

    def replace_config(
        self, name: str, data: ConfigData, draft: str, message: str | None = None
    ) -> str | None:
        """Replace the whole content of an existing configuration on a draft.

        Args:
            name: Configuration file.
            data: New content.
            draft: Draft to commit to.
            message: Commit message (a default is generated).

        Returns:
            The commit hash, or ``None`` if the content did not change.

        Raises:
            ConfigNotFound: If the file does not exist on the draft.
        """
        return self._update(
            "config.replace", name, draft, message or f"Update {name}", lambda _: data
        )

    def set_value(
        self, name: str, key: str, value: Any, draft: str, message: str | None = None
    ) -> str | None:
        """Set one value in a configuration on a draft.

        Args:
            name: Configuration file.
            key: Dotted key path; missing parent mappings are created.
            value: New value.
            draft: Draft to commit to.
            message: Commit message (a default is generated).

        Returns:
            The commit hash, or ``None`` if the value was already set.

        Raises:
            ConfigNotFound: If the file does not exist on the draft.
            KeyNotFound: If the path crosses a non-mapping value.
        """

        def change(data: ConfigData) -> ConfigData:
            try:
                return keypath.set_value(data, key, value)
            except ValueError:
                raise KeyNotFound(name, key) from None

        return self._update(
            "config.set",
            name,
            draft,
            message or f"Set {key} in {name}",
            change,
            key=key,
            value=value,
        )

    def unset_value(
        self, name: str, key: str, draft: str, message: str | None = None
    ) -> str | None:
        """Remove one key from a configuration on a draft.

        Args:
            name: Configuration file.
            key: Dotted key path.
            draft: Draft to commit to.
            message: Commit message (a default is generated).

        Returns:
            The commit hash.

        Raises:
            KeyNotFound: If the key does not exist.
        """

        def change(data: ConfigData) -> ConfigData:
            try:
                return keypath.delete_value(data, key)
            except (KeyError, ValueError):
                raise KeyNotFound(name, key) from None

        return self._update(
            "config.unset",
            name,
            draft,
            message or f"Unset {key} in {name}",
            change,
            key=key,
        )

    def delete_config(self, name: str, draft: str, message: str | None = None) -> str:
        """Delete a configuration file on a draft.

        Args:
            name: Configuration file.
            draft: Draft to commit to.
            message: Commit message (a default is generated).

        Returns:
            The commit hash.

        Raises:
            ConfigNotFound: If the file does not exist on the draft.
        """
        self.validate_config_name(name)
        with audit("config.delete", config=name, draft=draft) as log:
            self._ensure_ready()
            branch = self._require_draft(draft)
            if name not in self._repo.list_files(branch):
                raise ConfigNotFound(name, branch)
            sha = self._commit_change(name, branch, None, message or f"Delete {name}")
            assert sha is not None  # removing an existing file always changes the tree
            log.info("committed", commit=sha, branch=branch)
            return sha

    def rollback(
        self,
        name: str,
        *,
        to: str | None = None,
        steps: int | None = None,
        draft: str | None = None,
        message: str | None = None,
    ) -> str | None:
        """Restore an earlier version of a configuration as a new commit.

        History is never rewritten: the old content is committed on top, so
        the rollback itself can be inspected or rolled back later.

        Args:
            name: Configuration file.
            to: Commit (or branch) whose version of the file to restore.
            steps: Alternatively, how many versions to go back on the
                branch (``1`` = the version before the current one). Each
                publish counts as one version.
            draft: Roll back on this draft instead of directly on main.
            message: Commit message (a default is generated).

        Returns:
            The commit hash, or ``None`` if the file already had that content.

        Raises:
            ValueError: If neither or both of ``to`` and ``steps`` are given.
            RevisionNotFound: If the requested version does not exist.
        """
        if (to is None) == (steps is None):
            raise ValueError("pass exactly one of 'to' or 'steps'")
        self.validate_config_name(name)
        branch = self._require_draft(draft) if draft else self._main
        with audit(
            "config.rollback", config=name, branch=branch, to=to, steps=steps
        ) as log:
            self._ensure_ready()
            if steps is not None:
                versions = self._repo.history(branch, path=name, first_parent=True)
                if steps < 1 or steps >= len(versions):
                    raise RevisionNotFound(name, f"{steps} version(s) back")
                commit = versions[steps].sha
            else:
                assert to is not None
                try:
                    commit = self._repo.resolve_commit(to)
                except GitOperationError:
                    raise RevisionNotFound(name, to) from None
            try:
                text = self._repo.read_file(name, commit)
            except FileNotFoundError:
                raise RevisionNotFound(name, commit[:7]) from None
            self._parser.loads(name, text)
            sha = self._commit_change(
                name, branch, text, message or f"Rollback {name} to {commit[:7]}"
            )
            log.info("committed" if sha else "unchanged", commit=sha, source=commit)
            return sha

    # -- merging ---------------------------------------------------------

    @staticmethod
    def _git_preference(prefer: Side | None, incoming: Side) -> MergePreference | None:
        """Translate 'draft'/'main' into Git's 'ours'/'theirs'.

        ``incoming`` is the side being merged in (Git's "theirs").
        """
        if prefer is None:
            return None
        return "theirs" if prefer == incoming else "ours"

    def _validate_merged(self, files: Iterable[str], ref: str) -> None:
        """Parse every merged configuration file to make sure it is valid."""
        for name in files:
            if not self._parser.supports(name):
                continue
            try:
                text = self._repo.read_file(name, ref)
            except FileNotFoundError:
                continue  # the merge deleted it
            self._parser.loads(name, text)

    def _merge(
        self,
        source: str,
        message: str,
        *,
        prefer: MergePreference | None,
        squash: bool = False,
        keep_conflicts: bool = False,
    ) -> MergeResult:
        """Merge ``source`` into the checked-out branch, resolving what it can.

        Git's textual conflicts in configuration files are re-merged key by
        key; if no key really conflicts the merge is completed automatically.
        Otherwise the merge is aborted (or, with ``keep_conflicts``, the
        resolvable files are written and the rest left for the user).

        ``prefer`` is applied by the key-level resolver, not passed to Git as
        ``-X ours/theirs``: Git applies it per *hunk*, which would silently
        drop the other side's change to a neighbouring key.

        Raises:
            MergeConflictError: With key-level details of what conflicts.
            ConfigFormatError: If the merged result is not valid JSON/YAML;
                the branch is reset to its previous commit.
        """
        target = self._repo.current_branch()
        before = self._repo.resolve_commit(target)
        try:
            try:
                result = self._repo.merge(
                    source, message, squash=squash, abort_on_conflict=False
                )
            except MergeConflictError as exc:
                result = self._resolve_textual_conflicts(
                    exc, message, prefer, before, keep_conflicts
                )
            self._validate_merged(result.files, target)
        except ConfigFormatError as exc:
            self._repo.reset_hard(before)
            raise ConfigFormatError(
                exc.name, f"merging would produce invalid content ({exc.reason})"
            ) from exc
        return result

    def _resolve_textual_conflicts(
        self,
        conflict: MergeConflictError,
        message: str,
        prefer: MergePreference | None,
        before: str,
        keep_conflicts: bool,
    ) -> MergeResult:
        """Handle a merge that Git stopped because of conflicts."""
        resolution = self._resolver.analyze(
            conflict.conflicted_files, before, conflict.source, prefer
        )
        if resolution.complete:
            paths = self._resolver.apply(resolution)
            sha = self._repo.commit(message, paths)
            auto = sorted(resolution.resolved)
            log.info("conflicts.auto_resolved", files=auto, commit=sha)
            return MergeResult(
                commit=sha,
                files=self._repo.changed_files(before, sha),
                auto_resolved=auto,
            )

        unresolved = [c.path for c in resolution.unresolved]
        if keep_conflicts:
            written = self._resolver.apply(resolution)
            if written:
                # stage what we resolved so only real conflicts remain
                self._repo.stage(written)
        elif self._repo.is_merging():
            self._repo.abort_merge()
        else:  # a squash merge has no MERGE_HEAD to abort
            self._repo.reset_hard(before)
        log.info("conflicts.unresolved", files=unresolved, kept=keep_conflicts)
        raise MergeConflictError(
            conflict.source,
            conflict.target,
            unresolved,
            details=resolution.unresolved,
            in_progress=keep_conflicts,
        ) from None

    # -- publishing ------------------------------------------------------

    def publish(
        self,
        draft: str,
        *,
        prefer: Side | None = None,
        squash: bool = False,
        keep_draft: bool = False,
        message: str | None = None,
    ) -> PublishResult:
        """Merge a draft into main.

        Conflicts that only *look* like conflicts to Git (different keys
        edited on neighbouring lines) are resolved automatically. Merged
        files are parsed before the publish is accepted: if the merge would
        produce invalid JSON/YAML, main is reset to its previous commit.

        Args:
            draft: Draft to publish.
            prefer: Resolve conflicting changes in favour of ``"draft"`` or
                ``"main"`` instead of failing.
            squash: Publish all draft commits as a single commit.
            keep_draft: Keep the draft branch after publishing.
            message: Commit message (a default is generated).

        Returns:
            The publish outcome.

        Raises:
            DraftNotFound: If the draft does not exist.
            MergeConflictError: If draft and main changed the same keys and
                no preference was given. Main is left unchanged and the
                error lists every conflicting key.
            ConfigFormatError: If the merge would produce an invalid file.
        """
        branch = self._require_draft(draft)
        text = message or f"Publish draft '{draft}'"
        with audit("draft.publish", draft=draft, prefer=prefer, squash=squash) as log:
            self._ensure_ready()
            with self._repo.checked_out(self._main):
                result = self._merge(
                    branch,
                    text,
                    prefer=self._git_preference(prefer, incoming="draft"),
                    squash=squash,
                )

            deleted = False
            if not keep_draft and not result.up_to_date:
                if self._repo.current_branch() == branch:
                    self._repo.checkout(self._main)
                self._repo.delete_branch(branch, force=squash)
                deleted = True
            log.info(
                "published",
                commit=result.commit,
                files=result.files,
                up_to_date=result.up_to_date,
                auto_resolved=result.auto_resolved,
                draft_deleted=deleted,
            )
            return PublishResult(
                draft=draft,
                target=self._main,
                commit=result.commit,
                files=[f for f in result.files if self._parser.supports(f)],
                up_to_date=result.up_to_date,
                draft_deleted=deleted,
                auto_resolved=result.auto_resolved,
            )

    def preview_publish(
        self, draft: str, *, prefer: Side | None = None, squash: bool = False
    ) -> PublishPreview:
        """Show what :meth:`publish` would do, without changing anything.

        The publish is performed for real inside a temporary clone, which
        is deleted afterwards; the working repository is never touched.

        Args:
            draft: Draft to check.
            prefer: Same as for :meth:`publish`.
            squash: Same as for :meth:`publish`.

        Returns:
            Files that would change and any conflicts or problems.
        """
        self._require_draft(draft)
        with audit("draft.preview", draft=draft, prefer=prefer, squash=squash) as log:
            self._ensure_no_merge()
            with self._repo.temporary_clone() as clone, bound_context(dry_run=True):
                if clone.current_branch() != self._main:
                    clone.checkout(self._main)
                trial = ConfigManager(clone, self._parser, self._main)
                try:
                    result = trial.publish(
                        draft, prefer=prefer, squash=squash, keep_draft=True
                    )
                except MergeConflictError as exc:
                    preview = PublishPreview(draft=draft, conflicts=exc.details)
                except ConfigFormatError as exc:
                    preview = PublishPreview(draft=draft, problems=[str(exc)])
                else:
                    preview = PublishPreview(
                        draft=draft,
                        files=result.files,
                        auto_resolved=result.auto_resolved,
                        up_to_date=result.up_to_date,
                    )
            log.info(
                "previewed",
                can_publish=preview.can_publish,
                files=preview.files,
                conflicts=[c.path for c in preview.conflicts],
            )
            return preview

    # -- conflict resolution workflow -------------------------------------

    def sync_draft(self, draft: str, *, prefer: Side | None = None) -> MergeResult:
        """Bring the latest main into a draft, so it can be published cleanly.

        Compatible changes are merged (key by key if needed). If real
        conflicts remain, the draft is left checked out with the merge in
        progress: edit the conflicted files, then call
        :meth:`resolve_conflicts` (or :meth:`abort_merge`).

        Args:
            draft: Draft to update.
            prefer: Resolve conflicting keys in favour of ``"draft"`` or
                ``"main"`` instead of stopping.

        Returns:
            The merge outcome (``up_to_date`` if main had nothing new).

        Raises:
            MergeConflictError: With ``in_progress=True`` if manual
                resolution is needed.
        """
        branch = self._require_draft(draft)
        with audit("draft.sync", draft=draft, prefer=prefer) as log:
            self._ensure_ready()
            original = self._repo.current_branch()
            self._repo.checkout(branch)
            try:
                result = self._merge(
                    self._main,
                    f"Sync draft '{draft}' with {self._main}",
                    prefer=self._git_preference(prefer, incoming="main"),
                    keep_conflicts=True,
                )
            except MergeConflictError:
                raise  # stay on the draft so the user can resolve
            except BaseException:
                self._repo.checkout(original)
                raise
            self._repo.checkout(original)
            log.info(
                "synced",
                commit=result.commit,
                up_to_date=result.up_to_date,
                auto_resolved=result.auto_resolved,
            )
            return result

    def _require_merge(self) -> str:
        """Return the current branch, failing if no merge is in progress."""
        if not self._repo.is_merging():
            raise NoMergeInProgress()
        return self._repo.current_branch()

    def resolve_conflicts(self, message: str | None = None) -> str:
        """Commit a merge after the conflicted files were fixed by hand.

        Every previously conflicted file must be free of conflict markers
        and, for configuration files, parse correctly.

        Args:
            message: Commit message (a default is generated).

        Returns:
            The merge commit hash.

        Raises:
            NoMergeInProgress: If there is nothing to resolve.
            ConfigFormatError: If a file still has markers or is invalid.
        """
        branch = self._require_merge()
        with audit("merge.resolve", branch=branch) as log:
            to_stage: list[str] = []
            for path in self._repo.conflicted_files():
                try:
                    text = self._repo.read_working_file(path)
                except FileNotFoundError:
                    self._repo.remove_file(path)
                    continue
                if has_conflict_markers(text):
                    raise ConfigFormatError(
                        path,
                        "still contains conflict markers (<<<<<<<, =======, >>>>>>>)",
                    )
                if self._parser.supports(path):
                    self._parser.loads(path, text)
                to_stage.append(path)
            sha = self._repo.commit(
                message or f"Merge {self._main} into {branch} (conflicts resolved)",
                to_stage,
            )
            log.info("committed", commit=sha, files=to_stage)
            return sha

    def abort_merge(self) -> str:
        """Abandon a merge in progress and restore the branch.

        Returns:
            The branch on which the merge was aborted.

        Raises:
            NoMergeInProgress: If there is nothing to abort.
        """
        branch = self._require_merge()
        with audit("merge.abort", branch=branch):
            self._repo.abort_merge()
            return branch

    # -- remotes ---------------------------------------------------------

    def push(self, draft: str | None = None, remote: str = "origin") -> str:
        """Push main (or a draft) to a remote.

        Args:
            draft: Push this draft instead of main.
            remote: Remote name.

        Returns:
            The branch that was pushed.

        Raises:
            GitOperationError: If the push fails.
        """
        branch = self._require_draft(draft) if draft else self._main
        with audit("remote.push", branch=branch, remote=remote):
            self._repo.push(branch, remote)
            return branch

    def pull(self, remote: str = "origin") -> None:
        """Fast-forward main from a remote.

        Args:
            remote: Remote name.

        Raises:
            GitOperationError: If the pull fails or is not a fast-forward.
        """
        with audit("remote.pull", branch=self._main, remote=remote):
            self._ensure_ready()
            self._repo.pull(self._main, remote)
