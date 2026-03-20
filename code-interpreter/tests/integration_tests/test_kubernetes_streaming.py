"""Tests for KubernetesExecutor.execute_python_streaming.

Unit tests that mock the Kubernetes API to exercise the streaming
execution path without requiring a real cluster.
"""

from __future__ import annotations

import base64
import io
import logging
import tarfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.services.executor_base import StreamChunk, StreamEvent, StreamResult
from app.services.executor_kubernetes import KubernetesExecutor

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor() -> KubernetesExecutor:
    """Create a KubernetesExecutor bypassing __init__ (no cluster needed)."""
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst.namespace = "test"
    inst.image = "test:latest"
    inst.service_account = ""
    pod_mock = MagicMock()
    pod_mock.status.phase = "Running"

    def _read_namespaced_pod(*args: object, **kwargs: object) -> MagicMock:
        if inst.v1.delete_namespaced_pod.called:
            raise ApiException(status=404)
        return pod_mock

    inst.v1.read_namespaced_pod.side_effect = _read_namespaced_pod
    return inst


class FakeExecResp:
    """Simulates a Kubernetes WebSocket exec stream.

    Delivers *stdout_chunks* and *stderr_chunks* one per loop iteration.
    Once both queues are drained, the *exit_status* is returned on the
    error channel, causing the reader to break out of the loop.
    """

    def __init__(
        self,
        stdout_chunks: list[str] | None = None,
        stderr_chunks: list[str] | None = None,
        exit_status: str = "{'status': 'Success'}",
    ) -> None:
        self._stdout = list(stdout_chunks or [])
        self._stderr = list(stderr_chunks or [])
        self._exit_status = exit_status
        self._closed = False
        self._exit_delivered = False
        self.stdin_writes: list[Any] = []

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

    def read_channel(self, channel: int) -> str:  # noqa: ARG002
        if not self._stdout and not self._stderr and not self._exit_delivered:
            self._exit_delivered = True
            return self._exit_status
        return ""

    def write_stdin(self, data: str | bytes) -> None:
        self.stdin_writes.append(data)

    def close(self) -> None:
        self._closed = True


def _make_tar_mock() -> MagicMock:
    """Mock for the tar-upload exec stream (succeeds immediately)."""
    resp = MagicMock()
    resp.is_open.side_effect = [True, False]
    resp.peek_stdout.return_value = False
    resp.peek_stderr.return_value = False
    resp.read_channel.return_value = "{'status': 'Success'}"
    return resp


def _make_snapshot_mock(files: dict[str, bytes] | None = None) -> MagicMock:
    """Mock for workspace-snapshot exec stream."""
    resp = MagicMock()
    if not files:
        resp.is_open.side_effect = [False]
        resp.peek_stdout.return_value = False
        resp.peek_stderr.return_value = False
        return resp

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    resp.is_open.side_effect = [True, False]
    resp.peek_stdout.side_effect = [True, False]
    resp.read_stdout.return_value = b64
    resp.peek_stderr.return_value = False
    return resp


def _run_streaming(
    executor: KubernetesExecutor,
    exec_resp: FakeExecResp,
    *,
    extra_stream_mocks: list[Any] | None = None,
    snapshot_files: dict[str, bytes] | None = None,
    **kwargs: object,
) -> list[StreamEvent]:
    """Run execute_python_streaming with mocked Kubernetes streams."""
    mocks: list[Any] = [_make_tar_mock(), exec_resp]
    if extra_stream_mocks:
        mocks.extend(extra_stream_mocks)
    mocks.append(_make_snapshot_mock(snapshot_files))

    defaults: dict[str, Any] = {
        "code": "print('hello')",
        "stdin": None,
        "timeout_ms": 5000,
        "max_output_bytes": 65536,
    }
    defaults.update(kwargs)

    with patch("app.services.executor_kubernetes.stream.stream") as mock_stream:
        mock_stream.side_effect = mocks
        return list(executor.execute_python_streaming(**defaults))


