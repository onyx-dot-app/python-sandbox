"""Tests for the executor image watchdog.

Executor containers start with ``--pull never``, so if the host removes the
executor image while the service is running (``docker system prune -a``, image
garbage collection), every execution fails until the image is back. The
watchdog re-pulls it from the lifespan so the service recovers on its own.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.app_configs import PYTHON_EXECUTOR_DOCKER_IMAGE
from app.image_ref import normalize_image_ref
from app.main import _image_watchdog_once, create_app
from app.services.executor_factory import get_executor

IMAGE = normalize_image_ref(PYTHON_EXECUTOR_DOCKER_IMAGE)


@pytest.fixture(autouse=True)
def _clear_executor_cache() -> Generator[None, None, None]:
    """Reset the lru_cache on get_executor so patches take effect."""
    get_executor.cache_clear()
    yield
    get_executor.cache_clear()


def _completed(returncode: int, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=b"", stderr=stderr)


class FakeDockerHost:
    """Stand-in for the docker CLI that tracks which images the host holds.

    Handles the commands the health and watchdog paths issue: ``docker version``,
    ``docker image inspect`` and ``docker pull``.
    """

    def __init__(self, *, images: set[str] | None = None, pull_ok: bool = True) -> None:
        self.images: set[str] = set(images or ())
        self.pull_ok = pull_ok
        self.pulls: list[str] = []
        self.commands: list[list[str]] = []

    def run(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(list(cmd))
        args = cmd[1:]  # drop the docker binary
        if args[:1] == ["version"]:
            return _completed(0)
        if args[:2] == ["image", "inspect"]:
            present = args[2] in self.images
            return _completed(0 if present else 1, stderr=b"" if present else b"No such image")
        if args[:1] == ["pull"]:
            self.pulls.append(args[1])
            if not self.pull_ok:
                return _completed(1, stderr=b"manifest unknown")
            self.images.add(args[1])
            return _completed(0)
        raise AssertionError(f"unexpected docker command: {cmd}")


@contextmanager
def _fake_docker(host: FakeDockerHost) -> Iterator[None]:
    with (
        patch("app.main.which", return_value="/usr/bin/docker"),
        patch("app.main.subprocess.run", side_effect=host.run),
        patch("app.services.executor_docker.subprocess.run", side_effect=host.run),
    ):
        yield


def test_watchdog_pulls_when_image_missing() -> None:
    host = FakeDockerHost(images=set())

    with _fake_docker(host):
        asyncio.run(_image_watchdog_once())

    assert host.pulls == [IMAGE]
    assert IMAGE in host.images


def test_watchdog_does_not_pull_when_image_present() -> None:
    """The common path must stay cheap: one ``docker image inspect`` and nothing else."""
    host = FakeDockerHost(images={IMAGE})

    with _fake_docker(host):
        asyncio.run(_image_watchdog_once())

    assert host.pulls == []
    assert [cmd[1:3] for cmd in host.commands] == [["image", "inspect"]]


def test_watchdog_pulls_pinned_digest_not_moving_tag() -> None:
    """A digest-pinned deployment must re-pull the pinned digest."""
    pinned = "onyxdotapp/python-executor-sci@sha256:" + "ab" * 32
    host = FakeDockerHost(images=set())

    with patch("app.main.PYTHON_EXECUTOR_DOCKER_IMAGE", pinned), _fake_docker(host):
        asyncio.run(_image_watchdog_once())

    assert host.pulls == [pinned]


def test_watchdog_pass_returns_when_pull_fails() -> None:
    """A failed re-pull is logged and retried later; it must not kill the service."""
    host = FakeDockerHost(images=set(), pull_ok=False)

    with _fake_docker(host):
        asyncio.run(_image_watchdog_once())  # must not raise

        assert host.pulls == [IMAGE]
        assert IMAGE not in host.images

        # The service keeps serving and keeps naming the condition precisely.
        body = TestClient(create_app()).get("/ready").json()

    assert body["status"] == "error"
    assert f"Executor image {IMAGE} not available locally" == body["message"]


@pytest.mark.parametrize("timing_out", ["image", "pull"])
def test_watchdog_pass_returns_when_docker_times_out(timing_out: str) -> None:
    def _run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if cmd[1] == timing_out:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return _completed(1)

    with (
        patch("app.main.which", return_value="/usr/bin/docker"),
        patch("app.main.subprocess.run", side_effect=_run),
    ):
        asyncio.run(_image_watchdog_once())  # must not raise


def test_health_recovers_after_watchdog_repull() -> None:
    """The exact reported scenario: a host prune breaks readiness, the watchdog restores it."""
    host = FakeDockerHost(images={IMAGE})

    with _fake_docker(host):
        client = TestClient(create_app())
        assert client.get("/ready").json()["status"] == "ok"

        # The host removes the dormant image while the service is running.
        host.images.clear()
        body = client.get("/ready").json()
        assert body["status"] == "error"
        assert "not available locally" in body["message"]

        asyncio.run(_image_watchdog_once())

        assert host.pulls == [IMAGE]
        body = client.get("/ready").json()

    assert body["status"] == "ok"
    assert body["message"] is None


@pytest.mark.parametrize(
    ("backend", "interval_sec", "expected_intervals"),
    [
        ("docker", 45, [45]),
        ("docker", 0, []),  # interval 0 disables the watchdog (air-gapped hosts)
        ("kubernetes", 45, []),  # the watchdog is Docker-specific
    ],
)
def test_lifespan_starts_watchdog_only_for_docker_backend(
    backend: str, interval_sec: int, expected_intervals: list[int]
) -> None:
    started: list[int] = []

    async def _fake_loop(interval_sec: int) -> None:
        started.append(interval_sec)
        await asyncio.Event().wait()  # park until the lifespan cancels us

    with (
        patch("app.main.EXECUTOR_BACKEND", backend),
        patch("app.main.PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC", interval_sec),
        patch("app.main._ensure_docker_image_available"),
        patch("app.main._reap_expired_sessions_once", new=AsyncMock()),
        patch("app.main._image_watchdog_loop", new=_fake_loop),
        TestClient(create_app()),
    ):
        pass

    assert started == expected_intervals
