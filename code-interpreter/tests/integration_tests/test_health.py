from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import (
    SERVICE_VERSION,
    BackendHealthMonitor,
    checker_stale_after_sec,
    create_app,
    network_isolation_mode,
)
from app.services.executor_base import HealthCheck
from app.services.executor_docker import DockerExecutor
from app.services.executor_factory import get_executor

CHART_YAML = Path(__file__).resolve().parents[3] / "kubernetes" / "code-interpreter" / "Chart.yaml"


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
    assert body["version"] == SERVICE_VERSION


def test_ready_returns_503_and_health_reports_error_when_backend_unhealthy() -> None:
    unhealthy = HealthCheck(status="error", message="daemon down")

    with patch.object(DockerExecutor, "check_health", return_value=unhealthy):
        client = TestClient(create_app())
        ready = client.get("/ready")
        health = client.get("/health")

    assert ready.status_code == 503
    assert ready.json()["status"] == "error"
    assert ready.json()["message"] == "daemon down"

    # /health stays 200 for the liveness probe but reports the cached result.
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "error"
    assert body["message"] == "daemon down"
    assert body["version"] == SERVICE_VERSION


def test_ready_returns_200_when_backend_healthy() -> None:
    with patch.object(DockerExecutor, "check_health", return_value=HealthCheck(status="ok")):
        client = TestClient(create_app())
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_times_out_slow_backend_check() -> None:
    def _slow_check(self: DockerExecutor) -> HealthCheck:
        time.sleep(1.0)
        return HealthCheck(status="ok")

    with (
        patch("app.main.BACKEND_CHECK_TIMEOUT_SEC", 0.1),
        patch.object(DockerExecutor, "check_health", _slow_check),
    ):
        client = TestClient(create_app())
        response = client.get("/ready")

    assert response.status_code == 503
    assert "timed out" in response.json()["message"]


def _health_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("backend-health")]


def _wait_until(predicate: Callable[[], bool], timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_hung_backend_check_is_single_flight_and_bounded() -> None:
    release = threading.Event()
    calls = 0

    def _hung_check(self: DockerExecutor) -> HealthCheck:
        nonlocal calls
        calls += 1
        release.wait()
        return HealthCheck(status="ok")

    try:
        with (
            patch("app.main.BACKEND_CHECK_TIMEOUT_SEC", 0.05),
            patch.object(DockerExecutor, "check_health", _hung_check),
        ):
            client = TestClient(create_app())
            baseline = threading.active_count()
            for _ in range(10):
                start = time.monotonic()
                response = client.get("/ready")
                assert time.monotonic() - start < 1
                assert response.status_code == 503
                assert "in progress" in response.json()["message"]
                assert len(_health_threads()) <= 2
                assert threading.active_count() <= baseline + 2
            assert calls == 1
    finally:
        release.set()


def test_health_stays_200_while_checks_fail() -> None:
    unhealthy = HealthCheck(status="error", message="kubernetes API down")
    with patch.object(DockerExecutor, "check_health", return_value=unhealthy):
        app = create_app()
        client = TestClient(app)
        monitor: BackendHealthMonitor = app.state.backend_monitor
        for _ in range(3):
            monitor.last_completed_at -= checker_stale_after_sec()
            assert client.get("/ready").status_code == 503
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["message"] == "kubernetes API down"


def test_health_flips_to_503_when_checker_is_wedged_and_recovers() -> None:
    release = threading.Event()

    def _hung_check(self: DockerExecutor) -> HealthCheck:
        release.wait()
        return HealthCheck(status="ok")

    try:
        with (
            patch("app.main.BACKEND_CHECK_TIMEOUT_SEC", 0.05),
            patch.object(DockerExecutor, "check_health", _hung_check),
        ):
            app = create_app()
            client = TestClient(app)
            monitor: BackendHealthMonitor = app.state.backend_monitor
            assert client.get("/ready").status_code == 503
            assert client.get("/health").status_code == 200

            monitor.last_completed_at = time.monotonic() - checker_stale_after_sec() - 1
            wedged = client.get("/health")
            assert wedged.status_code == 503
            assert "stuck" in wedged.json()["message"]

            release.set()
            _wait_until(lambda: not monitor.is_wedged())
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 200
    finally:
        release.set()


def test_checker_staleness_window_covers_three_intervals() -> None:
    with (
        patch("app.main.HEALTH_CHECK_INTERVAL_SEC", 30),
        patch("app.main.BACKEND_CHECK_TIMEOUT_SEC", 2.5),
    ):
        assert checker_stale_after_sec() == 92.5


def test_health_never_calls_executor_backend() -> None:
    """Liveness must not wait on the Docker daemon or the Kubernetes API."""
    with (
        patch("app.main.get_executor") as get_executor_mock,
        patch.object(DockerExecutor, "check_health") as check_mock,
    ):
        client = TestClient(create_app())
        for _ in range(3):
            assert client.get("/health").status_code == 200

    get_executor_mock.assert_not_called()
    check_mock.assert_not_called()


def test_health_reports_backend_and_network_isolation() -> None:
    client = TestClient(create_app())
    body = client.get("/health").json()
    assert body["executor_backend"] == "docker"
    assert body["network_isolation"].startswith("docker_network:")


@pytest.mark.parametrize(
    ("net_admin", "expected"),
    [
        (True, "net_admin_init_container+network_policy"),
        (False, "network_policy_only"),
    ],
)
def test_network_isolation_mode_for_kubernetes(net_admin: bool, expected: str) -> None:
    with (
        patch("app.main.EXECUTOR_BACKEND", "kubernetes"),
        patch("app.main.KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN", net_admin),
    ):
        assert network_isolation_mode() == expected


def test_health_version_matches_package_metadata() -> None:
    """The version should come from the installed package, not be hardcoded."""
    from importlib.metadata import version as package_version

    assert package_version("code-interpreter") == SERVICE_VERSION


def test_service_version_matches_helm_chart_version() -> None:
    """Guard against drift between the Python package and the Helm chart.

    A version mismatch means clients calling /health to gate on capabilities
    would see one number while the deployment artifact reports another.
    """
    assert CHART_YAML.is_file(), f"Chart.yaml not found at {CHART_YAML}"
    text = CHART_YAML.read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
    assert match is not None, f"could not find a top-level 'version:' line in {CHART_YAML}"
    chart_version = match.group(1).strip("\"'")
    assert chart_version == SERVICE_VERSION, (
        f"Helm chart version {chart_version!r} != Python package version "
        f"{SERVICE_VERSION!r}. Bump both together so /health and the deployed "
        "chart report the same number."
    )


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
