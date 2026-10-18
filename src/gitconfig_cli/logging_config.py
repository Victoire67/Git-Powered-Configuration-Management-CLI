"""Structured logging and audit trail for gitconfig-cli.

The tool logs through ``structlog`` on top of the standard ``logging``
module. Two outputs are configured by :func:`configure_logging`:

* an **audit file** that receives one JSON object per line for every
  action (who ran what, against which repository, with what result), and
* the **console** (stderr), which stays silent by default and shows
  human-readable events when the user passes ``-v`` / ``-vv``.

Use :func:`get_logger` to obtain a logger and :func:`audit` to wrap an
action so its start, success or failure is always recorded.
"""

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

LOGGER_NAME = "gitconfig_cli"
DEFAULT_LOG_FILE = Path.home() / ".gitconfig-cli" / "audit.log"

# Above CRITICAL, so the console handler drops everything unless -v is used.
_SILENT = logging.CRITICAL + 10

_SHARED_PROCESSORS: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def _console_level(verbosity: int) -> int:
    """Map the number of ``-v`` flags to a logging level."""
    if verbosity <= 0:
        return _SILENT
    if verbosity == 1:
        return logging.INFO
    return logging.DEBUG


def configure_logging(log_file: Path | None = None, verbosity: int = 0) -> None:
    """Configure structlog and the standard logging handlers.

    Calling this function again replaces the previous configuration, which
    keeps it safe to call once per CLI invocation (and in tests).

    Args:
        log_file: Where to append the JSON audit trail. ``None`` disables
            the audit file.
        verbosity: ``0`` keeps the console quiet, ``1`` shows info events,
            ``2`` or more also shows debug events (e.g. raw Git calls).
    """
    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(_console_level(verbosity))
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
    )
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.dict_tracebacks,
                    structlog.processors.JSONRenderer(),
                ],
                foreign_pre_chain=_SHARED_PROCESSORS,
            )
        )
        logger.addHandler(file_handler)


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger for one component of the tool.

    Args:
        component: Short component name, e.g. ``"git"`` or ``"manager"``.

    Returns:
        A structlog bound logger whose name is ``gitconfig_cli.<component>``.
    """
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(
        f"{LOGGER_NAME}.{component}"
    )
    return logger


def bind_context(**fields: Any) -> None:
    """Attach fields (e.g. repository path, command) to every later log entry.

    Args:
        **fields: Key/value pairs to include in all subsequent events.
    """
    structlog.contextvars.bind_contextvars(**fields)


@contextmanager
def bound_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log entry emitted inside a ``with`` block.

    Args:
        **fields: Key/value pairs, e.g. ``dry_run=True``.

    Yields:
        Nothing; the fields are removed again when the block exits.
    """
    with structlog.contextvars.bound_contextvars(**fields):
        yield


def clear_context() -> None:
    """Remove all fields previously attached with :func:`bind_context`."""
    structlog.contextvars.clear_contextvars()


@contextmanager
def audit(action: str, **fields: Any) -> Iterator[structlog.stdlib.BoundLogger]:
    """Record the lifecycle of one action in the audit trail.

    Logs ``started`` on entry, then either ``succeeded`` or ``failed``
    (with the error type and message) on exit, always including the
    duration. Exceptions are re-raised unchanged.

    Example:
        >>> with audit("config.set", config="app.json") as log:
        ...     log.info("value.written", key="db.port")

    Args:
        action: Dotted action name, e.g. ``"draft.publish"``.
        **fields: Extra details to attach to every event of this action.

    Yields:
        A logger bound with ``action`` and ``fields`` for intermediate events.
    """
    log = get_logger("audit").bind(action=action, **fields)
    start = time.perf_counter()
    log.info("started")
    try:
        yield log
    except Exception as exc:
        log.error(
            "failed",
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        raise
    log.info("succeeded", duration_ms=round((time.perf_counter() - start) * 1000, 2))
