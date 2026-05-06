"""Unit tests for KubernetesExecutor.execute_bash_in_session."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.services.executor_base import SESSION_NAME_PREFIX, SessionNotFoundError
from app.services.executor_kubernetes import KubernetesExecutor


@pytest.fixture()
def executor() -> KubernetesExecutor:
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst.namespace = "test"
    inst.image = "test:latest"
    inst.service_account = ""
    return inst


class _FakeExecResp:
    """Minimal stand-in for a Kubernetes WebSocket exec stream."""

    def __init__(
        self,
        stdout_chunks: list[str] | None = None,
        stderr_chunks: list[str] | None = None,
        exit_status: str = "{'status': 'Success'}",
    ) -> None:
        self._stdout = list(stdout_chunks or [])
        self._stderr = list(stderr_chunks or [])
        self._exit_status = exit_status
        self._exit_delivered = False
        self._closed = False

    def is_open(self) -> bool:
        if self._closed:
            return False
        return bool(self._stdout or self._stderr or not self._exit_delivered)

    def update(self, timeout: float = 1) -> None:  # noqa: ARG002
        pass

    def peek_stdout(self) -> bool:
        return bool(self._stdout)

    def read_stdout(self) -> str:
        return self._stdout.pop(0)

    def peek_stderr(self) -> bool:
        return bool(self._stderr)

    def read_stderr(self) -> str:
        return self._stderr.pop(0)

    def read_channel(self, _channel: int) -> str:
        if not self._stdout and not self._stderr and not self._exit_delivered:
            self._exit_delivered = True
            return self._exit_status
        return ""

    def close(self) -> None:
        self._closed = True


def test_bash_returns_stdout_and_exit_code(executor: KubernetesExecutor) -> None:
    fake = _FakeExecResp(stdout_chunks=["hi\n"])
    with patch.object(executor, "_stream_pod_exec", return_value=fake):
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


def test_bash_passes_through_nonzero_exit(executor: KubernetesExecutor) -> None:
    fake = _FakeExecResp(
        stderr_chunks=["nope\n"],
        exit_status="{'status': 'Failure', 'details': {'exitCode': 7}}",
    )
    with patch.object(executor, "_stream_pod_exec", return_value=fake):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="false",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    assert result.exit_code == 7
    assert result.stderr == "nope\n"


def test_bash_invokes_bash_dash_c(executor: KubernetesExecutor) -> None:
    fake = _FakeExecResp()
    with patch.object(executor, "_stream_pod_exec", return_value=fake) as exec_mock:
        executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="ls -la",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )

    call = exec_mock.call_args
    assert call.kwargs["command"] == ["bash", "-c", "ls -la"]
    # Network-related kwargs should be absent — exec inherits the pod's locked-down namespace.
    assert "network" not in call.kwargs


def test_bash_times_out_and_kills_bash(executor: KubernetesExecutor) -> None:
    fake = _FakeExecResp()  # never delivers exit
    with (
        patch.object(executor, "_stream_pod_exec", return_value=fake),
        patch.object(executor, "_kill_processes_in_pod") as kill,
    ):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="sleep 999",
            timeout_ms=0,  # forces immediate timeout
            max_output_bytes=65_536,
        )

    assert result.timed_out is True
    assert result.exit_code is None
    kill.assert_called_once_with(f"{SESSION_NAME_PREFIX}abc", "bash")


def test_bash_rejects_non_session_id(executor: KubernetesExecutor) -> None:
    with pytest.raises(SessionNotFoundError):
        executor.execute_bash_in_session(
            "code-exec-abc",
            cmd="true",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )
    executor.v1.read_namespaced_pod.assert_not_called()


def test_bash_raises_session_not_found_on_404(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.side_effect = ApiException(status=404)
    with pytest.raises(SessionNotFoundError):
        executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}gone",
            cmd="true",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )


def test_bash_propagates_other_api_errors(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="true",
            timeout_ms=5_000,
            max_output_bytes=65_536,
        )


def test_bash_truncates_stdout_to_max_bytes(executor: KubernetesExecutor) -> None:
    fake = _FakeExecResp(stdout_chunks=["x" * 200])
    with patch.object(executor, "_stream_pod_exec", return_value=fake):
        result = executor.execute_bash_in_session(
            f"{SESSION_NAME_PREFIX}abc",
            cmd="cat big",
            timeout_ms=5_000,
            max_output_bytes=20,
        )

    assert "[truncated]" in result.stdout
    assert len(result.stdout) <= 50  # head + suffix
