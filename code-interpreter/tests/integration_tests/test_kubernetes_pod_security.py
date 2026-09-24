"""Tests for executor pod security settings, startup waits and lifetime bounds.

Unit tests that mock the Kubernetes API, so no cluster is needed.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import (  # type: ignore[import-untyped]
    V1ContainerState,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Pod,
    V1PodStatus,
)
from pydantic import ValidationError

from app import app_configs
from app.image_ref import default_image_pull_policy
from app.services.executor_kubernetes import (
    EXECUTE_POD_DEADLINE_MARGIN_SECONDS,
    ExecutorPodSettings,
    ExecutorPodStartError,
    KubernetesExecutor,
    kill_processes_command,
)

PLATFORM_IDS = ExecutorPodSettings(run_as_user=None, run_as_group=None, fs_group=None)


@pytest.fixture()
def executor() -> KubernetesExecutor:
    """Create a KubernetesExecutor bypassing __init__ (no cluster needed)."""
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst.namespace = "test"
    inst.image = "onyxdotapp/python-executor-sci"
    inst.service_account = ""
    inst.net_admin_lockdown = True
    inst.owner_reference = None
    inst.pod_settings = ExecutorPodSettings()
    return inst


def _manifest(executor: KubernetesExecutor) -> V1Pod:
    return executor._create_pod_manifest(
        pod_name="code-exec-abc",
        command=["sleep", "60"],
        labels={"app": "code-interpreter", "component": "executor"},
    )


def _pod(
    phase: str,
    *,
    waiting_reason: str | None = None,
    reason: str | None = None,
    message: str | None = None,
) -> V1Pod:
    statuses = None
    if waiting_reason is not None:
        statuses = [
            V1ContainerStatus(
                name="executor",
                image="missing:1",
                image_id="",
                ready=False,
                restart_count=0,
                state=V1ContainerState(
                    waiting=V1ContainerStateWaiting(
                        reason=waiting_reason, message=f"{waiting_reason} detail"
                    )
                ),
            )
        ]
    return V1Pod(
        status=V1PodStatus(phase=phase, reason=reason, message=message, container_statuses=statuses)
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_meets_restricted_pod_security(executor: KubernetesExecutor) -> None:
    executor.net_admin_lockdown = False
    spec = _manifest(executor).spec

    assert spec.security_context == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
        "fsGroup": 65532,
    }
    assert spec.automount_service_account_token is False
    assert spec.init_containers is None

    container = spec.containers[0]
    assert container.security_context == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "runAsUser": 65532,
        "runAsGroup": 65532,
    }
    assert {"name": "HOME", "value": "/tmp"} in container.env  # noqa: S108


def test_manifest_omits_ids_in_platform_mode(executor: KubernetesExecutor) -> None:
    executor.net_admin_lockdown = False
    executor.pod_settings = PLATFORM_IDS
    spec = _manifest(executor).spec

    assert "fsGroup" not in spec.security_context
    assert spec.security_context["runAsNonRoot"] is True
    assert "runAsUser" not in spec.containers[0].security_context
    assert "runAsGroup" not in spec.containers[0].security_context


def test_manifest_honours_writable_root_filesystem(executor: KubernetesExecutor) -> None:
    executor.pod_settings = ExecutorPodSettings(read_only_root_filesystem=False)
    container = _manifest(executor).spec.containers[0]
    assert container.security_context["readOnlyRootFilesystem"] is False


def test_lockdown_init_container_shares_seccomp_and_pull_policy(
    executor: KubernetesExecutor,
) -> None:
    spec = _manifest(executor).spec

    assert spec.security_context["seccompProfile"] == {"type": "RuntimeDefault"}
    lockdown = spec.init_containers[0]
    assert lockdown.security_context["capabilities"]["add"] == ["NET_ADMIN"]
    assert lockdown.image_pull_policy == spec.containers[0].image_pull_policy


@pytest.mark.parametrize(
    ("image", "configured", "expected"),
    [
        ("onyxdotapp/python-executor-sci", None, "Always"),
        ("onyxdotapp/python-executor-sci:latest", None, "Always"),
        ("onyxdotapp/python-executor-sci:0.4.7", None, "IfNotPresent"),
        ("registry.io:5000/python-executor-sci", None, "Always"),
        ("repo@sha256:" + "a" * 64, None, "IfNotPresent"),
        ("onyxdotapp/python-executor-sci:latest", "IfNotPresent", "IfNotPresent"),
        ("onyxdotapp/python-executor-sci:0.4.7", "Never", "Never"),
    ],
)
def test_manifest_image_pull_policy(
    executor: KubernetesExecutor, image: str, configured: str | None, expected: str
) -> None:
    executor.image = image
    executor.pod_settings = ExecutorPodSettings(image_pull_policy=configured)
    assert _manifest(executor).spec.containers[0].image_pull_policy == expected


def test_default_image_pull_policy_matches_kubernetes() -> None:
    assert default_image_pull_policy("python") == "Always"
    assert default_image_pull_policy("python:3.11") == "IfNotPresent"


def test_execute_pod_has_active_deadline(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.return_value = _pod("Running")
    executor.pod_settings = ExecutorPodSettings(ready_timeout_sec=45)

    with (
        patch.object(executor, "_upload_tar_to_pod"),
        patch.object(executor, "_stream_pod_exec"),
        patch.object(executor, "_cleanup_pod"),
        executor._run_in_pod(
            code="print(1)",
            timeout_ms=10_500,
            memory_limit_mb=None,
            files=None,
            last_line_interactive=False,
        ),
    ):
        pass

    pod = executor.v1.create_namespaced_pod.call_args.kwargs["body"]
    expected = 45 + 11 + EXECUTE_POD_DEADLINE_MARGIN_SECONDS
    assert pod.spec.active_deadline_seconds == expected
    assert pod.spec.containers[0].command == ["sleep", str(expected)]


# ---------------------------------------------------------------------------
# Waiting for the pod
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "waiting_reason",
    ["ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError"],
)
def test_wait_fails_fast_on_unrecoverable_container(
    executor: KubernetesExecutor, waiting_reason: str
) -> None:
    executor.v1.read_namespaced_pod.return_value = _pod("Pending", waiting_reason=waiting_reason)

    start = time.monotonic()
    with pytest.raises(ExecutorPodStartError, match=waiting_reason):
        executor._wait_for_pod_ready("code-exec-abc")
    assert time.monotonic() - start < 1
    assert executor.v1.read_namespaced_pod.call_count == 1


def test_wait_fails_fast_on_failed_phase(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.return_value = _pod(
        "Failed", reason="DeadlineExceeded", message="Pod was active too long"
    )
    with pytest.raises(ExecutorPodStartError, match="Failed: Pod was active too long"):
        executor._wait_for_pod_ready("code-exec-abc")


def test_wait_keeps_polling_while_image_pulls(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.side_effect = [
        _pod("Pending", waiting_reason="ContainerCreating"),
        _pod("Pending"),
        _pod("Running"),
    ]
    executor._wait_for_pod_ready("code-exec-abc")
    assert executor.v1.read_namespaced_pod.call_count == 3


def test_wait_times_out_on_monotonic_deadline(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.return_value = _pod("Pending")
    executor.pod_settings = ExecutorPodSettings(ready_timeout_sec=1)

    start = time.monotonic()
    with pytest.raises(ExecutorPodStartError, match="did not become ready in 1 seconds"):
        executor._wait_for_pod_ready("code-exec-abc")
    assert 1 <= time.monotonic() - start < 2


def test_wait_retries_create_container_error_until_deadline(
    executor: KubernetesExecutor,
) -> None:
    executor.v1.read_namespaced_pod.return_value = _pod(
        "Pending", waiting_reason="CreateContainerError"
    )
    executor.pod_settings = ExecutorPodSettings(ready_timeout_sec=1)

    start = time.monotonic()
    with pytest.raises(
        ExecutorPodStartError,
        match="last waiting reason: CreateContainerError: CreateContainerError detail",
    ):
        executor._wait_for_pod_ready("code-exec-abc")
    assert 1 <= time.monotonic() - start < 2
    assert executor.v1.read_namespaced_pod.call_count > 1


def test_wait_recovers_from_transient_create_container_error(
    executor: KubernetesExecutor,
) -> None:
    executor.v1.read_namespaced_pod.side_effect = [
        _pod("Pending", waiting_reason="CreateContainerError"),
        _pod("Running"),
    ]
    executor._wait_for_pod_ready("code-exec-abc")
    assert executor.v1.read_namespaced_pod.call_count == 2


def test_timeout_without_waiting_reason_omits_it(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.return_value = _pod(
        "Pending", waiting_reason="ContainerCreating"
    )
    executor.pod_settings = ExecutorPodSettings(ready_timeout_sec=1)
    with pytest.raises(ExecutorPodStartError) as exc_info:
        executor._wait_for_pod_ready("code-exec-abc")
    assert "last waiting reason" not in str(exc_info.value)


def test_pod_settings_is_frozen_and_rejects_unknown_fields() -> None:
    settings = ExecutorPodSettings()
    with pytest.raises(ValidationError):
        settings.ready_timeout_sec = 5
    with pytest.raises(ValidationError):
        ExecutorPodSettings(ready_timout_sec=5)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "601", "abc"])
def test_ready_timeout_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC", raw)
    with pytest.raises(ValueError, match="KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC"):
        app_configs._bounded_int_env(
            "KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC", 30, minimum=1, maximum=600
        )


def test_ready_timeout_defaults_to_30(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC", raising=False)
    value = app_configs._bounded_int_env(
        "KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC", 30, minimum=1, maximum=600
    )
    assert value == 30


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 65532), ("", None), ("  ", None), ("1000700000", 1000700000)],
)
def test_optional_id_env(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int | None
) -> None:
    if raw is None:
        monkeypatch.delenv("KUBERNETES_EXECUTOR_RUN_AS_USER", raising=False)
    else:
        monkeypatch.setenv("KUBERNETES_EXECUTOR_RUN_AS_USER", raw)
    assert app_configs._optional_id_env("KUBERNETES_EXECUTOR_RUN_AS_USER", minimum=1) == expected


def test_optional_id_env_rejects_root_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_EXECUTOR_RUN_AS_USER", "0")
    with pytest.raises(ValueError, match="at least 1"):
        app_configs._optional_id_env("KUBERNETES_EXECUTOR_RUN_AS_USER", minimum=1)


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("IfNotPresent", "IfNotPresent")])
def test_image_pull_policy_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None
) -> None:
    monkeypatch.setenv("KUBERNETES_EXECUTOR_IMAGE_PULL_POLICY", raw)
    policy = app_configs._image_pull_policy_env("KUBERNETES_EXECUTOR_IMAGE_PULL_POLICY")
    assert policy == expected


def test_image_pull_policy_env_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_EXECUTOR_IMAGE_PULL_POLICY", "always")
    with pytest.raises(ValueError, match="must be one of"):
        app_configs._image_pull_policy_env("KUBERNETES_EXECUTOR_IMAGE_PULL_POLICY")


# ---------------------------------------------------------------------------
# Killing processes
# ---------------------------------------------------------------------------


def test_kill_processes_command_uses_python_not_pkill() -> None:
    command = kill_processes_command("env", "CODE_INTERPRETER_EXEC_ID=abc")

    assert command[:2] == ["python", "-c"]
    assert command[3:] == ["env", "CODE_INTERPRETER_EXEC_ID=abc"]
    compile(command[2], "<kill>", "exec")
    assert "pkill" not in command[2]
    assert "pid not in (me, 1)" in command[2]
    assert 'split(b"\\0")' in command[2]


def _exec_resp(stdout: str, exit_status: str) -> MagicMock:
    resp = MagicMock()
    resp.is_open.side_effect = [True, False]
    resp.peek_stdout.side_effect = [True]
    resp.read_stdout.return_value = stdout
    resp.peek_stderr.return_value = False
    resp.read_channel.return_value = exit_status
    return resp


def test_kill_processes_logs_count(
    executor: KubernetesExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    resp = _exec_resp("2\n", "{'status': 'Success'}")
    with (
        caplog.at_level("INFO"),
        patch.object(executor, "_stream_pod_exec", return_value=resp) as exec_mock,
    ):
        executor._kill_processes_in_pod("session-abc", "comm", "bash")

    assert exec_mock.call_args.kwargs["command"] == kill_processes_command("comm", "bash")
    assert "Killed 2 process(es) (comm=bash) in pod session-abc" in caplog.text


def test_kill_processes_warns_on_failure(
    executor: KubernetesExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    resp = _exec_resp("", "{'status': 'Failure', 'details': {'exitCode': 127}}")
    with patch.object(executor, "_stream_pod_exec", return_value=resp):
        executor._kill_processes_in_pod("session-abc", "comm", "bash")

    assert "Failed to kill processes (comm=bash) in pod session-abc (exit_code=127" in caplog.text
