"""Dotted key paths for reading and editing nested configuration values.

A key path such as ``"database.replicas.0.host"`` walks through nested
mappings by key and through lists by numeric index. All editing functions
return a modified *copy* and never mutate their input.
"""

import copy
from typing import Any

from gitconfig_cli.abstractions import ConfigData


def split_key(key: str) -> list[str]:
    """Split a dotted key path into its segments.

    Args:
        key: Dotted path, e.g. ``"server.port"``.

    Returns:
        The non-empty segments, e.g. ``["server", "port"]``.

    Raises:
        ValueError: If the path is empty or contains an empty segment.
    """
    parts = key.split(".")
    if not key or any(part == "" for part in parts):
        raise ValueError(f"invalid key path '{key}'")
    return parts


def _child(node: Any, part: str) -> Any:
    """Return one level down from ``node``, or raise ``KeyError``."""
    if isinstance(node, dict):
        if part in node:
            return node[part]
    elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
        return node[int(part)]
    raise KeyError(part)


def get_value(data: ConfigData, key: str) -> Any:
    """Read a nested value.

    Args:
        data: The configuration mapping.
        key: Dotted key path.

    Returns:
        The value at the path.

    Raises:
        KeyError: If any segment of the path does not exist.
        ValueError: If the key path is malformed.
    """
    node: Any = data
    for part in split_key(key):
        node = _child(node, part)
    return node


def set_value(data: ConfigData, key: str, value: Any) -> ConfigData:
    """Return a copy of ``data`` with a nested value set.

    Missing intermediate mappings are created. List elements can be
    replaced by index but lists are never extended implicitly.

    Args:
        data: The configuration mapping.
        key: Dotted key path.
        value: The value to store.

    Returns:
        A new mapping containing the change.

    Raises:
        ValueError: If the path is malformed or crosses a scalar value or
            an out-of-range list index.
    """
    result = copy.deepcopy(data)
    *parents, last = split_key(key)
    node: Any = result
    for part in parents:
        if isinstance(node, dict):
            node = node.setdefault(part, {})
        else:
            try:
                node = _child(node, part)
            except KeyError:
                raise ValueError(f"cannot set '{key}': '{part}' is not valid") from None
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = value
    else:
        raise ValueError(f"cannot set '{key}': parent is not a mapping")
    return result


def delete_value(data: ConfigData, key: str) -> ConfigData:
    """Return a copy of ``data`` with a nested value removed.

    Args:
        data: The configuration mapping.
        key: Dotted key path.

    Returns:
        A new mapping without the key.

    Raises:
        KeyError: If the key does not exist.
        ValueError: If the key path is malformed.
    """
    result = copy.deepcopy(data)
    *parents, last = split_key(key)
    node: Any = result
    for part in parents:
        node = _child(node, part)
    _child(node, last)  # raises KeyError when missing
    if isinstance(node, dict):
        del node[last]
    else:
        del node[int(last)]
    return result
