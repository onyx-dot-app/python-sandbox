import codecs
import io
import logging
import os
import selectors
import shlex
import subprocess
import tarfile
import time
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Literal

from app.app_configs import (
    PYTHON_EXECUTOR_DOCKER_BIN,
    PYTHON_EXECUTOR_DOCKER_IMAGE,
    PYTHON_EXECUTOR_DOCKER_RUN_ARGS,
)
from app.services.executor_base import (
    BaseExecutor,
    ExecutionResult,
    StreamChunk,
    StreamEvent,
    StreamResult,
    WorkspaceEntry,
    wrap_last_line_interactive,
)

logger = logging.getLogger(__name__)


@dataclass
class _ExecContext:
    """Holds the live container and process for the duration of an execution."""

    container_name: str
    proc: subprocess.Popen[bytes]
    start: float


class DockerExecutor(BaseExecutor):
    def __init__(self) -> None:
        self.docker_binary = self._resolve_docker_binary()
        self.image = PYTHON_EXECUTOR_DOCKER_IMAGE
        self.run_args = PYTHON_EXECUTOR_DOCKER_RUN_ARGS

    def _resolve_docker_binary(self) -> str:
        candidate = PYTHON_EXECUTOR_DOCKER_BIN
        docker_path = which(candidate)
        if docker_path is None:
            raise RuntimeError(
                "Docker CLI not found. Set PYTHON_EXECUTOR_DOCKER_BIN to the docker binary if it is"
                " installed in a non-standard location."
            )
        return docker_path

    def _kill_container(self, name: str) -> None:
        with suppress(Exception):
            subprocess.run(
                [self.docker_binary, "kill", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def _validate_relative_path(self, path_str: str) -> Path:
        path = Path(path_str)
        if path.is_absolute():
            raise ValueError("File paths must be relative.")

        sanitized_parts = []
        for part in path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                raise ValueError("File paths must not contain '..'.")
            sanitized_parts.append(part)

        if not sanitized_parts:
            raise ValueError("File path must not be empty.")

        return Path(*sanitized_parts)

    def _create_tar_archive(
        self,
        code: str,
        files: Sequence[tuple[str, bytes]] | None = None,
        last_line_interactive: bool = True,
    ) -> bytes:
        """Create a tar archive containing the code and any additional files.

        Args:
            last_line_interactive: If True, wrap code so the last line prints its value
                                   if it's a bare expression (only the last line is affected).
        """
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            # Add __main__.py - optionally wrap in last-line-interactive mode
            code_to_execute = code
            if last_line_interactive:
                # Wrap to make the last expression value print to stdout like Jupyter/REPL
                code_to_execute = wrap_last_line_interactive(code)

            code_bytes = code_to_execute.encode("utf-8")
            code_info = tarfile.TarInfo(name="__main__.py")
            code_info.size = len(code_bytes)
            code_info.mode = 0o644
            tar.addfile(code_info, io.BytesIO(code_bytes))

            # Track directories we've created
            created_dirs = set()

            # Add any additional files
            if files:
                for file_path, content in files:
                    # Validate the path
                    validated_path = self._validate_relative_path(file_path)
                    if validated_path == Path("__main__.py"):
                        raise ValueError(
                            "File path '__main__.py' is reserved for the execution entrypoint."
                        )

                    # Create parent directories if needed
                    parent_parts = validated_path.parts[:-1]
                    for i in range(len(parent_parts)):
                        dir_path = "/".join(parent_parts[: i + 1])
                        if dir_path not in created_dirs:
                            dir_info = tarfile.TarInfo(name=dir_path + "/")
                            dir_info.type = tarfile.DIRTYPE
                            dir_info.mode = 0o755
                            tar.addfile(dir_info)
                            created_dirs.add(dir_path)

                    file_info = tarfile.TarInfo(name=validated_path.as_posix())
                    file_info.size = len(content)
                    file_info.mode = 0o644
                    tar.addfile(file_info, io.BytesIO(content))

        return tar_buffer.getvalue()

    def _extract_workspace_snapshot(self, container_name: str) -> tuple[WorkspaceEntry, ...]:
        """Extract files from the container workspace after execution using tar."""
        try:
            # Use tar to get all files from workspace (excluding __main__.py)
            tar_cmd = [
                self.docker_binary,
                "exec",
                container_name,
                "tar",
                "-c",
                "--exclude=__main__.py",
                "-C",
                "/workspace",
                ".",
            ]
            tar_result = subprocess.run(tar_cmd, capture_output=True, timeout=10)

            if tar_result.returncode != 0:
                return tuple()

            entries = []

            # Extract files from tar archive
            with tarfile.open(fileobj=io.BytesIO(tar_result.stdout), mode="r") as tar:
                for member in tar.getmembers():
                    # Skip the root directory
                    if member.name == ".":
                        continue

                    # Clean up the path (remove leading ./)
                    clean_path = member.name.lstrip("./")

                    if member.isdir():
                        entries.append(
                            WorkspaceEntry(path=clean_path, kind="directory", content=None)
                        )
                    elif member.isfile():
                        # Extract file content
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            content = file_obj.read()
                            entries.append(
                                WorkspaceEntry(path=clean_path, kind="file", content=content)
                            )

            return tuple(entries)
        except (subprocess.TimeoutExpired, Exception):
            return tuple()

    def _build_run_command(
        self,
        container_name: str,
        cpu_time_limit_sec: int | None,
        memory_limit_mb: int | None,
        timeout_ms: int,
    ) -> list[str]:
        """Build the ``docker run`` command for an ephemeral container."""
        # Start the container in detached mode
        # We need CAP_CHOWN to set up the workspace, but we'll drop privileges for execution
        cmd: list[str] = [
            self.docker_binary,
            "run",
            "-d",  # detached mode
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--name",
            container_name,
            "--cgroupns",
            "host",  # Use host cgroup namespace to avoid cgroup v2 issues in DinD
            "--pids-limit",
            "64",
            "--security-opt",
            "no-new-privileges",
            # Keep CAP_CHOWN to allow setting up workspace permissions
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,size=64m",  # noqa: S108 - intentionally constrain container tmpfs
            "--tmpfs",
            "/workspace:rw,uid=65532,gid=65532",  # Create workspace as tmpfs owned by the user
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONIOENCODING=utf-8",
            "--env",
            "MPLCONFIGDIR=/tmp/matplotlib",
        ]

        if cpu_time_limit_sec is not None:
            cpu_limit = max(int(cpu_time_limit_sec), 1)
            cmd.extend(["--ulimit", f"cpu={cpu_limit}:{cpu_limit}"])

        if memory_limit_mb is not None:
            memory_limit = max(int(memory_limit_mb), 16)
            mem_flag = f"{memory_limit}m"
            cmd.extend(["--memory", mem_flag, "--memory-swap", mem_flag])

        if self.run_args:
            cmd.extend(shlex.split(self.run_args))

        # Just sleep - workspace is already created as tmpfs with correct ownership
        cmd.extend([self.image, "sleep", str((timeout_ms * 1000) + 10)])
        return cmd

    def _stage_files_in_container(
        self,
        container_name: str,
        code: str,
        files: Sequence[tuple[str, bytes]] | None,
        last_line_interactive: bool,
    ) -> None:
        """Create a tar archive and stream it into the container workspace."""
        tar_archive = self._create_tar_archive(code, files, last_line_interactive)
        tar_cmd = [
            self.docker_binary,
            "exec",
            "-u",
            "65532:65532",
            "-i",
            container_name,
            "tar",
            "-x",
            "-C",
            "/workspace",
        ]
        tar_proc = subprocess.run(tar_cmd, input=tar_archive, capture_output=True)  # nosec B603
        if tar_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to extract files: {tar_proc.stderr.decode('utf-8', errors='replace')}"
            )

    @contextmanager
    def _run_in_container(
        self,
        *,
        code: str,
        cpu_time_limit_sec: int | None,
        memory_limit_mb: int | None,
        timeout_ms: int,
        files: Sequence[tuple[str, bytes]] | None,
        last_line_interactive: bool,
    ) -> Generator[_ExecContext, None, None]:
        """Create a container, stage files, start the Python process, and clean up.

        Yields an ``_ExecContext`` whose ``proc`` is ready for I/O (stdin is
        still open).  The container is killed in the ``finally`` block
        regardless of how the caller exits.
        """
        container_name = f"code-exec-{uuid.uuid4().hex}"

        cmd = self._build_run_command(
            container_name, cpu_time_limit_sec, memory_limit_mb, timeout_ms
        )
        start_proc = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
        if start_proc.returncode != 0:
            raise RuntimeError(f"Failed to start container: {start_proc.stderr}")

        try:
            self._stage_files_in_container(container_name, code, files, last_line_interactive)

            start = time.perf_counter()
            exec_cmd = [
                self.docker_binary,
                "exec",
                "-u",
                "65532:65532",
                "-i",
                container_name,
                "python",
                "/workspace/__main__.py",
            ]

            proc = subprocess.Popen(  # nosec B603
                exec_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )

            yield _ExecContext(
                container_name=container_name,
                proc=proc,
                start=start,
            )
        finally:
            self._kill_container(container_name)

    def execute_python(
        self,
        *,
        code: str,
        stdin: str | None,
        timeout_ms: int,
        max_output_bytes: int,
        cpu_time_limit_sec: int | None = None,
        memory_limit_mb: int | None = None,
        files: Sequence[tuple[str, bytes]] | None = None,
        last_line_interactive: bool = True,
    ) -> ExecutionResult:
        """Execute Python code inside an ephemeral Docker container with no network.

        Args:
            last_line_interactive: If True, the last line will print its value to stdout
                                   if it's a bare expression (only the last line is affected).
        """
        with self._run_in_container(
            code=code,
            cpu_time_limit_sec=cpu_time_limit_sec,
            memory_limit_mb=memory_limit_mb,
            timeout_ms=timeout_ms,
            files=files,
            last_line_interactive=last_line_interactive,
        ) as ctx:
            logger.debug(f"Executing code: {code}")

            try:
                input_bytes = stdin.encode("utf-8") if stdin is not None else None
                stdout_bytes, stderr_bytes = ctx.proc.communicate(
                    input=input_bytes,
                    timeout=timeout_ms / 1000.0,
                )
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                # Kill the Python process in the container (as root to ensure we can kill it)
                subprocess.run(
                    [
                        self.docker_binary,
                        "exec",
                        ctx.container_name,
                        "pkill",
                        "-9",
                        "python",
                    ],
                    capture_output=True,
                )
                ctx.proc.kill()
                stdout_bytes, stderr_bytes = ctx.proc.communicate()

            # Extract workspace snapshot
            workspace_snapshot = self._extract_workspace_snapshot(ctx.container_name)

        duration_ms = int((time.perf_counter() - ctx.start) * 1000)

        stdout = self.truncate_output(stdout_bytes or b"", max_output_bytes)
        logger.debug(f"stdout: {stdout}")
        stderr = self.truncate_output(stderr_bytes or b"", max_output_bytes)
        logger.debug(f"stderr: {stderr}")
        exit_code = None if timed_out else ctx.proc.returncode

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            files=workspace_snapshot,
        )

    def _terminate_process(self, ctx: _ExecContext, timed_out: bool) -> None:
        """Kill the process on timeout or wait for normal exit."""
        if timed_out:
            subprocess.run(
                [
                    self.docker_binary,
                    "exec",
                    ctx.container_name,
                    "pkill",
                    "-9",
                    "python",
                ],
                capture_output=True,
            )
            ctx.proc.kill()
        ctx.proc.wait()

    def execute_python_streaming(
        self,
        *,
        code: str,
        stdin: str | None,
        timeout_ms: int,
        max_output_bytes: int,
        cpu_time_limit_sec: int | None = None,
        memory_limit_mb: int | None = None,
        files: Sequence[tuple[str, bytes]] | None = None,
        last_line_interactive: bool = True,
    ) -> Generator[StreamEvent, None, None]:
        """Execute Python code and yield output chunks as they arrive via SSE.

        Yields StreamChunk events during execution, then a single StreamResult
        at the end containing exit_code, timing, and workspace files.
        """
        with self._run_in_container(
            code=code,
            cpu_time_limit_sec=cpu_time_limit_sec,
            memory_limit_mb=memory_limit_mb,
            timeout_ms=timeout_ms,
            files=files,
            last_line_interactive=last_line_interactive,
        ) as ctx:
            _write_stdin(ctx.proc, stdin)

            deadline = time.monotonic() + (timeout_ms / 1000.0)
            timed_out = yield from _stream_process_output(ctx.proc, deadline, max_output_bytes)

            self._terminate_process(ctx, timed_out)
            workspace_snapshot = self._extract_workspace_snapshot(ctx.container_name)

        duration_ms = int((time.perf_counter() - ctx.start) * 1000)
        exit_code = None if timed_out else ctx.proc.returncode

        yield StreamResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            files=workspace_snapshot,
        )


