from pathlib import Path

import pytest

from gitconfig_cli.abstractions import ConfigSerializer
from gitconfig_cli.config_parser import (
    ConfigParser,
    JsonFormat,
    YamlFormat,
    parse_value,
)
from gitconfig_cli.exceptions import ConfigFormatError, ConfigNotFound


@pytest.fixture
def parser() -> ConfigParser:
    return ConfigParser()


def test_parser_implements_the_serializer_abstraction(parser: ConfigParser) -> None:
    assert isinstance(parser, ConfigSerializer)


def test_supported_extensions(parser: ConfigParser) -> None:
    assert parser.supported_extensions == (".json", ".yaml", ".yml")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("app.json", True),
        ("app.yaml", True),
        ("services/api.yml", True),
        ("APP.JSON", True),
        ("notes.txt", False),
        ("Makefile", False),
    ],
)
def test_supports(parser: ConfigParser, name: str, expected: bool) -> None:
    assert parser.supports(name) is expected


def test_format_for_picks_strategy(parser: ConfigParser) -> None:
    assert isinstance(parser.format_for("a.json"), JsonFormat)
    assert isinstance(parser.format_for("a.yml"), YamlFormat)


def test_format_for_rejects_unknown_extension(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match=r"unsupported file type '\.txt'"):
        parser.format_for("notes.txt")


def test_format_for_rejects_missing_extension(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match=r"'\(none\)'"):
        parser.format_for("Makefile")


def test_custom_formats_can_be_injected() -> None:
    parser = ConfigParser(formats=[YamlFormat()])

    assert parser.supports("a.yaml")
    assert not parser.supports("a.json")


def test_loads_json(parser: ConfigParser) -> None:
    assert parser.loads("app.json", '{"server": {"port": 8080}}') == {
        "server": {"port": 8080}
    }


def test_loads_yaml(parser: ConfigParser) -> None:
    text = "server:\n  port: 8080\n  debug: true\n"

    assert parser.loads("app.yaml", text) == {"server": {"port": 8080, "debug": True}}


def test_loads_empty_yaml_is_empty_mapping(parser: ConfigParser) -> None:
    assert parser.loads("app.yaml", "") == {}


def test_loads_malformed_json(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match="malformed json") as exc_info:
        parser.loads("app.json", '{"port": ')

    assert exc_info.value.name == "app.json"


def test_loads_malformed_yaml(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match="malformed yaml"):
        parser.loads("app.yaml", "key: [unclosed")


def test_loads_rejects_non_mapping(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match="must be a mapping, got list"):
        parser.loads("app.json", "[1, 2]")


def test_yaml_safe_load_refuses_python_objects(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError):
        parser.loads("app.yaml", "!!python/object/apply:os.system ['echo hi']")


def test_dumps_json_is_indented_with_newline(parser: ConfigParser) -> None:
    assert parser.dumps("app.json", {"port": 8080}) == '{\n  "port": 8080\n}\n'


def test_dumps_json_keeps_unicode(parser: ConfigParser) -> None:
    assert "café" in parser.dumps("app.json", {"name": "café"})


def test_dumps_yaml_preserves_key_order(parser: ConfigParser) -> None:
    text = parser.dumps("app.yaml", {"zeta": 1, "alpha": {"b": 2}})

    assert text == "zeta: 1\nalpha:\n  b: 2\n"


@pytest.mark.parametrize("name", ["app.json", "app.yaml"])
def test_round_trip(parser: ConfigParser, name: str) -> None:
    data = {"server": {"port": 8080, "hosts": ["a", "b"]}, "debug": False}

    assert parser.loads(name, parser.dumps(name, data)) == data


def test_dumps_rejects_non_mapping(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match="must be a mapping"):
        parser.dumps("app.json", [1, 2])  # type: ignore[arg-type]


def test_dumps_json_rejects_unserializable(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match="cannot write json"):
        parser.dumps("app.json", {"value": object()})


def test_dumps_yaml_rejects_unserializable(parser: ConfigParser) -> None:
    with pytest.raises(ConfigFormatError, match="cannot write yaml"):
        parser.dumps("app.yaml", {"value": object()})


def test_load_file(parser: ConfigParser, tmp_path: Path) -> None:
    source = tmp_path / "input.yaml"
    source.write_text("port: 9000\n", encoding="utf-8")

    assert parser.load_file(source) == {"port": 9000}


def test_load_file_missing(parser: ConfigParser, tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFound):
        parser.load_file(tmp_path / "missing.json")


def test_load_file_unreadable(parser: ConfigParser, tmp_path: Path) -> None:
    source = tmp_path / "binary.json"
    source.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(ConfigFormatError, match="cannot read file"):
        parser.load_file(source)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8080", 8080),
        ("1.5", 1.5),
        ("true", True),
        ("null", None),
        ("[1, 2]", [1, 2]),
        ('{"a": 1}', {"a": 1}),
        ('"quoted"', "quoted"),
        ("localhost", "localhost"),
        ("", ""),
    ],
)
def test_parse_value(raw: str, expected: object) -> None:
    assert parse_value(raw) == expected
