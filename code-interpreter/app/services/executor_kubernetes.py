from __future__ import annotations

import base64
import io
import logging
import tarfile
import time
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kubernetes import client, config, stream  # type: ignore
from kubernetes.client import (  # type: ignore[import-untyped]
    V1Container,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
)
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]
from kubernetes.stream import ws_client  # type: ignore[import-untyped]

from app.app_configs import (
    KUBERNETES_EXECUTOR_IMAGE,
    KUBERNETES_EXECUTOR_NAMESPACE,
    KUBERNETES_EXECUTOR_SERVICE_ACCOUNT,
)
from app.services.executor_base import (
    BaseExecutor,
    EntryKind,
    ExecutionResult,
    HealthCheck,
    StreamChunk,
    StreamEvent,
    StreamResult,
    WorkspaceEntry,
    wrap_last_line_interactive,
)

logger = logging.getLogger(__name__)


def _parse_exit_code(error: str) -> int | None:
    """Parse the exit code from a Kubernetes exec error channel message."""
    try:
        error_dict = eval(error)  # noqa: S307
        if isinstance(error_dict, dict) and "status" in error_dict:
            if error_dict["status"] == "Success":
                return 0
            details = error_dict.get("details", {})
            if isinstance(details, dict) and "exitCode" in details:
                return int(details["exitCode"])
            return 1
    except Exception as e:
        logger.warning(f"Error occurred when parsing exit code: {e}")
        return None
    return None


@dataclass
class _KubeExecContext:
    """Holds the live pod and exec stream for the duration of an execution."""

    pod_name: str
    exec_resp: ws_client.WSClient
    start: float


