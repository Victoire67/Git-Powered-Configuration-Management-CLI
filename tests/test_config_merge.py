from typing import Any

import pytest

from gitconfig_cli.config_merge import three_way_merge
from gitconfig_cli.models import ABSENT, ROOT_KEY, KeyConflict


def test_identical_sides() -> None:
    assert three_way_merge({"a": 1}, {"a": 2}, {"a": 2}) == ({"a": 2}, [])


def test_only_theirs_changed() -> None:
    assert three_way_merge({"a": 1}, {"a": 1}, {"a": 2}) == ({"a": 2}, [])


def test_only_ours_changed() -> None:
    assert three_way_merge({"a": 1}, {"a": 2}, {"a": 1}) == ({"a": 2}, [])


def test_different_keys_changed_on_each_side() -> None:
    base = {"server": {"port": 80, "host": "a"}, "debug": False}
    ours = {"server": {"port": 8080, "host": "a"}, "debug": False}
    theirs = {"server": {"port": 80, "host": "b"}, "debug": True}

    merged, conflicts = three_way_merge(base, ours, theirs)

    assert conflicts == []
    assert merged == {"server": {"port": 8080, "host": "b"}, "debug": True}


def test_keys_added_on_both_sides_keep_order() -> None:
    merged, conflicts = three_way_merge({"a": 1}, {"a": 1, "b": 2}, {"a": 1, "c": 3})

    assert conflicts == []
    assert list(merged) == ["a", "b", "c"]


def test_deletion_on_one_side() -> None:
    merged, conflicts = three_way_merge({"a": 1, "b": 2}, {"a": 1, "b": 2}, {"a": 1})

    assert (merged, conflicts) == ({"a": 1}, [])


def test_same_key_changed_differently_conflicts() -> None:
    merged, conflicts = three_way_merge(
        {"db": {"port": 1}}, {"db": {"port": 2}}, {"db": {"port": 3}}
    )

    assert conflicts == [KeyConflict("db.port", 1, 2, 3)]
    assert merged == {"db": {"port": 2}}


def test_delete_versus_modify_conflicts() -> None:
    _, conflicts = three_way_merge({"a": 1}, {}, {"a": 2})

    assert conflicts == [KeyConflict("a", 1, ABSENT, 2)]


def test_added_on_both_sides_with_different_values() -> None:
    _, conflicts = three_way_merge({}, {"new": "x"}, {"new": "y"})

    assert conflicts == [KeyConflict("new", ABSENT, "x", "y")]


def test_lists_are_merged_as_whole_values() -> None:
    _, conflicts = three_way_merge({"l": [1]}, {"l": [1, 2]}, {"l": [1, 3]})

    assert conflicts == [KeyConflict("l", [1], [1, 2], [1, 3])]


def test_whole_file_conflict_uses_root_key() -> None:
    _, conflicts = three_way_merge({"a": 1}, ABSENT, {"a": 2})

    assert conflicts[0].key == ROOT_KEY


def test_both_deleted_file() -> None:
    assert three_way_merge({"a": 1}, ABSENT, ABSENT) == (ABSENT, [])


def test_file_added_on_both_sides_is_merged() -> None:
    merged, conflicts = three_way_merge(ABSENT, {"a": 1}, {"b": 2})

    assert (merged, conflicts) == ({"a": 1, "b": 2}, [])


@pytest.mark.parametrize(("prefer", "expected"), [("ours", 2), ("theirs", 3)])
def test_preference_resolves_conflicts(prefer: Any, expected: int) -> None:
    merged, conflicts = three_way_merge({"a": 1}, {"a": 2}, {"a": 3}, prefer)

    assert (merged, conflicts) == ({"a": expected}, [])


def test_preference_can_choose_deletion() -> None:
    merged, _ = three_way_merge({"a": 1, "b": 1}, {"b": 1}, {"a": 2, "b": 1}, "ours")

    assert merged == {"b": 1}


def test_absent_repr_and_null_are_distinct() -> None:
    assert repr(ABSENT) == "<absent>"
    assert ABSENT is not None
    _, conflicts = three_way_merge({"a": 1}, {"a": None}, {})

    assert conflicts == [KeyConflict("a", 1, None, ABSENT)]
