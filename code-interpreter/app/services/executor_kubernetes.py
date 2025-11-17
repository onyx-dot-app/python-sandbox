from __future__ import annotations

import io
import tarfile
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from kubernetes.client import (  # type: ignore[import-untyped]
    V1Container,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
)
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]
from kubernetes.stream import ws_client  # type: ignore[import-untyped]

from app.app_configs import (
    KUBERNETES_IMAGE,
    KUBERNETES_NAMESPACE,
    KUBERNETES_SERVICE_ACCOUNT,
)
from app.services.executor_base import (
    BaseExecutor,
    ExecutionResult,
    WorkspaceEntry,
    wrap_last_line_interactive,
)
from kubernetes import client, config, stream  # type: ignore[import-untyped]


class KubernetesExecutor(BaseExecutor):
    def __init__(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

        self.v1 = client.CoreV1Api()
        self.namespace = KUBERNETES_NAMESPACE
        self.image = KUBERNETES_IMAGE
        self.service_account = KUBERNETES_SERVICE_ACCOUNT

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
        """Extract files from the pod workspace after execution using tar."""
        try:
            exec_command = ["tar", "-c", "--exclude=__main__.py", "-C", "/workspace", "."]

            resp = stream.stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                command=exec_command,
                stderr=False,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            tar_data = b""
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    tar_data += resp.read_stdout().encode("latin-1")

            resp.close()

            if not tar_data:
                return tuple()

            entries = []
            with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r") as tar:
                for member in tar.getmembers():
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
                            entries.append(
                                WorkspaceEntry(path=clean_path, kind="file", content=content)
                            )

            return tuple(entries)
        except Exception:
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

        pod_manifest = self._create_pod_manifest(
            pod_name=pod_name,
            memory_limit_mb=memory_limit_mb,
            cpu_time_limit_sec=cpu_time_limit_sec,
        )

        try:
            self.v1.create_namespaced_pod(
                namespace=self.namespace,
                body=pod_manifest,
            )

            max_wait = 30
            for _ in range(max_wait * 10):
                pod = self.v1.read_namespaced_pod(pod_name, self.namespace)
                if pod.status.phase == "Running":
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Pod {pod_name} did not become ready in {max_wait} seconds")

            tar_archive = self._create_tar_archive(code, files, last_line_interactive)

            exec_command = ["tar", "-x", "-C", "/workspace"]
            resp = stream.stream(
                self.v1.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                command=exec_command,
                stderr=True,
                stdin=True,
                stdout=False,
                tty=False,
                _preload_content=False,
            )

            commands: list[tuple[int, bytes]] = []
            commands.append((ws_client.STDIN_CHANNEL, tar_archive))
            commands.append((ws_client.STDIN_CHANNEL, b""))

            for channel, data in commands:
                if channel == ws_client.STDIN_CHANNEL:
                    channel_prefix = bytes([channel])
                    payload = channel_prefix + data
                    resp.write_stdin(payload.decode("latin-1"))

            resp.close()

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

            workspace_snapshot = self._extract_workspace_snapshot(pod_name)

        finally:
            self._cleanup_pod(pod_name)

        duration_ms = int((time.perf_counter() - start) * 1000)

        stdout = self.truncate_output(stdout_data, max_output_bytes)
        stderr = self.truncate_output(stderr_data, max_output_bytes)

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
