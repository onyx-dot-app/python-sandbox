from __future__ import annotations

import logging
from typing import Final

from app.app_configs import JSON_LOGGING, LOG_LEVEL

PLAIN_FORMAT: Final[str] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Uvicorn installs its own handlers on these loggers. We clear them so records
# propagate to the root logger and are emitted through our single handler/format.
_UVICORN_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")


class _DropColorMessageFilter(logging.Filter):
    """Drop uvicorn's ``color_message`` record attribute.

    Uvicorn attaches an ANSI-colored duplicate of the message as
    ``record.color_message``. The JSON formatter would otherwise emit it as a
    field carrying raw escape codes, polluting the structured output.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "color_message"):
            del record.color_message
        return True


def get_json_formatter() -> logging.Formatter:
    """Return a structured single-line JSON formatter.

    Standard record attributes are emitted as discrete top-level fields and any
    ``extra`` keys passed to a logging call are merged in alongside them, which
    makes the output suitable for container log aggregators.

    The ``pythonjsonlogger`` import is deferred to this call site (only reached
    when ``LOG_FORMAT=json``) so importing this module never hard-fails in
    environments where the optional dependency is absent.
    """
    from pythonjsonlogger.json import JsonFormatter

    return JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(filename)s %(lineno)d %(message)s",
        rename_fields={
            "asctime": "timestamp",
            "levelname": "level",
            "name": "logger",
        },
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def get_formatter() -> logging.Formatter:
    """Return the configured formatter (JSON when ``LOG_FORMAT=json``)."""
    if JSON_LOGGING:
        return get_json_formatter()
    return logging.Formatter(PLAIN_FORMAT)


def _resolve_level() -> tuple[int, bool]:
    """Resolve LOG_LEVEL to a numeric level, falling back to INFO.

    Returns ``(level, was_valid)``. ``setLevel`` would raise ``ValueError`` on a
    typo'd, operator-supplied level (e.g. ``INFOO``); we fall back to INFO and
    let the caller warn, rather than crash the service at startup.
    """
    level = logging.getLevelNamesMapping().get(LOG_LEVEL)
    if level is None:
        return logging.INFO, False
    return level, True


def setup_logging() -> None:
    """Configure root and uvicorn logging from the environment settings.

    Idempotent: the root logger's handlers are replaced (not appended) so that
    repeated calls — e.g. module import plus an explicit startup call — do not
    stack duplicate handlers.

    Note: this consolidates *all* logging (including uvicorn's access/error
    logs) onto a single handler and format. In the default ``plain`` mode that
    replaces uvicorn's own colorized access-log format with ``PLAIN_FORMAT``.
    """
    formatter = get_formatter()

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(_DropColorMessageFilter())

    level, level_valid = _resolve_level()

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Route uvicorn's loggers through the root handler to keep one consistent
    # format, and let them propagate rather than emitting via their own handlers.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.setLevel(level)
        uvicorn_logger.propagate = True

    if not level_valid:
        logging.getLogger(__name__).warning("Unknown LOG_LEVEL %r; falling back to INFO", LOG_LEVEL)
