"""Tests that binary files with high bytes (>= 0x80) survive the tar pipeline.

Regression test for a bug where the Kubernetes executor decoded tar archives
as latin-1 text before sending through a WebSocket. The WebSocket re-encoded
as UTF-8, corrupting any byte >= 0x80 (single latin-1 bytes became multi-byte
UTF-8 sequences), which produced "tar: Skipping to next header" errors.
"""

from __future__ import annotations

import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from app.services.executor_kubernetes import KubernetesExecutor


@pytest.fixture()
def executor() -> KubernetesExecutor:
    """Create a KubernetesExecutor without calling __init__ (no cluster needed)."""
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst.namespace = "test"
    inst.image = "test:latest"
    inst.service_account = ""
    return inst


def _mock_pod_running(v1_mock: MagicMock) -> None:
    """Make read_namespaced_pod return a Running pod."""
    pod_mock = MagicMock()
    pod_mock.status.phase = "Running"
    v1_mock.read_namespaced_pod.return_value = pod_mock


def _mock_stream_resp() -> MagicMock:
    """Create a mock WebSocket stream response that closes after one iteration."""
    resp = MagicMock()
    # is_open returns True once (to enter the loop), then False
    resp.is_open.side_effect = [True, False]
    resp.peek_stdout.return_value = False
    resp.peek_stderr.return_value = False
    # Return a Success status on the error channel (= command completed OK)
    resp.read_channel.return_value = "{'status': 'Success'}"
    return resp


def test_write_stdin_receives_bytes_not_string(
    executor: KubernetesExecutor,
) -> None:
    """The critical assertion: write_stdin must be called with raw bytes,
    not a latin-1 decoded string. Passing a string causes the WebSocket
    text frame to re-encode as UTF-8, corrupting bytes >= 0x80.
    """
    _mock_pod_running(executor.v1)

    binary_content = bytes(range(0x80, 0x100))

    # Two stream calls: first for tar extraction, second for python execution
    tar_resp = _mock_stream_resp()
    exec_resp = _mock_stream_resp()

    with patch("app.services.executor_kubernetes.stream.stream") as mock_stream:
        mock_stream.side_effect = [tar_resp, exec_resp]

        executor.execute_python(
            code="print('hello')",
            stdin=None,
            timeout_ms=5000,
            max_output_bytes=1024,
            files=[("data.bin", binary_content)],
        )

    # Find the write_stdin calls on the tar extraction stream
    write_calls = tar_resp.write_stdin.call_args_list
    assert len(write_calls) >= 1, "write_stdin was never called"

    # The first call should be the tar archive data
    tar_data_arg = write_calls[0][0][0]

    assert isinstance(tar_data_arg, bytes), (
        f"write_stdin was called with {type(tar_data_arg).__name__}, expected bytes. "
        f"Passing a string causes UTF-8 re-encoding which corrupts binary tar data."
    )

    # Verify the tar archive is valid and contains our binary file
    with tarfile.open(fileobj=io.BytesIO(tar_data_arg), mode="r") as tar:
        member = next(m for m in tar.getmembers() if m.name == "data.bin")
        extracted = tar.extractfile(member)
        assert extracted is not None
        assert extracted.read() == binary_content


def test_write_stdin_string_causes_tar_corruption(
    executor: KubernetesExecutor,
) -> None:
    """Demonstrate that passing a latin-1 decoded string through a UTF-8
    WebSocket would produce a different (corrupted) byte sequence.
    """
    binary_content = bytes(range(0x80, 0x100))
    tar_bytes = executor._create_tar_archive(
        code="print('hello')",
        files=[("data.bin", binary_content)],
        last_line_interactive=False,
    )

    # This is what the old code did: decode as latin-1 to make a string
    as_string = tar_bytes.decode("latin-1")

    # The WebSocket text frame encodes strings as UTF-8
    after_websocket = as_string.encode("utf-8")

    # The byte sequences differ — this IS the corruption
    assert after_websocket != tar_bytes, (
        "latin-1→UTF-8 round-trip should corrupt bytes >= 0x80"
    )
    assert len(after_websocket) > len(tar_bytes), (
        "UTF-8 encoding expands bytes >= 0x80 into multi-byte sequences"
    )


def test_ascii_only_tar_unaffected_by_encoding(
    executor: KubernetesExecutor,
) -> None:
    """ASCII-only archives survive latin-1→UTF-8, explaining why the bug
    only triggered with binary file uploads.
    """
    tar_bytes = executor._create_tar_archive(
        code="print('hello')",
        files=[("readme.txt", b"just ascii\n")],
        last_line_interactive=False,
    )

    roundtripped = tar_bytes.decode("latin-1").encode("utf-8")

    assert roundtripped == tar_bytes, (
        "ASCII-only tar archives should be unaffected by latin-1→UTF-8"
    )