def _write_stdin(proc: subprocess.Popen[bytes], stdin: str | None) -> None:
    """Write optional stdin data and close the pipe."""
    if proc.stdin is None:
        raise RuntimeError("Failed to open subprocess stdin pipe")
    if stdin is not None:
        proc.stdin.write(stdin.encode("utf-8"))
    proc.stdin.close()


class _StreamTracker:
    """Per-stream state for incremental decoding with truncation."""

    __slots__ = ("stream", "decoder", "bytes_sent", "max_bytes")

    def __init__(self, stream: Literal["stdout", "stderr"], max_bytes: int) -> None:
        self.stream = stream
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.bytes_sent = 0
        self.max_bytes = max_bytes

    def decode_chunk(self, data: bytes) -> StreamChunk | None:
        """Decode a raw chunk and return a ``StreamChunk`` if within limits."""
        chunk: StreamChunk | None = None
        if self.bytes_sent < self.max_bytes:
            allowed = self.max_bytes - self.bytes_sent
            text = self.decoder.decode(data[:allowed], False)
            if text:
                chunk = StreamChunk(stream=self.stream, data=text)
        self.bytes_sent += len(data)
        return chunk

    def flush(self) -> StreamChunk | None:
        """Flush the decoder and return a final chunk if any bytes remain."""
        text = self.decoder.decode(b"", True)
        if text:
            return StreamChunk(stream=self.stream, data=text)
        return None


def _stream_process_output(
    proc: subprocess.Popen[bytes],
    deadline: float,
    max_output_bytes: int,
) -> Generator[StreamChunk, None, bool]:
    """Read stdout/stderr incrementally and yield ``StreamChunk`` events.

    Returns ``True`` if the process timed out, ``False`` otherwise.
    """
    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("Failed to open subprocess output pipes")

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

    trackers: dict[str, _StreamTracker] = {
        "stdout": _StreamTracker("stdout", max_output_bytes),
        "stderr": _StreamTracker("stderr", max_output_bytes),
    }
    fds: dict[str, int] = {
        "stdout": proc.stdout.fileno(),
        "stderr": proc.stderr.fileno(),
    }
    timed_out = False
    chunk_size = 4096

    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            events = sel.select(timeout=min(remaining, 5.0))

            for key, _ in events:
                stream_name: str = key.data
                data = os.read(fds[stream_name], chunk_size)
                if not data:
                    sel.unregister(key.fileobj)
                    continue

                chunk = trackers[stream_name].decode_chunk(data)
                if chunk is not None:
                    yield chunk
    finally:
        sel.close()

    for tracker in trackers.values():
        chunk = tracker.flush()
        if chunk is not None:
            yield chunk

    return timed_out
