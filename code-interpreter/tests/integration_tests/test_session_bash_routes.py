"""Route-layer tests for POST /v1/sessions/{id}/bash."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.executor_base import ExecutionResult, SessionNotFoundError
from app.services.executor_factory import get_executor


@pytest.fixture(autouse=True)
def _clear_executor_cache() -> Generator[None, None, None]:
    get_executor.cache_clear()
    yield
    get_executor.cache_clear()


def _result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = 0,
    timed_out: bool = False,
    duration_ms: int = 5,
) -> ExecutionResult:
    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        files=tuple(),
    )


def test_bash_returns_execution_result() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.return_value = _result(
        stdout="hi\n", exit_code=0, duration_ms=12
    )

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post(
            "/v1/sessions/code-session-abc/bash",
            json={"cmd": "echo hi"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "stdout": "hi\n",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 12,
    }

    call = mock_executor.execute_bash_in_session.call_args
    assert call.args == ("code-session-abc",)
    assert call.kwargs["cmd"] == "echo hi"


def test_bash_passes_through_nonzero_exit() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.return_value = _result(stderr="boom\n", exit_code=2)

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post(
            "/v1/sessions/code-session-abc/bash",
            json={"cmd": "false"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 2
    assert body["stderr"] == "boom\n"


def test_bash_timed_out_is_reported() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.return_value = _result(exit_code=None, timed_out=True)

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post(
            "/v1/sessions/code-session-abc/bash",
            json={"cmd": "sleep 100"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is True
    assert body["exit_code"] is None


def test_bash_default_timeout_is_30s() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.return_value = _result()

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        client.post("/v1/sessions/code-session-x/bash", json={"cmd": "true"})

    assert mock_executor.execute_bash_in_session.call_args.kwargs["timeout_ms"] == 30_000


def test_bash_uses_provided_timeout() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.return_value = _result()

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        client.post(
            "/v1/sessions/code-session-x/bash",
            json={"cmd": "true", "timeout_ms": 5000},
        )

    assert mock_executor.execute_bash_in_session.call_args.kwargs["timeout_ms"] == 5_000


def test_bash_rejects_timeout_above_cap() -> None:
    """The route's cap mirrors /v1/execute (max_exec_timeout_ms, default 60s)."""
    client = TestClient(create_app())
    response = client.post(
        "/v1/sessions/code-session-x/bash",
        json={"cmd": "true", "timeout_ms": 1_000_000},
    )
    assert response.status_code == 422


def test_bash_rejects_non_positive_timeout() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/sessions/code-session-x/bash",
        json={"cmd": "true", "timeout_ms": 0},
    )
    assert response.status_code == 422


def test_bash_requires_cmd() -> None:
    client = TestClient(create_app())
    response = client.post("/v1/sessions/code-session-x/bash", json={})
    assert response.status_code == 422


def test_bash_returns_404_when_session_not_found() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.side_effect = SessionNotFoundError("code-session-missing")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post(
            "/v1/sessions/code-session-missing/bash",
            json={"cmd": "true"},
        )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_bash_returns_501_when_unsupported() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.side_effect = NotImplementedError("nope")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post(
            "/v1/sessions/code-session-x/bash",
            json={"cmd": "true"},
        )

    assert response.status_code == 501


def test_bash_max_output_bytes_passed_from_settings() -> None:
    mock_executor = MagicMock()
    mock_executor.execute_bash_in_session.return_value = _result()

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        client.post("/v1/sessions/code-session-x/bash", json={"cmd": "true"})

    # The route should forward the configured cap, not let callers override it.
    kwargs = mock_executor.execute_bash_in_session.call_args.kwargs
    assert kwargs["max_output_bytes"] > 0
