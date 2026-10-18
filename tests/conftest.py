"""Shared pytest fixtures for gitconfig-cli.

Every test runs with an isolated Git environment: a throw-away global
config with a fixed identity, and no system config. Tests therefore never
read or modify the developer's real Git settings, and they behave the same
on every machine and in CI.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import git
import pytest

from gitconfig_cli.config_parser import ConfigParser
from gitconfig_cli.git_repository import GitRepository
from gitconfig_cli.logging_config import clear_context, configure_logging
from gitconfig_cli.manager import ConfigManager

TEST_NAME = "Test User"
TEST_EMAIL = "test@example.com"


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Give each test a quiet logging setup and close any audit file after it."""
    configure_logging(log_file=None)
    yield
    clear_context()
    configure_logging(log_file=None)


@pytest.fixture(autouse=True)
def _isolated_git_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point Git at a private global config with a known identity."""
    home = tmp_path_factory.mktemp("git-home")
    global_config = home / ".gitconfig"
    global_config.write_text(
        "[user]\n"
        f"\tname = {TEST_NAME}\n"
        f"\temail = {TEST_EMAIL}\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
        "[core]\n"
        "\tautocrlf = false\n"
        "[commit]\n"
        "\tgpgsign = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)


@pytest.fixture
def empty_repo_path(tmp_path: Path) -> Iterator[Path]:
    """Create a brand-new, empty Git repository (no commits) for one test."""
    path = tmp_path / "empty"
    repo = git.Repo.init(path, initial_branch="main")
    yield path
    repo.close()


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[GitRepository]:
    """A GitRepository on ``main`` with one initial commit (a README)."""
    repository = GitRepository.init(tmp_path / "configs")
    repository.write_file("README.md", "# configs\n")
    repository.commit("Initial commit", ["README.md"])
    yield repository
    repository.close()


CommitFile = Callable[..., str]


@pytest.fixture
def commit_file(repo: GitRepository) -> CommitFile:
    """Write a file on a branch and commit it; returns the commit hash."""

    def _commit_file(
        path: str, content: str, branch: str = "main", message: str | None = None
    ) -> str:
        with repo.checked_out(branch):
            repo.write_file(path, content)
            return repo.commit(message or f"Update {path}", [path])

    return _commit_file


@pytest.fixture
def config_repo(tmp_path: Path) -> Iterator[GitRepository]:
    """An initialized configuration repository (README + managed .gitignore)."""
    repository = GitRepository.init(tmp_path / "config-repo")
    ConfigManager(repository, ConfigParser()).initialize()
    yield repository
    repository.close()


@pytest.fixture
def manager(config_repo: GitRepository) -> ConfigManager:
    """A ConfigManager wired to a real, initialized temporary repository."""
    return ConfigManager(config_repo, ConfigParser())
