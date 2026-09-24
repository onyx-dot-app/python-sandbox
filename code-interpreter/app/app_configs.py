from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from app.kubernetes_pod_config import (
    DEFAULT_TMP_SIZE_LIMIT,
    DEFAULT_WORKSPACE_SIZE_LIMIT,
    ExecutorPodOverrides,
    ExecutorPodResources,
    parse_pod_overrides,
    parse_pod_resources,
    parse_size_limit,
)

IMAGE_PULL_POLICIES: Final[frozenset[str]] = frozenset({"Always", "IfNotPresent", "Never"})
DEFAULT_EXECUTOR_ID: Final[int] = 65532


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name) or str(default)
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _optional_id_env(name: str, *, minimum: int) -> int | None:
    """Unset means the default ID; set but empty means omit the field."""
    raw = os.environ.get(name)
    if raw is None:
        return DEFAULT_EXECUTOR_ID
    if not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer or empty, got {raw!r}") from e
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def _image_pull_policy_env(name: str) -> str | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    if raw not in IMAGE_PULL_POLICIES:
        raise ValueError(f"{name} must be one of {sorted(IMAGE_PULL_POLICIES)}, got {raw!r}")
    return raw


# Executor backend selection
EXECUTOR_BACKEND = os.environ.get("EXECUTOR_BACKEND") or "docker"

# Docker executor configuration
PYTHON_EXECUTOR_DOCKER_BIN = os.environ.get("PYTHON_EXECUTOR_DOCKER_BIN") or "docker"
PYTHON_EXECUTOR_DOCKER_IMAGE = (
    os.environ.get("PYTHON_EXECUTOR_DOCKER_IMAGE") or "onyxdotapp/python-executor-sci"
)
PYTHON_EXECUTOR_DOCKER_RUN_ARGS = os.environ.get("PYTHON_EXECUTOR_DOCKER_RUN_ARGS") or ""
# Docker network for spawned executor containers. Defaults to "none" (no network access)
# for maximum isolation. Set to a Docker network name (e.g. "onyx_default", "traefik")
# to allow executor containers to reach services on that network.
PYTHON_EXECUTOR_DOCKER_NETWORK = os.environ.get("PYTHON_EXECUTOR_DOCKER_NETWORK") or "none"
# How often (seconds) the image watchdog checks that the executor image is still
# present on the host and re-pulls it if it has gone missing (e.g. after
# `docker system prune -a`). Executor containers run with `--pull never`, so without
# this a removed image breaks every execution until the service is restarted. The
# common case costs one `docker image inspect` per pass. Set to 0 to disable, e.g.
# on air-gapped hosts that cannot pull and would only pay a registry timeout.
PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC = int(
    os.environ.get("PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC") or 60
)