def _chunks(events: list[StreamEvent]) -> list[StreamChunk]:
    return [e for e in events if isinstance(e, StreamChunk)]


def _result(events: list[StreamEvent]) -> StreamResult:
    results = [e for e in events if isinstance(e, StreamResult)]
    assert len(results) == 1, f"Expected exactly 1 StreamResult, got {len(results)}"
    return results[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_streaming_yields_stdout_chunks(executor: KubernetesExecutor) -> None:
    events = _run_streaming(executor, FakeExecResp(stdout_chunks=["hello\n"]))

    chunks = _chunks(events)
    assert len(chunks) == 1
    assert chunks[0] == StreamChunk(stream="stdout", data="hello\n")

    result = _result(events)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.duration_ms >= 0


def test_streaming_yields_stderr_chunks(executor: KubernetesExecutor) -> None:
    events = _run_streaming(executor, FakeExecResp(stderr_chunks=["oops\n"]))

    chunks = _chunks(events)
    assert len(chunks) == 1
    assert chunks[0] == StreamChunk(stream="stderr", data="oops\n")


def test_streaming_multiple_stdout_chunks(executor: KubernetesExecutor) -> None:
    events = _run_streaming(executor, FakeExecResp(stdout_chunks=["line1\n", "line2\n"]))

    chunks = _chunks(events)
    assert len(chunks) == 2
    assert chunks[0].data == "line1\n"
    assert chunks[1].data == "line2\n"
    assert all(c.stream == "stdout" for c in chunks)


def test_streaming_mixed_stdout_and_stderr(executor: KubernetesExecutor) -> None:
    events = _run_streaming(
        executor, FakeExecResp(stdout_chunks=["out\n"], stderr_chunks=["err\n"])
    )

    chunks = _chunks(events)
    stdout = [c for c in chunks if c.stream == "stdout"]
    stderr = [c for c in chunks if c.stream == "stderr"]

    assert len(stdout) == 1
    assert stdout[0].data == "out\n"
    assert len(stderr) == 1
    assert stderr[0].data == "err\n"


def test_streaming_nonzero_exit_code(executor: KubernetesExecutor) -> None:
    events = _run_streaming(
        executor,
        FakeExecResp(
            stderr_chunks=["error!\n"],
            exit_status="{'status': 'Failure', 'details': {'exitCode': 1}}",
        ),
    )

    result = _result(events)
    assert result.exit_code == 1
    assert result.timed_out is False


def test_streaming_timeout(executor: KubernetesExecutor) -> None:
    """timeout_ms=0 guarantees immediate timeout."""
    events = _run_streaming(
        executor,
        FakeExecResp(),
        extra_stream_mocks=[MagicMock()],  # _kill_python_process
        timeout_ms=0,
    )

    assert _chunks(events) == []
    result = _result(events)
    assert result.exit_code is None
    assert result.timed_out is True


def test_streaming_timeout_calls_kill(executor: KubernetesExecutor) -> None:
    """Verify _kill_python_process is invoked on timeout."""
    exec_resp = FakeExecResp()

    with patch("app.services.executor_kubernetes.stream.stream") as mock_stream:
        mock_stream.side_effect = [
            _make_tar_mock(),
            exec_resp,
            MagicMock(),  # kill
            _make_snapshot_mock(),
        ]
        list(
            executor.execute_python_streaming(
                code="import time; time.sleep(999)",
                stdin=None,
                timeout_ms=0,
                max_output_bytes=65536,
            )
        )

    kill_calls = [
        c
        for c in mock_stream.call_args_list
        if c.kwargs.get("command") == ["pkill", "-9", "python"]
    ]
    assert len(kill_calls) == 1


def test_streaming_truncates_stdout(executor: KubernetesExecutor) -> None:
    """A single chunk exceeding the byte budget is truncated."""
    events = _run_streaming(
        executor,
        FakeExecResp(stdout_chunks=["hello world"]),
        max_output_bytes=5,
    )

    chunks = _chunks(events)
    assert len(chunks) == 1
    assert chunks[0].data == "hello"
    assert chunks[0].stream == "stdout"


def test_streaming_suppresses_chunks_past_limit(
    executor: KubernetesExecutor,
) -> None:
    """Once the byte budget is exhausted, further chunks are not yielded."""
    events = _run_streaming(
        executor,
        FakeExecResp(stdout_chunks=["aaa", "bbb"]),
        max_output_bytes=3,
    )

    chunks = _chunks(events)
    assert len(chunks) == 1
    assert chunks[0].data == "aaa"


def test_streaming_forwards_stdin(executor: KubernetesExecutor) -> None:
    exec_resp = FakeExecResp(stdout_chunks=["echoed\n"])
    _run_streaming(executor, exec_resp, stdin="input data")

    assert exec_resp.stdin_writes == ["input data"]


def test_streaming_includes_workspace_files(
    executor: KubernetesExecutor,
) -> None:
    events = _run_streaming(
        executor,
        FakeExecResp(),
        snapshot_files={"output.txt": b"file content"},
    )

    result = _result(events)
    assert len(result.files) == 1
    assert result.files[0].path == "output.txt"
    assert result.files[0].content == b"file content"


def test_streaming_cleans_up_pod(executor: KubernetesExecutor) -> None:
    """Pod is deleted via _cleanup_pod regardless of outcome."""
    _run_streaming(executor, FakeExecResp())

    executor.v1.delete_namespaced_pod.assert_called_once()


def test_cleanup_retries_delete_failures(
    executor: KubernetesExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    executor.v1.delete_namespaced_pod.side_effect = [
        ApiException(status=500, reason="boom"),
        None,
    ]
    executor.v1.read_namespaced_pod.side_effect = [
        MagicMock(),
        ApiException(status=404),
    ]

    with caplog.at_level(logging.WARNING):
        executor._cleanup_pod("code-exec-test")

    assert executor.v1.delete_namespaced_pod.call_count == 2
    assert "Failed to delete pod code-exec-test" in caplog.text


def test_cleanup_logs_when_delete_never_succeeds(
    executor: KubernetesExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    executor.v1.delete_namespaced_pod.side_effect = ApiException(status=500, reason="boom")

    with caplog.at_level(logging.WARNING):
        executor._cleanup_pod("code-exec-test")

    assert executor.v1.delete_namespaced_pod.call_count == 3
    assert "Failed to delete pod code-exec-test" in caplog.text
    assert "Failed to confirm deletion of pod code-exec-test" in caplog.text


def test_stream_exec_uses_fresh_api_client(executor: KubernetesExecutor) -> None:
    with (
        patch("app.services.executor_kubernetes.client.ApiClient") as api_client_cls,
        patch("app.services.executor_kubernetes.client.CoreV1Api") as core_v1_cls,
        patch("app.services.executor_kubernetes.stream.stream") as mock_stream,
    ):
        stream_api = MagicMock()
        core_v1_cls.return_value = stream_api

        executor._stream_pod_exec(
            "code-exec-test",
            ["python", "/workspace/__main__.py"],
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
        )

    api_client_cls.assert_called_once()
    core_v1_cls.assert_called_once_with(api_client=api_client_cls.return_value)
    assert mock_stream.call_args.args[0] is stream_api.connect_get_namespaced_pod_exec
    assert mock_stream.call_args.kwargs["_preload_content"] is False


def test_streaming_empty_output(executor: KubernetesExecutor) -> None:
    events = _run_streaming(executor, FakeExecResp())

    assert _chunks(events) == []
    result = _result(events)
    assert result.exit_code == 0
    assert result.timed_out is False


def test_streaming_always_ends_with_result(
    executor: KubernetesExecutor,
) -> None:
    """The last event yielded must always be a StreamResult."""
    events = _run_streaming(executor, FakeExecResp(stdout_chunks=["data\n"]))

    assert len(events) >= 1
    assert isinstance(events[-1], StreamResult)
