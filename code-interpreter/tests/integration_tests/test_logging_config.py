from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Iterator
from types import ModuleType

import pytest


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Snapshot and restore root/uvicorn logger state around each test.

    setup_logging() mutates global logging state, so we save and restore it to
    keep tests isolated from each other and from the rest of the suite.
    """
    root = logging.getLogger()
    saved_root_handlers = root.handlers[:]
    saved_root_level = root.level
    saved: dict[str, tuple[list[logging.Handler], int, bool]] = {}
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level, lg.propagate)
    try:
        yield
    finally:
        root.handlers = saved_root_handlers
        root.setLevel(saved_root_level)
        for name, (handlers, level, propagate) in saved.items():
            lg = logging.getLogger(name)
            lg.handlers = handlers
            lg.setLevel(level)
            lg.propagate = propagate


def _reload_logging_config(
    monkeypatch: pytest.MonkeyPatch, log_format: str, level: str = "INFO"
) -> ModuleType:
    """Reload app.app_configs and app.logging_config under the given env."""
    monkeypatch.setenv("LOG_FORMAT", log_format)
    monkeypatch.setenv("LOG_LEVEL", level)
    import app.app_configs as app_configs
    import app.logging_config as logging_config

    importlib.reload(app_configs)
    return importlib.reload(logging_config)


def test_plain_format_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    import app.app_configs as app_configs

    importlib.reload(app_configs)
    assert app_configs.JSON_LOGGING is False


def test_plain_logging_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_config = _reload_logging_config(monkeypatch, "plain")
    logging_config.setup_logging()

    logging.getLogger("app.test").info("hello plain")

    err = capsys.readouterr().err
    assert "hello plain" in err
    assert "app.test" in err
    # Plain output is not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(err.strip().splitlines()[-1])


def test_json_logging_output_and_extra_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_config = _reload_logging_config(monkeypatch, "json")
    logging_config.setup_logging()

    logging.getLogger("app.test").info("hello json", extra={"session_id": "abc123"})

    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["message"] == "hello json"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    # extra fields are promoted to discrete top-level keys.
    assert payload["session_id"] == "abc123"
    # renamed standard fields are present.
    assert "timestamp" in payload


def test_json_logging_strips_color_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Uvicorn's ANSI-coded color_message must not leak into JSON output."""
    logging_config = _reload_logging_config(monkeypatch, "json")
    logging_config.setup_logging()

    logging.getLogger("uvicorn.error").info(
        "Started server", extra={"color_message": "\x1b[36mStarted server\x1b[0m"}
    )

    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["message"] == "Started server"
    assert "color_message" not in payload


def test_setup_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated calls must not stack duplicate handlers on the root logger."""
    logging_config = _reload_logging_config(monkeypatch, "plain")
    logging_config.setup_logging()
    logging_config.setup_logging()
    logging_config.setup_logging()

    assert len(logging.getLogger().handlers) == 1


def test_uvicorn_loggers_propagate_to_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uvicorn loggers should propagate to root and own no handlers of their own."""
    logging_config = _reload_logging_config(monkeypatch, "json")
    logging_config.setup_logging()

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == []
        assert lg.propagate is True
