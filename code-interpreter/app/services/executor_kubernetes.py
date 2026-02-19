from __future__ import annotations

import base64
import io
import logging
import tarfile
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
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
    ExecutionResult,
    WorkspaceEntry,
    wrap_last_line_interactive,
)

logger = logging.getLogger(__name__)


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
                            WorkspaceEntry(path=clean_path, kind="directory", content=None)
                        )
                    elif member.isfile():
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            content = file_obj.read()
                            logger.debug(f"Extracted file {clean_path}: {len(content)} bytes")
                            entries.append(
                                WorkspaceEntry(path=clean_path, kind="file", content=content)
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

            logger.info(f"Waiting for pod {pod_name} to be ready")
            max_wait = 30
            for _ in range(max_wait * 10):
                pod = self.v1.read_namespaced_pod(pod_name, self.namespace)
                if pod.status.phase == "Running":
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Pod {pod_name} did not become ready in {max_wait} seconds")

            logger.info(f"Pod {pod_name} is running, creating tar archive")
            tar_archive = self._create_tar_archive(code, files, last_line_interactive)
            logger.debug(f"Tar archive size: {len(tar_archive)} bytes")

            logger.info(f"Executing tar extraction in pod {pod_name}")
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

            # Write tar archive to stdin as raw bytes
            logger.debug("Writing tar archive to stdin")
            resp.write_stdin(tar_archive)
            # Signal end of input
            logger.debug("Closing stdin")
            resp.write_stdin(b"")

            # Wait for tar extraction to complete by reading until the stream closes
            logger.debug("Waiting for tar extraction to complete")
            tar_stderr = b""
            tar_stdout = b""
            tar_exit_code: int | None = None

            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    chunk = resp.read_stdout().encode("utf-8")
                    tar_stdout += chunk
                    logger.debug(f"Tar stdout: {chunk}")
                if resp.peek_stderr():
                    chunk = resp.read_stderr().encode("utf-8")
                    tar_stderr += chunk
                    logger.warning(f"Tar stderr: {chunk}")

                # Check for command completion
                error = resp.read_channel(ws_client.ERROR_CHANNEL)
                if error:
                    logger.debug(f"Tar command error channel: {error}")
                    try:
                        error_dict = eval(error)  # noqa: S307
                        if isinstance(error_dict, dict) and "status" in error_dict:
                            if error_dict["status"] == "Success":
                                tar_exit_code = 0
                            elif "details" in error_dict and "exitCode" in error_dict["details"]:
                                tar_exit_code = error_dict["details"]["exitCode"]
                            else:
                                tar_exit_code = 1
                    except Exception as e:  # noqa: S110
                        logger.error(f"Failed to parse error channel: {e}")
                    break

            resp.close()
            logger.info(f"Tar extraction completed with exit code: {tar_exit_code}")

            # Check if tar extraction failed
            if tar_exit_code is None:
                raise RuntimeError("Tar extraction command did not complete")
            if tar_exit_code != 0:
                raise RuntimeError(
                    f"Tar extraction failed with exit code {tar_exit_code}. "
                    f"stderr: {tar_stderr.decode('utf-8', errors='replace')}"
                )

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

            if stdin:
                logger.debug("Writing stdin to Python process")
                exec_resp.write_stdin(stdin)

            stdout_data = b""
            stderr_data = b""
            exit_code: int | None = None
            timed_out = False

            timeout_sec = timeout_ms / 1000.0
            end_time = time.time() + timeout_sec

            while exec_resp.is_open():
                remaining = end_time - time.time()
                if remaining <= 0:
                    timed_out = True
                    break

                exec_resp.update(timeout=min(remaining, 1))

                if exec_resp.peek_stdout():
                    stdout_data += exec_resp.read_stdout().encode("utf-8")

                if exec_resp.peek_stderr():
                    stderr_data += exec_resp.read_stderr().encode("utf-8")

                error = exec_resp.read_channel(ws_client.ERROR_CHANNEL)
                if error:
                    try:
                        error_dict = eval(error)  # noqa: S307
                        if isinstance(error_dict, dict) and "status" in error_dict:
                            status = error_dict["status"]
                            if status == "Success":
                                exit_code = 0
                            elif (
                                "reason" in error_dict and error_dict["reason"] == "NonZeroExitCode"
                            ):
                                if "details" in error_dict and "exitCode" in error_dict["details"]:
                                    exit_code = error_dict["details"]["exitCode"]
                                else:
                                    exit_code = 1
                            break
                    except Exception:  # noqa: S110
                        pass

            exec_resp.close()

            if timed_out:
                exec_command = ["pkill", "-9", "python"]
                with suppress(Exception):
                    stream.stream(
                        self.v1.connect_get_namespaced_pod_exec,
                        pod_name,
                        self.namespace,
                        command=exec_command,
                        stderr=False,
                        stdin=False,
                        stdout=False,
                        tty=False,
                    )

            logger.info(
                f"Python execution completed. Exit code: {exit_code}, Timed out: {timed_out}"
            )
            logger.debug(f"stdout length: {len(stdout_data)}, stderr length: {len(stderr_data)}")

            logger.info(f"Extracting workspace snapshot from pod {pod_name}")
            workspace_snapshot = self._extract_workspace_snapshot(pod_name)
            logger.debug(f"Workspace snapshot has {len(workspace_snapshot)} entries")

        except Exception as e:
            logger.error(f"Error during execution in pod {pod_name}: {e}", exc_info=True)
            raise
        finally:
            logger.info(f"Cleaning up pod {pod_name}")
            self._cleanup_pod(pod_name)

        duration_ms = int((time.perf_counter() - start) * 1000)

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
