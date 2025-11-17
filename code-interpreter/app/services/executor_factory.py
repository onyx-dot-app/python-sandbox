from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

from app.app_configs import EXECUTOR_BACKEND
from app.services.executor_base import BaseExecutor, ExecutionResult


@lru_cache(maxsize=1)
def get_executor() -> BaseExecutor:
    """Get the appropriate executor based on configuration."""
    backend = EXECUTOR_BACKEND.lower()

    if backend == "docker":
        from app.services.executor_docker import DockerExecutor

        return DockerExecutor()
    elif backend == "kubernetes":
        from app.services.executor_kubernetes import KubernetesExecutor

        return KubernetesExecutor()
    else:
        raise ValueError(f"Unknown executor backend: {backend}")


def execute_python(
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
    """Execute Python code using the configured backend.

    Args:
        last_line_interactive: If True, the last line will print its value to stdout
                               if it's a bare expression (only the last line is affected).
    """
    executor = get_executor()
    return executor.execute_python(
        code=code,
        stdin=stdin,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        cpu_time_limit_sec=cpu_time_limit_sec,
        memory_limit_mb=memory_limit_mb,
        files=files,
        last_line_interactive=last_line_interactive,
    )
