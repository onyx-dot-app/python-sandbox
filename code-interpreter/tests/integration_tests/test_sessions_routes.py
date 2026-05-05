"""Route-layer tests for /v1/sessions.

Patches the executor so the routes can be exercised without a real Docker
daemon or Kubernetes cluster.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.executor_base import SessionInfo
from app.services.executor_factory import get_executor


@pytest.fixture(autouse=True)
def _clear_executor_cache() -> Generator[None, None, None]:
    get_executor.cache_clear()
    yield
    get_executor.cache_clear()


def test_create_session_returns_session_id() -> None:
    mock_executor = MagicMock()
    mock_executor.create_session.return_value = SessionInfo(session_id="code-session-abc")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post("/v1/sessions", json={})

    assert response.status_code == 201
    body = response.json()
    assert body["session_id"] == "code-session-abc"


def test_create_session_returns_404_for_unknown_file_id() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/sessions",
        json={"files": [{"path": "data.txt", "file_id": "does-not-exist"}]},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_create_session_resolves_file_ids_into_content() -> None:
    """Files referenced by file_id must be loaded and passed to the executor."""
    client = TestClient(create_app())
    upload_resp = client.post(
        "/v1/files",
        files={"file": ("data.txt", b"hello bytes", "application/octet-stream")},
    )
    file_id = upload_resp.json()["file_id"]

    mock_executor = MagicMock()
    mock_executor.create_session.return_value = SessionInfo(session_id="code-session-x")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        response = client.post(
            "/v1/sessions",
            json={"files": [{"path": "inputs/data.txt", "file_id": file_id}]},
        )

    assert response.status_code == 201
    files_arg = mock_executor.create_session.call_args.kwargs["files"]
    assert files_arg == [("inputs/data.txt", b"hello bytes")]


def test_create_session_returns_422_when_executor_raises_value_error() -> None:
    mock_executor = MagicMock()
    mock_executor.create_session.side_effect = ValueError("bad path")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post("/v1/sessions", json={})

    assert response.status_code == 422
    assert "bad path" in response.json()["detail"]


def test_create_session_returns_501_when_unsupported() -> None:
    mock_executor = MagicMock()
    mock_executor.create_session.side_effect = NotImplementedError("nope")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.post("/v1/sessions", json={})

    assert response.status_code == 501


def test_delete_session_returns_204_when_found() -> None:
    mock_executor = MagicMock()
    mock_executor.delete_session.return_value = True

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.delete("/v1/sessions/code-session-abc")

    assert response.status_code == 204
    mock_executor.delete_session.assert_called_once_with("code-session-abc")


def test_delete_session_returns_404_when_unknown() -> None:
    mock_executor = MagicMock()
    mock_executor.delete_session.return_value = False

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.delete("/v1/sessions/code-session-missing")

    assert response.status_code == 404


def test_delete_session_returns_501_when_unsupported() -> None:
    mock_executor = MagicMock()
    mock_executor.delete_session.side_effect = NotImplementedError("nope")

    with patch("app.api.routes.get_executor", return_value=mock_executor):
        client = TestClient(create_app())
        response = client.delete("/v1/sessions/code-session-abc")

    assert response.status_code == 501