# Kubernetes executor configuration
KUBERNETES_EXECUTOR_NAMESPACE = os.environ.get("KUBERNETES_EXECUTOR_NAMESPACE") or "default"
KUBERNETES_EXECUTOR_IMAGE = (
    os.environ.get("KUBERNETES_EXECUTOR_IMAGE") or "onyxdotapp/python-executor-sci"
)
KUBERNETES_EXECUTOR_SERVICE_ACCOUNT = os.environ.get("KUBERNETES_EXECUTOR_SERVICE_ACCOUNT") or ""
# When true, executor pods run a privileged (NET_ADMIN) init container that uses
# iptables to drop all outbound traffic before the executor container starts. This
# avoids the race where a pod can reach the network before the CNI enforces a
# NetworkPolicy. Environments whose CNI applies NetworkPolicies without that race
# (or that disallow NET_ADMIN) can set this to false and rely on a NetworkPolicy.
KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN = (
    os.environ.get("KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN") or "true"
).lower() not in ("false", "0", "no")
# Namespace this service runs in, and the name of the Deployment that owns it.
# When both are set and the service shares a namespace with its executor pods,
# executor pods get an ownerReference to that Deployment. This lets Kubernetes
# garbage-collect leaked pods and lets monitoring tell them apart from
# long-lived workloads. Requires "get" on apps/deployments.
KUBERNETES_OWN_NAMESPACE = os.environ.get("KUBERNETES_OWN_NAMESPACE") or ""
KUBERNETES_OWNER_DEPLOYMENT_NAME = os.environ.get("KUBERNETES_OWNER_DEPLOYMENT_NAME") or ""
# How long to wait for an executor pod to reach Running, including the image pull.
KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC: Final[int] = _bounded_int_env(
    "KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC", 30, minimum=1, maximum=600
)
# Empty derives the policy from the image tag, as Kubernetes does: Always for an
# untagged or ":latest" image, IfNotPresent otherwise.
KUBERNETES_EXECUTOR_IMAGE_PULL_POLICY: Final[str | None] = _image_pull_policy_env(
    "KUBERNETES_EXECUTOR_IMAGE_PULL_POLICY"
)
# User, group and fsGroup of executor pods. Unset means 65532. Set but empty omits
# the field so the platform assigns one (OpenShift restricted-v2 SCC).
KUBERNETES_EXECUTOR_RUN_AS_USER: Final[int | None] = _optional_id_env(
    "KUBERNETES_EXECUTOR_RUN_AS_USER", minimum=1
)
KUBERNETES_EXECUTOR_RUN_AS_GROUP: Final[int | None] = _optional_id_env(
    "KUBERNETES_EXECUTOR_RUN_AS_GROUP", minimum=0
)
KUBERNETES_EXECUTOR_FS_GROUP: Final[int | None] = _optional_id_env(
    "KUBERNETES_EXECUTOR_FS_GROUP", minimum=0
)
KUBERNETES_EXECUTOR_READ_ONLY_ROOT_FILESYSTEM: Final[bool] = (
    os.environ.get("KUBERNETES_EXECUTOR_READ_ONLY_ROOT_FILESYSTEM") or "true"
).lower() not in ("false", "0", "no")
# JSON with nodeSelector, tolerations, affinity, topologySpreadConstraints,
# priorityClassName, runtimeClassName, labels and annotations for executor pods.
KUBERNETES_EXECUTOR_POD_OVERRIDES: Final[ExecutorPodOverrides] = parse_pod_overrides(
    "KUBERNETES_EXECUTOR_POD_OVERRIDES", os.environ.get("KUBERNETES_EXECUTOR_POD_OVERRIDES")
)
# JSON {"requests": {...}, "limits": {...}} for cpu, memory (requests only) and
# ephemeral-storage. Unset means requests cpu=100m, memory=64Mi and limits cpu=5.
KUBERNETES_EXECUTOR_POD_RESOURCES: Final[ExecutorPodResources] = parse_pod_resources(
    "KUBERNETES_EXECUTOR_POD_RESOURCES", os.environ.get("KUBERNETES_EXECUTOR_POD_RESOURCES")
)
KUBERNETES_EXECUTOR_WORKSPACE_SIZE_LIMIT: Final[str] = parse_size_limit(
    "KUBERNETES_EXECUTOR_WORKSPACE_SIZE_LIMIT",
    os.environ.get("KUBERNETES_EXECUTOR_WORKSPACE_SIZE_LIMIT"),
    DEFAULT_WORKSPACE_SIZE_LIMIT,
)
KUBERNETES_EXECUTOR_TMP_SIZE_LIMIT: Final[str] = parse_size_limit(
    "KUBERNETES_EXECUTOR_TMP_SIZE_LIMIT",
    os.environ.get("KUBERNETES_EXECUTOR_TMP_SIZE_LIMIT"),
    DEFAULT_TMP_SIZE_LIMIT,
)

# Execution limits
MAX_EXEC_TIMEOUT_MS = int(os.environ.get("MAX_EXEC_TIMEOUT_MS") or 60_000)
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES") or 1_000_000)
# Docker only: the RLIMIT_CPU of the executor container. Kubernetes executor pods
# are bounded by the execution timeout and by KUBERNETES_EXECUTOR_POD_RESOURCES.
CPU_TIME_LIMIT_SEC = int(os.environ.get("CPU_TIME_LIMIT_SEC") or 5)
MEMORY_LIMIT_MB = int(os.environ.get("MEMORY_LIMIT_MB") or 256)

# API server configuration
HOST = os.environ.get("HOST") or "0.0.0.0"  # noqa: S104
PORT = int(os.environ.get("PORT") or "8000")

# Logging configuration
# LOG_LEVEL controls verbosity (e.g. DEBUG, INFO, WARNING).
# LOG_FORMAT selects the output style: "plain" (default human-readable text) or
# "json" (structured single-line JSON suitable for container log aggregators).
LOG_LEVEL = (os.environ.get("LOG_LEVEL") or "INFO").upper()
LOG_FORMAT = (os.environ.get("LOG_FORMAT") or "plain").lower()
JSON_LOGGING = LOG_FORMAT == "json"

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
