"""Unit tests for DockerExecutor.execute_bash_in_session."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.services.executor_base import SESSION_NAME_PREFIX, SessionNotFoundError
from app.services.executor_docker import DockerExecutor


@pytest.fixture()
def executor() -> DockerExecutor:
    inst = DockerExecutor.__new__(DockerExecutor)
    inst.docker_binary = "/usr/bin/docker"
    inst.image = "test:latest"
    inst.run_args = ""
    return inst


def _popen_mock(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
    raise_timeout: bool = False,
) -> MagicMock:
    proc = MagicMock()
    if raise_timeout:
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="docker", timeout=1),
            (stdout, stderr),
        ]
    else:
        proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


def test_bash_returns_stdout_and_exit_code(executor: DockerExecutor) -> None:
    proc = _popen_mock(stdout=b"hi\n", returncode=0)
    with patch("app.services.executor_docker.subprocess.Popen", return_value=proc):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="echo hi",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    assert result.stdout == "hi\n"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.files == ()


def test_bash_passes_through_nonzero_exit(executor: DockerExecutor) -> None:
    proc = _popen_mock(stderr=b"boom\n", returncode=2)
    with patch("app.services.executor_docker.subprocess.Popen", return_value=proc):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="false",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    assert result.exit_code == 2
    assert result.stderr == "boom\n"


def test_bash_uses_docker_exec_with_bash_dash_c(executor: DockerExecutor) -> None:
    proc = _popen_mock(returncode=0)
    with patch("app.services.executor_docker.subprocess.Popen", return_value=proc) as popen:
        executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="ls -la",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    cmd = popen.call_args.args[0]
    assert cmd[:2] == [executor.docker_binary, "exec"]
    assert cmd[-3:] == ["bash", "-c", "ls -la"]
    assert f"{SESSION_NAME_PREFIX}abc" in cmd
    # No --network flag — exec inherits the container's locked-down network namespace.
    assert "--network" not in cmd


def test_bash_runs_as_unprivileged_user(executor: DockerExecutor) -> None:
    """The exec must drop to 65532:65532, matching the session container's user."""
    proc = _popen_mock(returncode=0)
    with patch("app.services.executor_docker.subprocess.Popen", return_value=proc) as popen:
        executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="id",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    cmd = popen.call_args.args[0]
    user_idx = cmd.index("-u")
    assert cmd[user_idx + 1] == "65532:65532"


def test_bash_times_out_and_kills_bash(executor: DockerExecutor) -> None:
    proc = _popen_mock(raise_timeout=True)
    with (
        patch("app.services.executor_docker.subprocess.Popen", return_value=proc),
        patch("app.services.executor_docker.subprocess.run") as run,
    ):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="sleep 999",
            timeout_ms=10,
            max_output_bytes=65_536,
        )

    assert result.timed_out is True
    assert result.exit_code is None
    proc.kill.assert_called_once()
    # Verify pkill -9 bash was invoked inside the container.
    pkill_calls = [
        c
        for c in run.call_args_list
        if c.args[0]
        == [
            executor.docker_binary,
            "exec",
            f"{SESSION_NAME_PREFIX}abc",
            "pkill",
            "-9",
            "bash",
        ]
    ]
    assert len(pkill_calls) == 1


def test_bash_rejects_non_session_id(executor: DockerExecutor) -> None:
    with (
        patch("app.services.executor_docker.subprocess.Popen") as popen,
        pytest.raises(SessionNotFoundError),
    ):
        executor.execute_bash_in_session(
            "random-name",
            cmd="true",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )
    popen.assert_not_called()


def test_bash_raises_session_not_found_when_container_missing(
    executor: DockerExecutor,
) -> None:
    proc = _popen_mock(
        stderr=b"Error response from daemon: No such container: code-session-abc\n",
        returncode=1,
    )
    with (
        patch("app.services.executor_docker.subprocess.Popen", return_value=proc),
        pytest.raises(SessionNotFoundError),
    ):
        executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="true",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )


def test_bash_treats_other_nonzero_as_command_failure(executor: DockerExecutor) -> None:
    """Generic non-zero exit + stderr should NOT be misclassified as 'session missing'."""
    proc = _popen_mock(stderr=b"command not found: foo\n", returncode=127)
    with patch("app.services.executor_docker.subprocess.Popen", return_value=proc):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="foo",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    assert result.exit_code == 127
    assert result.stderr == "command not found: foo\n"


def test_bash_truncates_stdout_to_max_bytes(executor: DockerExecutor) -> None:
    proc = _popen_mock(stdout=b"x" * 200, returncode=0)
    with patch("app.services.executor_docker.subprocess.Popen", return_value=proc):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="cat big",
            timeout_ms=5_000,
            max_output_bytes=20,
        )

    assert "[truncated]" in result.stdout
