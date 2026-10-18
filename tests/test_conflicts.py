from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from gitconfig_cli.abstractions import VersionControl
from gitconfig_cli.config_parser import ConfigParser
from gitconfig_cli.conflicts import ConflictResolver, Resolution, has_conflict_markers
from gitconfig_cli.models import FileConflict


@pytest.fixture
def vcs(mocker: MockerFixture) -> MagicMock:
    return mocker.create_autospec(VersionControl, instance=True)


@pytest.fixture
def resolver(vcs: MagicMock) -> ConflictResolver:
    return ConflictResolver(vcs, ConfigParser())


def _files(contents: dict[tuple[str, str], str]) -> object:
    def read_file(path: str, ref: str) -> str:
        try:
            return contents[(path, ref)]
        except KeyError:
            raise FileNotFoundError(path) from None

    return read_file


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n", True),
        ("a\n=======\n", True),
        ('{"a": "======="}\n', False),
        ("plain\n", False),
    ],
)
def test_has_conflict_markers(text: str, expected: bool) -> None:
    assert has_conflict_markers(text) is expected


def test_unrelated_histories_have_no_base(
    resolver: ConflictResolver, vcs: MagicMock
) -> None:
    vcs.merge_base.return_value = None
    vcs.read_file.side_effect = _files(
        {("a.json", "ours"): '{"x": 1}', ("a.json", "theirs"): '{"y": 2}'}
    )

    resolution = resolver.analyze(["a.json"], "ours", "theirs")

    assert resolution.complete
    assert resolution.resolved == {"a.json": '{\n  "x": 1,\n  "y": 2\n}\n'}


@pytest.mark.parametrize(("prefer", "expected"), [("ours", "mine\n"), ("theirs", None)])
def test_non_config_file_follows_preference(
    resolver: ConflictResolver, vcs: MagicMock, prefer: str, expected: str | None
) -> None:
    vcs.merge_base.return_value = "base"
    vcs.read_file.side_effect = _files(
        {("NOTES.md", "base"): "old\n", ("NOTES.md", "ours"): "mine\n"}
    )

    resolution = resolver.analyze(["NOTES.md"], "ours", "theirs", prefer)  # type: ignore[arg-type]

    assert resolution.resolved == {"NOTES.md": expected}


def test_apply_writes_and_removes(resolver: ConflictResolver, vcs: MagicMock) -> None:
    resolution = Resolution(resolved={"a.json": "{}\n", "gone.json": None})

    assert resolver.apply(resolution) == ["a.json"]

    vcs.write_file.assert_called_once_with("a.json", "{}\n")
    vcs.remove_file.assert_called_once_with("gone.json")


def test_resolution_complete_flag() -> None:
    assert Resolution().complete
    assert not Resolution(unresolved=[FileConflict("a.json")]).complete