class KubernetesExecutor(BaseExecutor):
    def __init__(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        self.v1 = client.CoreV1Api()
        self.namespace = KUBERNETES_EXECUTOR_NAMESPACE
        self.image = KUBERNETES_EXECUTOR_IMAGE
        self.service_account = KUBERNETES_EXECUTOR_SERVICE_ACCOUNT

    def check_health(self) -> HealthCheck:
        """Verify Kubernetes API is reachable and we can create pods in the namespace."""
        try:
            auth_api = client.AuthorizationV1Api()
            review = auth_api.create_self_subject_access_review(
                body=client.V1SelfSubjectAccessReview(
                    spec=client.V1SelfSubjectAccessReviewSpec(
                        resource_attributes=client.V1ResourceAttributes(
                            namespace=self.namespace,
                            verb="create",
                            resource="pods",
                        )
                    )
                )
            )
            if not review.status.allowed:
                reason = review.status.reason or "no reason provided"
                logger.warning(
                    f"Health check failed: cannot create pods in namespace={self.namespace} "
                    f"(reason={reason})"
                )
                return HealthCheck(
                    status="error",
                    message=f"Service account lacks permission to create pods in namespace={self.namespace}",
                )
        except ApiException as e:
            return HealthCheck(
                status="error",
                message=f"Kubernetes API error (namespace={self.namespace}): {e.reason}",
            )
        except Exception as e:
            return HealthCheck(
                status="error",
                message=f"Kubernetes API not reachable: {e}",
            )
        return HealthCheck(status="ok")

    def _create_pod_manifest(
        self,
        pod_name: str,
        memory_limit_mb: int | None,
        cpu_time_limit_sec: int | None,
    ) -> V1Pod:
        """Create a Kubernetes pod manifest for code execution."""

        resources: dict[str, dict[str, Any]] = {
            "limits": {},
            "requests": {},
        }

        if memory_limit_mb is not None:
            memory_limit = max(int(memory_limit_mb), 16)
            resources["limits"]["memory"] = f"{memory_limit}Mi"
            resources["requests"]["memory"] = f"{min(memory_limit, 64)}Mi"

        if cpu_time_limit_sec is not None:
            cpu_limit = max(int(cpu_time_limit_sec), 1)
            resources["limits"]["cpu"] = str(cpu_limit)
            resources["requests"]["cpu"] = "100m"

        container = V1Container(
            name="executor",
            image=self.image,
            command=["sleep", "3600"],
            working_dir="/workspace",
            resources=resources if resources["limits"] else None,
            security_context={
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": False,
                "capabilities": {"drop": ["ALL"]},
            },
            env=[
                {"name": "PYTHONUNBUFFERED", "value": "1"},
                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                {"name": "PYTHONIOENCODING", "value": "utf-8"},
                {"name": "MPLCONFIGDIR", "value": "/tmp/matplotlib"},  # noqa: S108
            ],
            volume_mounts=[
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108
            ],
        )

        spec = V1PodSpec(
            containers=[container],
            restart_policy="Never",
            service_account_name=self.service_account if self.service_account else None,
            volumes=[
                {"name": "workspace", "emptyDir": {"sizeLimit": "100Mi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
            ],
            security_context={
                "runAsNonRoot": True,
                "fsGroup": 65532,
            },
        )

        metadata = V1ObjectMeta(
            name=pod_name,
            namespace=self.namespace,
            labels={
                "app": "code-interpreter",
                "component": "executor",
            },
        )

        return V1Pod(api_version="v1", kind="Pod", metadata=metadata, spec=spec)

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
            code_info.uid = 65532
            code_info.gid = 65532
            tar.addfile(code_info, io.BytesIO(code_bytes))

            created_dirs = set()

            if files:
                for file_path, content in files:
                    validated_path = self._validate_relative_path(file_path)
                    if validated_path == Path("__main__.py"):
                        raise ValueError(
                            "File path '__main__.py' is reserved for the execution entrypoint."
                        )

                    parent_parts = validated_path.parts[:-1]
                    for i in range(len(parent_parts)):
                        dir_path = "/".join(parent_parts[: i + 1])
                        if dir_path not in created_dirs:
                            dir_info = tarfile.TarInfo(name=dir_path + "/")
                            dir_info.type = tarfile.DIRTYPE
                            dir_info.mode = 0o755
                            dir_info.uid = 65532
                            dir_info.gid = 65532
                            tar.addfile(dir_info)
                            created_dirs.add(dir_path)

                    file_info = tarfile.TarInfo(name=validated_path.as_posix())
                    file_info.size = len(content)
                    file_info.mode = 0o644
                    file_info.uid = 65532
                    file_info.gid = 65532
                    tar.addfile(file_info, io.BytesIO(content))

        return tar_buffer.getvalue()

    def _wait_for_pod_ready(self, pod_name: str, timeout_sec: int = 30) -> None:
        """Wait for a pod to reach Running state."""
        logger.info(f"Waiting for pod {pod_name} to be ready")
        for _ in range(timeout_sec * 10):
            pod = self.v1.read_namespaced_pod(pod_name, self.namespace)
            if pod.status.phase == "Running":
                logger.info(f"Pod {pod_name} is running")
                return
            time.sleep(0.1)
        raise RuntimeError(f"Pod {pod_name} did not become ready in {timeout_sec} seconds")

    def _upload_tar_to_pod(self, pod_name: str, tar_archive: bytes) -> None:
        """Upload and extract a tar archive into the pod's workspace."""
        logger.info(f"Uploading tar archive ({len(tar_archive)} bytes) to pod {pod_name}")
        exec_command = ["tar", "-x", "-C", "/workspace"]
        resp = stream.stream(
            self.v1.connect_get_namespaced_pod_exec,
            pod_name,
            self.namespace,
            command=exec_command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

        resp.write_stdin(tar_archive)
        resp.write_stdin(b"")

        tar_stderr = b""
        tar_exit_code: int | None = None

        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_chunk: str = resp.read_stdout()
                logger.debug(f"Tar stdout: {stdout_chunk}")
            if resp.peek_stderr():
                stderr_chunk: str = resp.read_stderr()
                tar_stderr += stderr_chunk.encode("utf-8")
                logger.warning(f"Tar stderr: {stderr_chunk}")

            error: str = resp.read_channel(ws_client.ERROR_CHANNEL)
            if error:
                logger.debug(f"Tar command error channel: {error}")
                tar_exit_code = _parse_exit_code(error)
                break

        resp.close()
        logger.info(f"Tar extraction completed with exit code: {tar_exit_code}")

        if tar_exit_code is None:
            raise RuntimeError("Tar extraction command did not complete")
        if tar_exit_code != 0:
            raise RuntimeError(
                f"Tar extraction failed with exit code {tar_exit_code}. "
                f"stderr: {tar_stderr.decode('utf-8', errors='replace')}"
            )

    def _kill_python_process(self, pod_name: str) -> None:
        """Kill the Python process running in the pod."""
        with suppress(Exception):
            stream.stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                command=["pkill", "-9", "python"],
                stderr=False,
                stdin=False,
                stdout=False,
                tty=False,
            )

    @contextmanager
    def _run_in_pod(
        self,
        *,
        code: str,
        cpu_time_limit_sec: int | None,
        memory_limit_mb: int | None,
        files: Sequence[tuple[str, bytes]] | None,
        last_line_interactive: bool,
    ) -> Generator[_KubeExecContext, None, None]:
        """Create a pod, stage files, open Python exec stream, and clean up.

        Yields a _KubeExecContext whose exec_resp is ready for stdin/stdout I/O.
        The pod is deleted in the finally block regardless of how the caller exits.
        """
        pod_name = f"code-exec-{uuid.uuid4().hex}"
        logger.info(f"Starting execution in pod {pod_name}")
        logger.debug(
            f"Code to execute: {code[:100]}..." if len(code) > 100 else f"Code to execute: {code}"
        )

        pod_manifest = self._create_pod_manifest(
            pod_name=pod_name,
            memory_limit_mb=memory_limit_mb,
            cpu_time_limit_sec=cpu_time_limit_sec,
        )

        try:
            logger.info(f"Creating pod {pod_name} in namespace {self.namespace}")
            self.v1.create_namespaced_pod(
                namespace=self.namespace,
                body=pod_manifest,
            )

            self._wait_for_pod_ready(pod_name)

            tar_archive = self._create_tar_archive(code, files, last_line_interactive)
            self._upload_tar_to_pod(pod_name, tar_archive)

            logger.info(f"Executing Python code in pod {pod_name}")
            start = time.perf_counter()
            exec_command = ["python", "/workspace/__main__.py"]

            exec_resp = stream.stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                command=exec_command,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            yield _KubeExecContext(
                pod_name=pod_name,
                exec_resp=exec_resp,
                start=start,
            )
        except Exception as e:
            logger.error(f"Error during execution in pod {pod_name}: {e}", exc_info=True)
            raise
        finally:
            logger.info(f"Cleaning up pod {pod_name}")
            self._cleanup_pod(pod_name)

    def _extract_workspace_snapshot(self, pod_name: str) -> tuple[WorkspaceEntry, ...]:
        """Extract files from the pod workspace after execution using tar.

        Uses base64 encoding to safely transmit binary tar data through the
        text-based Kubernetes WebSocket stream.
        """
        try:
            # Use base64 to encode the tar output so it can safely pass through
            # the text-based WebSocket stream without corruption
            exec_command = [
                "sh",
                "-c",
                "tar -c --exclude=__main__.py -C /workspace . | base64",
            ]

            logger.info(f"Starting tar extraction from pod {pod_name}")
            resp = stream.stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            base64_data = ""
            stderr_data = ""

            while resp.is_open():
                resp.update(timeout=1)

                if resp.peek_stdout():
                    base64_data += resp.read_stdout()

                if resp.peek_stderr():
                    stderr_data += resp.read_stderr()

            resp.close()

            logger.info(f"Tar extraction complete. Received {len(base64_data)} base64 chars")
            if stderr_data:
                logger.warning(f"Tar extraction stderr: {stderr_data}")

            if not base64_data:
                logger.warning("No tar data received from workspace snapshot")
                return tuple()

            # Decode base64 to get the original tar binary data
            tar_data = base64.b64decode(base64_data)
            logger.info(f"Decoded to {len(tar_data)} bytes of tar data")

            entries = []
            logger.info("Parsing tar archive")
            with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r") as tar:
                members = tar.getmembers()
                logger.info(f"Tar archive contains {len(members)} members")
                for member in members:
                    logger.debug(
                        f"Processing tar member: {member.name!r} (type={member.type!r}, "
                        f"size={member.size})"
                    )
                    if member.name == ".":
                        continue

                    clean_path = member.name.lstrip("./")

                    if member.isdir():
                        entries.append(
                            WorkspaceEntry(path=clean_path, kind=EntryKind.DIRECTORY, content=None)
                        )
                    elif member.isfile():
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            content = file_obj.read()
                            logger.debug(f"Extracted file {clean_path}: {len(content)} bytes")
                            entries.append(
                                WorkspaceEntry(
                                    path=clean_path, kind=EntryKind.FILE, content=content
                                )
                            )
                        else:
                            logger.warning(f"Failed to extract file content for {clean_path}")

            logger.info(f"Extracted {len(entries)} workspace entries")
            return tuple(entries)
        except Exception as e:
            logger.error(f"Failed to extract workspace snapshot: {e}", exc_info=True)
            return tuple()

    def _cleanup_pod(self, pod_name: str) -> None:
        """Delete a pod and wait for cleanup."""
        with suppress(ApiException):
            self.v1.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )

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
        """Execute Python code inside a Kubernetes pod.

        Args:
            last_line_interactive: If True, the last line will print its value to stdout
                                   if it's a bare expression (only the last line is affected).
        """
        with self._run_in_pod(
            code=code,
            cpu_time_limit_sec=cpu_time_limit_sec,
            memory_limit_mb=memory_limit_mb,
            files=files,
            last_line_interactive=last_line_interactive,
        ) as ctx:
            if stdin:
                logger.debug("Writing stdin to Python process")
                ctx.exec_resp.write_stdin(stdin)

            stdout_data = b""
            stderr_data = b""
            exit_code: int | None = None
            timed_out = False

            timeout_sec = timeout_ms / 1000.0
            end_time = time.time() + timeout_sec

            while ctx.exec_resp.is_open():
                remaining = end_time - time.time()
                if remaining <= 0:
                    timed_out = True
                    break

                ctx.exec_resp.update(timeout=min(remaining, 1))

                if ctx.exec_resp.peek_stdout():
                    stdout_data += ctx.exec_resp.read_stdout().encode("utf-8")

                if ctx.exec_resp.peek_stderr():
                    stderr_data += ctx.exec_resp.read_stderr().encode("utf-8")

                error = ctx.exec_resp.read_channel(ws_client.ERROR_CHANNEL)
                if error:
                    exit_code = _parse_exit_code(error)
                    break

            ctx.exec_resp.close()

            if timed_out:
                self._kill_python_process(ctx.pod_name)

            logger.info(
                f"Python execution completed. Exit code: {exit_code}, Timed out: {timed_out}"
            )
            logger.debug(f"stdout length: {len(stdout_data)}, stderr length: {len(stderr_data)}")

            logger.info(f"Extracting workspace snapshot from pod {ctx.pod_name}")
            workspace_snapshot = self._extract_workspace_snapshot(ctx.pod_name)
            logger.debug(f"Workspace snapshot has {len(workspace_snapshot)} entries")

        duration_ms = int((time.perf_counter() - ctx.start) * 1000)

        stdout = self.truncate_output(stdout_data, max_output_bytes)
        stderr = self.truncate_output(stderr_data, max_output_bytes)

        logger.info(f"Execution completed in {duration_ms}ms")
        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code if not timed_out else None,
            timed_out=timed_out,
            duration_ms=duration_ms,
            files=workspace_snapshot,
        )

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
        """Execute Python code and yield output chunks as they arrive.

        Yields StreamChunk events during execution, then a single StreamResult
        at the end containing exit_code, timing, and workspace files.
        """
        with self._run_in_pod(
            code=code,
            cpu_time_limit_sec=cpu_time_limit_sec,
            memory_limit_mb=memory_limit_mb,
            files=files,
            last_line_interactive=last_line_interactive,
        ) as ctx:
            if stdin:
                logger.debug("Writing stdin to Python process")
                ctx.exec_resp.write_stdin(stdin)

            deadline = time.time() + (timeout_ms / 1000.0)
            exit_code, timed_out = yield from _stream_kube_output(
                ctx.exec_resp, deadline, max_output_bytes
            )

            if timed_out:
                self._kill_python_process(ctx.pod_name)

            workspace_snapshot = self._extract_workspace_snapshot(ctx.pod_name)

        duration_ms = int((time.perf_counter() - ctx.start) * 1000)
        yield StreamResult(
            exit_code=exit_code if not timed_out else None,
            timed_out=timed_out,
            duration_ms=duration_ms,
            files=workspace_snapshot,
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


def _stream_kube_output(
    exec_resp: ws_client.WSClient,
    deadline: float,
    max_output_bytes: int,
) -> Generator[StreamChunk, None, tuple[int | None, bool]]:
    """Read stdout/stderr from a Kubernetes exec stream and yield StreamChunk events.

    Returns a (exit_code, timed_out) tuple.
    """
    stdout_bytes = 0
    stderr_bytes = 0
    exit_code: int | None = None
    timed_out = False

    while exec_resp.is_open():
        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            break

        exec_resp.update(timeout=min(remaining, 1))

        if exec_resp.peek_stdout():
            text: str = exec_resp.read_stdout()
            raw = text.encode("utf-8")
            if stdout_bytes < max_output_bytes:
                allowed = max_output_bytes - stdout_bytes
                if len(raw) > allowed:
                    text = raw[:allowed].decode("utf-8", errors="ignore")
                if text:
                    yield StreamChunk(stream="stdout", data=text)
            stdout_bytes += len(raw)

        if exec_resp.peek_stderr():
            text = exec_resp.read_stderr()
            raw = text.encode("utf-8")
            if stderr_bytes < max_output_bytes:
                allowed = max_output_bytes - stderr_bytes
                if len(raw) > allowed:
                    text = raw[:allowed].decode("utf-8", errors="ignore")
                if text:
                    yield StreamChunk(stream="stderr", data=text)
            stderr_bytes += len(raw)

        error: str = exec_resp.read_channel(ws_client.ERROR_CHANNEL)
        if error:
            exit_code = _parse_exit_code(error)
            break

    exec_resp.close()
    return exit_code, timed_out
