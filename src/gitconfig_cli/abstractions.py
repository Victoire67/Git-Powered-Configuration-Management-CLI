"""Abstract interfaces that the application logic depends on.

Following the Dependency Inversion Principle, :class:`~gitconfig_cli.manager.
ConfigManager` (and therefore the CLI) only knows about these interfaces.
The concrete implementations live in :mod:`gitconfig_cli.config_parser`
and :mod:`gitconfig_cli.git_repository`, and tests can substitute fakes or
mocks without touching Git or the file system.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

ConfigData = dict[str, Any]
"""A parsed configuration document: always a mapping at the top level."""


class ConfigSerializer(ABC):
    """Converts configuration documents between text and Python data."""

    @property
    @abstractmethod
    def supported_extensions(self) -> tuple[str, ...]:
        """File extensions (with leading dot) this serializer understands."""

    @abstractmethod
    def supports(self, name: str) -> bool:
        """Tell whether a file name has a supported configuration extension.

        Args:
            name: File name or relative path, e.g. ``"app.json"``.

        Returns:
            ``True`` if :meth:`loads` / :meth:`dumps` can handle the file.
        """

    @abstractmethod
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

    @abstractmethod
    def dumps(self, name: str, data: ConfigData) -> str:
        """Serialize configuration data to text.

        Args:
            name: File name; its extension selects the format.
            data: The configuration mapping to serialize.

        Returns:
            The file content, ending with a newline.

        Raises:
            ConfigFormatError: If the format is unsupported or the data
                cannot be represented in it.
        """

    @abstractmethod
    def load_file(self, path: Path) -> ConfigData:
        """Read and parse a configuration file from disk.

        Args:
            path: Path of the file; its extension selects the format.

        Returns:
            The parsed configuration mapping.

        Raises:
            ConfigNotFound: If the file does not exist.
            ConfigFormatError: If the file cannot be parsed.
        """
