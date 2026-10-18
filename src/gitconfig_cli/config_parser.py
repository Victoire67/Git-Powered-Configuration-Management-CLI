"""Reading and writing JSON / YAML configuration documents.

This module knows nothing about Git: it only converts between text and
Python dictionaries. Each file format is a small :class:`ConfigFormat`
strategy, and :class:`ConfigParser` picks the right one from the file
extension. Supporting a new format (e.g. TOML) means adding a new
strategy, without modifying existing code (Open/Closed Principle).
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from gitconfig_cli.abstractions import ConfigData, ConfigSerializer
from gitconfig_cli.exceptions import ConfigFormatError, ConfigNotFound


class ConfigFormat(ABC):
    """Strategy for one configuration file format."""

    name: str
    extensions: tuple[str, ...]

    @abstractmethod
    def loads(self, text: str) -> Any:
        """Parse text into Python data.

        Args:
            text: Raw file content.

        Returns:
            The parsed value (validated as a mapping by the caller).

        Raises:
            ValueError: If the text is not valid in this format.
        """

    @abstractmethod
    def dumps(self, data: ConfigData) -> str:
        """Serialize a mapping to text.

        Args:
            data: The configuration mapping.

        Returns:
            The serialized text.

        Raises:
            ValueError: If the data cannot be represented in this format.
        """


class JsonFormat(ConfigFormat):
    """JSON format, written with 2-space indentation for readable diffs."""

    name = "json"
    extensions = (".json",)

    def loads(self, text: str) -> Any:
        """Parse JSON text.

        Args:
            text: Raw JSON content.

        Returns:
            The decoded value.

        Raises:
            ValueError: If the JSON is malformed.
        """
        return json.loads(text)

    def dumps(self, data: ConfigData) -> str:
        """Serialize to pretty-printed JSON.

        Args:
            data: The configuration mapping.

        Returns:
            Indented JSON ending with a newline.

        Raises:
            ValueError: If a value is not JSON serializable.
        """
        try:
            return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        except TypeError as exc:
            raise ValueError(str(exc)) from exc


class YamlFormat(ConfigFormat):
    """YAML format, using the safe loader and block style."""

    name = "yaml"
    extensions = (".yaml", ".yml")

    def loads(self, text: str) -> Any:
        """Parse YAML text safely (no arbitrary object construction).

        Args:
            text: Raw YAML content.

        Returns:
            The decoded value; an empty document becomes ``{}``.

        Raises:
            ValueError: If the YAML is malformed.
        """
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(str(exc)) from exc
        return {} if value is None else value

    def dumps(self, data: ConfigData) -> str:
        """Serialize to block-style YAML, preserving key order.

        Args:
            data: The configuration mapping.

        Returns:
            YAML text ending with a newline.

        Raises:
            ValueError: If a value cannot be represented in YAML.
        """
        try:
            return yaml.safe_dump(
                data, sort_keys=False, allow_unicode=True, default_flow_style=False
            )
        except yaml.YAMLError as exc:
            raise ValueError(str(exc)) from exc


class ConfigParser(ConfigSerializer):
    """Reads and writes configuration files, choosing the format by extension.

    Example:
        >>> parser = ConfigParser()
        >>> parser.loads("app.json", '{"port": 8080}')
        {'port': 8080}
        >>> print(parser.dumps("app.yaml", {"port": 8080}), end="")
        port: 8080
    """

    def __init__(self, formats: Sequence[ConfigFormat] | None = None) -> None:
        """Initialize the parser.

        Args:
            formats: Format strategies to support. Defaults to JSON and YAML.
        """
        chosen = formats if formats is not None else (JsonFormat(), YamlFormat())
        self._by_extension: dict[str, ConfigFormat] = {
            ext: fmt for fmt in chosen for ext in fmt.extensions
        }

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        """File extensions (with leading dot) this parser understands."""
        return tuple(self._by_extension)

    def supports(self, name: str) -> bool:
        """Tell whether a file name has a supported configuration extension.

        Args:
            name: File name or relative path, e.g. ``"app.json"``.

        Returns:
            ``True`` if the extension maps to a known format.
        """
        return PurePosixPath(name).suffix.lower() in self._by_extension

    def format_for(self, name: str) -> ConfigFormat:
        """Return the format strategy for a file name.

        Args:
            name: File name or relative path.

        Returns:
            The matching :class:`ConfigFormat`.

        Raises:
            ConfigFormatError: If the extension is not supported.
        """
        suffix = PurePosixPath(name).suffix.lower()
        try:
            return self._by_extension[suffix]
        except KeyError:
            supported = ", ".join(self.supported_extensions)
            raise ConfigFormatError(
                name, f"unsupported file type '{suffix or '(none)'}'; use {supported}"
            ) from None

    def loads(self, name: str, text: str) -> ConfigData:
        """Parse configuration text.

        Args:
            name: File name; its extension selects the format.
            text: Raw file content.

        Returns:
            The parsed configuration mapping.

        Raises:
            ConfigFormatError: If the format is unsupported, the text is
                malformed, or the top-level value is not a mapping.
        """
        fmt = self.format_for(name)
        try:
            value = fmt.loads(text)
        except ValueError as exc:
            raise ConfigFormatError(name, f"malformed {fmt.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigFormatError(
                name, f"top-level value must be a mapping, got {type(value).__name__}"
            )
        return value

    def dumps(self, name: str, data: ConfigData) -> str:
        """Serialize configuration data to text.

        Args:
            name: File name; its extension selects the format.
            data: The configuration mapping to serialize.

        Returns:
            The file content, ending with a newline.

        Raises:
            ConfigFormatError: If the format is unsupported, the data is not
                a mapping, or a value cannot be represented in the format.
        """
        fmt = self.format_for(name)
        if not isinstance(data, dict):
            raise ConfigFormatError(
                name, f"top-level value must be a mapping, got {type(data).__name__}"
            )
        try:
            return fmt.dumps(data)
        except ValueError as exc:
            raise ConfigFormatError(name, f"cannot write {fmt.name}: {exc}") from exc

    def load_file(self, path: Path) -> ConfigData:
        """Read and parse a configuration file from disk.

        Args:
            path: Path of the file; its extension selects the format.

        Returns:
            The parsed configuration mapping.

        Raises:
            ConfigNotFound: If the file does not exist.
            ConfigFormatError: If the file cannot be read or parsed.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigNotFound(str(path)) from None
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigFormatError(str(path), f"cannot read file: {exc}") from exc
        return self.loads(path.name, text)


def parse_value(raw: str) -> Any:
    """Convert a command-line string into a typed configuration value.

    The string is decoded as JSON when possible, so ``8080`` becomes an int,
    ``true`` a bool, ``null`` ``None`` and ``[1, 2]`` a list. Anything that is
    not valid JSON is kept as a plain string.

    Args:
        raw: The value as typed by the user.

    Returns:
        The decoded value.
    """
    try:
        return json.loads(raw)
    except ValueError:
        return raw
