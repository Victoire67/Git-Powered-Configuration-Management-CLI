from typing import Any

import pytest

from gitconfig_cli.keypath import delete_value, get_value, set_value, split_key


@pytest.fixture
def data() -> dict[str, Any]:
    return {
        "server": {"port": 8080, "hosts": ["a", "b"]},
        "replicas": [{"host": "db1"}, {"host": "db2"}],
        "debug": False,
    }


def test_split_key() -> None:
    assert split_key("server.port") == ["server", "port"]


@pytest.mark.parametrize("key", ["", ".", "server.", ".port", "a..b"])
def test_split_key_rejects_empty_segments(key: str) -> None:
    with pytest.raises(ValueError, match="invalid key path"):
        split_key(key)


def test_get_top_level(data: dict[str, Any]) -> None:
    assert get_value(data, "debug") is False


def test_get_nested(data: dict[str, Any]) -> None:
    assert get_value(data, "server.port") == 8080


def test_get_list_index(data: dict[str, Any]) -> None:
    assert get_value(data, "replicas.1.host") == "db2"


@pytest.mark.parametrize(
    "key", ["missing", "server.missing", "replicas.5", "replicas.x", "debug.deeper"]
)
def test_get_missing_raises_key_error(data: dict[str, Any], key: str) -> None:
    with pytest.raises(KeyError):
        get_value(data, key)


def test_set_existing_returns_copy(data: dict[str, Any]) -> None:
    result = set_value(data, "server.port", 9090)

    assert result["server"]["port"] == 9090
    assert data["server"]["port"] == 8080


def test_set_creates_intermediate_mappings(data: dict[str, Any]) -> None:
    result = set_value(data, "cache.redis.ttl", 60)

    assert result["cache"] == {"redis": {"ttl": 60}}


def test_set_list_element(data: dict[str, Any]) -> None:
    result = set_value(data, "replicas.0.host", "db0")

    assert result["replicas"][0]["host"] == "db0"


def test_set_list_item_by_index(data: dict[str, Any]) -> None:
    result = set_value(data, "server.hosts.1", "c")

    assert result["server"]["hosts"] == ["a", "c"]


def test_set_through_scalar_fails(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="parent is not a mapping"):
        set_value(data, "debug.level", 1)


def test_set_through_bad_list_index_fails(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="'9' is not valid"):
        set_value(data, "replicas.9.host", "x")


def test_set_out_of_range_list_index_fails(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="parent is not a mapping"):
        set_value(data, "server.hosts.5", "x")


def test_delete_key_returns_copy(data: dict[str, Any]) -> None:
    result = delete_value(data, "server.port")

    assert "port" not in result["server"]
    assert data["server"]["port"] == 8080


def test_delete_list_item(data: dict[str, Any]) -> None:
    result = delete_value(data, "server.hosts.0")

    assert result["server"]["hosts"] == ["b"]


def test_delete_missing_raises_key_error(data: dict[str, Any]) -> None:
    with pytest.raises(KeyError):
        delete_value(data, "server.missing")
