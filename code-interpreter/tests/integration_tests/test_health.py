from __future__ import annotations

import subprocess
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.executor_base import HealthCheck
from app.services.executor_docker import DockerExecutor
from app.services.executor_factory import get_executor


@pytest.fixture(autouse=True)
def _clear_executor_cache() -> Generator[None, None, None]:
    """Reset the lru_cache on get_executor so patches take effect."""
    get_executor.cache_clear()
    yield
    get_executor.cache_clear()


def test_health_returns_ok_when_backend_healthy() -> None:
    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["message"] is None


def test_health_returns_error_when_backend_unhealthy() -> None:
    unhealthy = HealthCheck(status="error", message="daemon down")

    with patch.object(DockerExecutor, "check_health", return_value=unhealthy):
        client = TestClient(create_app())
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["message"] == "daemon down"


def _make_completed(returncode: int, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=b"", stderr=stderr)


def test_docker_health_ok() -> None:
    """Both Docker daemon and image check succeed."""
    with patch("app.services.executor_docker.subprocess.run", return_value=_make_completed(0)):
        executor = DockerExecutor()
        result = executor.check_health()

    assert result.status == "ok"
    assert result.message is None


def test_docker_health_daemon_unreachable() -> None:
    """Docker daemon returns non-zero exit code."""
    with patch(
        "app.services.executor_docker.subprocess.run",
        return_value=_make_completed(1, stderr=b"Cannot connect to the Docker daemon"),
    ):
        executor = DockerExecutor()
        result = executor.check_health()

    assert result.status == "error"
    assert "Docker daemon not reachable" in (result.message or "")


def test_docker_health_daemon_timeout() -> None:
    """Docker daemon command times out."""
    with patch(
        "app.services.executor_docker.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=5),
    ):
        executor = DockerExecutor()
        result = executor.check_health()

    assert result.status == "error"
    assert "not responding" in (result.message or "")


def test_docker_health_binary_not_found() -> None:
    """Docker binary does not exist."""
    with patch(
        "app.services.executor_docker.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        executor = DockerExecutor()
        result = executor.check_health()

    assert result.status == "error"
    assert "not found" in (result.message or "")


def test_docker_health_image_missing() -> None:
    """Docker daemon is reachable but the executor image is not available."""
    daemon_ok = _make_completed(0)
    image_missing = _make_completed(1)

    call_count = 0

    def _side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal call_count
        call_count += 1
        # First call: docker version (daemon check) → ok
        # Second call: docker image inspect → fail
        return daemon_ok if call_count == 1 else image_missing

    with patch("app.services.executor_docker.subprocess.run", side_effect=_side_effect):
        executor = DockerExecutor()
        result = executor.check_health()

    assert result.status == "error"
    assert "not available locally" in (result.message or "")


def test_docker_health_image_check_timeout() -> None:
    """Docker daemon is reachable but the image inspect times out."""
    daemon_ok = _make_completed(0)
    call_count = 0

    def _side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return daemon_ok
        raise subprocess.TimeoutExpired(cmd="docker", timeout=5)

    with patch("app.services.executor_docker.subprocess.run", side_effect=_side_effect):
        executor = DockerExecutor()
        result = executor.check_health()

    assert result.status == "error"
    assert "Timeout checking image" in (result.message or "")
