"""Unit tests for DockerExecutor session methods.

Mocks subprocess so the session lifecycle can be exercised without a real
Docker daemon.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.services.executor_base import (
    SESSION_APP_LABEL,
    SESSION_COMPONENT_LABEL,
    SESSION_NAME_PREFIX,
)
from app.services.executor_docker import DockerExecutor


@pytest.fixture()
def executor() -> DockerExecutor:
    """Create a DockerExecutor bypassing __init__ (no docker binary needed)."""
    inst = DockerExecutor.__new__(DockerExecutor)
    inst.docker_binary = "/usr/bin/docker"
    inst.image = "test:latest"
    inst.run_args = ""
    return inst


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess in text mode (subprocess calls use text=True)."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _label_values(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


def test_create_session_returns_session_info(executor: DockerExecutor) -> None:
    with patch("app.services.executor_docker.subprocess.run", return_value=_completed(0)):
        info = executor.create_session()

    assert info.session_id.startswith(SESSION_NAME_PREFIX)


def test_create_session_runs_docker_with_session_labels(executor: DockerExecutor) -> None:
    with patch("app.services.executor_docker.subprocess.run") as run:
        run.return_value = _completed(0)
        executor.create_session()

    cmd = run.call_args.args[0]
    label_values = _label_values(cmd)
    assert f"app={SESSION_APP_LABEL}" in label_values
    assert f"component={SESSION_COMPONENT_LABEL}" in label_values


def test_create_session_stages_files(executor: DockerExecutor) -> None:
    with (
        patch("app.services.executor_docker.subprocess.run", return_value=_completed(0)),
        patch.object(executor, "_upload_tar_to_container") as upload,
    ):
        info = executor.create_session(files=[("data.txt", b"hello")])

    upload.assert_called_once()
    container_arg, tar_arg = upload.call_args.args
    assert container_arg == info.session_id
    assert isinstance(tar_arg, bytes)
    assert len(tar_arg) > 0


def test_create_session_skips_upload_when_no_files(executor: DockerExecutor) -> None:
    with (
        patch("app.services.executor_docker.subprocess.run", return_value=_completed(0)),
        patch.object(executor, "_upload_tar_to_container") as upload,
    ):
        executor.create_session()

    upload.assert_not_called()


def test_create_session_kills_container_on_staging_failure(executor: DockerExecutor) -> None:
    with (
        patch("app.services.executor_docker.subprocess.run", return_value=_completed(0)),
        patch.object(executor, "_upload_tar_to_container", side_effect=RuntimeError("boom")),
        patch.object(executor, "_kill_container") as kill,
        pytest.raises(RuntimeError, match="boom"),
    ):
        executor.create_session(files=[("data.txt", b"x")])

    kill.assert_called_once()


def test_create_session_raises_when_docker_run_fails(executor: DockerExecutor) -> None:
    with (
        patch(
            "app.services.executor_docker.subprocess.run",
            return_value=_completed(1, stderr="docker daemon down"),
        ),
        pytest.raises(RuntimeError, match="Failed to start session container"),
    ):
        executor.create_session()


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------


def test_delete_session_returns_true_on_success(executor: DockerExecutor) -> None:
    with patch("app.services.executor_docker.subprocess.run", return_value=_completed(0)):
        assert executor.delete_session(f"{SESSION_NAME_PREFIX}abc") is True


def test_delete_session_returns_false_on_no_such_container(executor: DockerExecutor) -> None:
    """Modern Docker exits 0 even when the container is missing — stderr is the signal."""
    with patch(
        "app.services.executor_docker.subprocess.run",
        return_value=_completed(0, stderr="Error: No such container: code-session-abc"),
    ):
        assert executor.delete_session(f"{SESSION_NAME_PREFIX}abc") is False


def test_delete_session_rejects_non_session_id(executor: DockerExecutor) -> None:
    """Prefix check prevents accidentally deleting unrelated containers."""
    run_mock = MagicMock()
    with patch("app.services.executor_docker.subprocess.run", run_mock):
        assert executor.delete_session("random-name") is False
    run_mock.assert_not_called()


def test_delete_session_raises_on_unexpected_failure(executor: DockerExecutor) -> None:
    with (
        patch(
            "app.services.executor_docker.subprocess.run",
            return_value=_completed(1, stderr="some other failure"),
        ),
        pytest.raises(RuntimeError, match="Failed to delete session"),
    ):
        executor.delete_session(f"{SESSION_NAME_PREFIX}abc")
