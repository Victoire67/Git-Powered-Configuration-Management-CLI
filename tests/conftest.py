"""Shared pytest fixtures for gitconfig-cli."""

from collections.abc import Iterator

import pytest

from gitconfig_cli.logging_config import clear_context, configure_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Give each test a quiet logging setup and close any audit file after it."""
    configure_logging(log_file=None)
    yield
    clear_context()
    configure_logging(log_file=None)
