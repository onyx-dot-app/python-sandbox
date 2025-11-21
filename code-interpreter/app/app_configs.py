from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

# Executor backend selection
EXECUTOR_BACKEND = os.environ.get("EXECUTOR_BACKEND") or "docker"

# Docker executor configuration
PYTHON_EXECUTOR_DOCKER_BIN = os.environ.get("PYTHON_EXECUTOR_DOCKER_BIN") or "docker"
PYTHON_EXECUTOR_DOCKER_IMAGE = (
    os.environ.get("PYTHON_EXECUTOR_DOCKER_IMAGE") or "onyxdotapp/python-executor-sci"
)
PYTHON_EXECUTOR_DOCKER_RUN_ARGS = os.environ.get("PYTHON_EXECUTOR_DOCKER_RUN_ARGS") or ""

# Kubernetes executor configuration
KUBERNETES_EXECUTOR_NAMESPACE = os.environ.get("KUBERNETES_EXECUTOR_NAMESPACE") or "default"
KUBERNETES_EXECUTOR_IMAGE = (
    os.environ.get("KUBERNETES_EXECUTOR_IMAGE") or "onyxdotapp/python-executor-sci"
)
KUBERNETES_EXECUTOR_SERVICE_ACCOUNT = os.environ.get("KUBERNETES_EXECUTOR_SERVICE_ACCOUNT") or ""

# Execution limits
MAX_EXEC_TIMEOUT_MS = int(os.environ.get("MAX_EXEC_TIMEOUT_MS") or 60_000)
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES") or 1_000_000)
CPU_TIME_LIMIT_SEC = int(os.environ.get("CPU_TIME_LIMIT_SEC") or 5)
MEMORY_LIMIT_MB = int(os.environ.get("MEMORY_LIMIT_MB") or 256)

# API server configuration
HOST = os.environ.get("HOST") or "0.0.0.0"  # noqa: S104
PORT = int(os.environ.get("PORT") or "8000")

# File storage configuration
FILE_STORAGE_DIR = (
    os.environ.get("FILE_STORAGE_DIR") or "/tmp/code-interpreter-files"  # noqa: S108
)
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB") or 100)
FILE_TTL_SEC = int(os.environ.get("FILE_TTL_SEC") or 3600)


@dataclass(frozen=True, slots=True)
class Settings:
    max_exec_timeout_ms: int
    max_output_bytes: int
    cpu_time_limit_sec: int
    memory_limit_mb: int
    file_storage_dir: str
    max_file_size_mb: int
    file_ttl_sec: int

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            max_exec_timeout_ms=MAX_EXEC_TIMEOUT_MS,
            max_output_bytes=MAX_OUTPUT_BYTES,
            cpu_time_limit_sec=CPU_TIME_LIMIT_SEC,
            memory_limit_mb=MEMORY_LIMIT_MB,
            file_storage_dir=FILE_STORAGE_DIR,
            max_file_size_mb=MAX_FILE_SIZE_MB,
            file_ttl_sec=FILE_TTL_SEC,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
