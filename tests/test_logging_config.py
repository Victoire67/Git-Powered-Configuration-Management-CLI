import json
import logging
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from gitconfig_cli.logging_config import (
    LOGGER_NAME,
    audit,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)


def _read_events(log_file: Path) -> list[dict[str, object]]:
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        handler.flush()
    return [json.loads(line) for line in log_file.read_text("utf-8").splitlines()]


def test_audit_file_receives_json_lines(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "audit.log"
    configure_logging(log_file=log_file)

    get_logger("test").info("config.created", config="app.json")

    events = _read_events(log_file)
    assert events[-1]["event"] == "config.created"
    assert events[-1]["config"] == "app.json"
    assert events[-1]["level"] == "info"
    assert events[-1]["logger"] == "gitconfig_cli.test"
    assert "timestamp" in events[-1]


def test_debug_events_only_reach_file_with_high_verbosity(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.log"

    configure_logging(log_file=log_file, verbosity=0)
    get_logger("test").debug("hidden")
    configure_logging(log_file=log_file, verbosity=2)
    get_logger("test").debug("shown")

    names = [event["event"] for event in _read_events(log_file)]
    assert names == ["shown"]


def test_console_is_silent_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(verbosity=0)

    get_logger("test").error("should not be printed")

    assert "should not be printed" not in capsys.readouterr().err


def test_console_shows_info_with_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(verbosity=1)

    get_logger("test").info("visible event", config="app.json")

    err = capsys.readouterr().err
    assert "visible event" in err
    assert "config=app.json" in err


def test_reconfiguring_replaces_handlers(tmp_path: Path) -> None:
    configure_logging(log_file=tmp_path / "a.log")
    configure_logging(log_file=tmp_path / "b.log")

    assert len(logging.getLogger(LOGGER_NAME).handlers) == 2


def test_bound_context_is_added_to_every_event(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.log"
    configure_logging(log_file=log_file)

    bind_context(repo="/tmp/configs", command="publish")
    try:
        get_logger("test").info("something")
    finally:
        clear_context()
    get_logger("test").info("after clear")

    first, second = _read_events(log_file)
    assert first["repo"] == "/tmp/configs"
    assert first["command"] == "publish"
    assert "repo" not in second


def test_audit_records_start_and_success() -> None:
    configure_logging()

    with capture_logs() as logs, audit("config.set", config="app.json") as log:
        log.info("value.written", key="db.port")

    assert [entry["event"] for entry in logs] == [
        "started",
        "value.written",
        "succeeded",
    ]
    assert all(entry["action"] == "config.set" for entry in logs)
    assert all(entry["config"] == "app.json" for entry in logs)
    assert "duration_ms" in logs[-1]


def test_audit_records_failure_and_reraises() -> None:
    configure_logging()

    with (
        capture_logs() as logs,
        pytest.raises(ValueError, match="boom"),
        audit("draft.publish", draft="new-port"),
    ):
        raise ValueError("boom")

    failed = logs[-1]
    assert failed["event"] == "failed"
    assert failed["log_level"] == "error"
    assert failed["error_type"] == "ValueError"
    assert failed["error"] == "boom"


def test_structlog_is_configured_for_stdlib() -> None:
    configure_logging()

    assert structlog.is_configured()
