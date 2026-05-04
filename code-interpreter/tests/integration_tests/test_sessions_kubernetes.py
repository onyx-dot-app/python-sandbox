"""Unit tests for KubernetesExecutor session methods.

Mocks the Kubernetes API so the session lifecycle can be exercised without
a real cluster.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.services.executor_base import (
    SESSION_APP_LABEL,
    SESSION_COMPONENT_LABEL,
    SESSION_EXPIRES_AT_KEY,
    SESSION_NAME_PREFIX,
)
from app.services.executor_kubernetes import (
    SESSION_LABEL_SELECTOR,
    KubernetesExecutor,
)


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
    inst.v1.read_namespaced_pod.return_value = pod_mock
    return inst


def _make_pod(name: str, expires_at: float | None) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.annotations = (
        {SESSION_EXPIRES_AT_KEY: str(expires_at)} if expires_at is not None else {}
    )
    return pod


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


def test_create_session_returns_session_info(executor: KubernetesExecutor) -> None:
    before = time.time()
    info = executor.create_session(ttl_seconds=600)

    assert info.session_id.startswith(SESSION_NAME_PREFIX)
    assert before + 600 <= info.expires_at <= time.time() + 600


def test_create_session_pod_carries_session_metadata(executor: KubernetesExecutor) -> None:
    info = executor.create_session(ttl_seconds=600)

    pod = executor.v1.create_namespaced_pod.call_args.kwargs["body"]
    assert pod.metadata.name == info.session_id
    assert pod.metadata.labels["app"] == SESSION_APP_LABEL
    assert pod.metadata.labels["component"] == SESSION_COMPONENT_LABEL
    assert pod.metadata.annotations[SESSION_EXPIRES_AT_KEY] == str(info.expires_at)


def test_create_session_sets_active_deadline(executor: KubernetesExecutor) -> None:
    """active_deadline_seconds is what makes kubelet stop the pod at TTL even if API is down."""
    executor.create_session(ttl_seconds=600)

    pod = executor.v1.create_namespaced_pod.call_args.kwargs["body"]
    assert pod.spec.active_deadline_seconds == 600
    assert pod.spec.containers[0].command == ["sleep", "600"]


def test_create_session_stages_files(executor: KubernetesExecutor) -> None:
    with patch.object(executor, "_upload_tar_to_pod") as upload:
        info = executor.create_session(
            ttl_seconds=300,
            files=[("data.txt", b"hello")],
        )

    upload.assert_called_once()
    pod_name_arg, tar_arg = upload.call_args.args
    assert pod_name_arg == info.session_id
    assert isinstance(tar_arg, bytes)
    assert len(tar_arg) > 0


def test_create_session_skips_upload_when_no_files(executor: KubernetesExecutor) -> None:
    with patch.object(executor, "_upload_tar_to_pod") as upload:
        executor.create_session(ttl_seconds=300)

    upload.assert_not_called()


def test_create_session_cleans_up_on_staging_failure(executor: KubernetesExecutor) -> None:
    with (
        patch.object(executor, "_upload_tar_to_pod", side_effect=RuntimeError("boom")),
        patch.object(executor, "_cleanup_pod") as cleanup,
        pytest.raises(RuntimeError, match="boom"),
    ):
        executor.create_session(ttl_seconds=300, files=[("data.txt", b"x")])

    cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------


def test_delete_session_returns_true_on_success(executor: KubernetesExecutor) -> None:
    assert executor.delete_session(f"{SESSION_NAME_PREFIX}abc") is True
    executor.v1.delete_namespaced_pod.assert_called_once()


def test_delete_session_returns_false_on_404(executor: KubernetesExecutor) -> None:
    executor.v1.delete_namespaced_pod.side_effect = ApiException(status=404)
    assert executor.delete_session(f"{SESSION_NAME_PREFIX}abc") is False


def test_delete_session_rejects_non_session_id(executor: KubernetesExecutor) -> None:
    """Prefix check prevents accidentally deleting unrelated pods."""
    assert executor.delete_session("code-exec-abc") is False
    executor.v1.delete_namespaced_pod.assert_not_called()


def test_delete_session_propagates_other_api_errors(executor: KubernetesExecutor) -> None:
    executor.v1.delete_namespaced_pod.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        executor.delete_session(f"{SESSION_NAME_PREFIX}abc")


# ---------------------------------------------------------------------------
# reap_expired_sessions
# ---------------------------------------------------------------------------


def test_reap_deletes_expired_pods(executor: KubernetesExecutor) -> None:
    now = time.time()
    expired_pod = _make_pod("code-session-old", expires_at=now - 100)
    fresh_pod = _make_pod("code-session-new", expires_at=now + 100)
    executor.v1.list_namespaced_pod.return_value = MagicMock(items=[expired_pod, fresh_pod])

    reaped = executor.reap_expired_sessions()

    assert reaped == 1
    executor.v1.list_namespaced_pod.assert_called_once_with(
        namespace=executor.namespace,
        label_selector=SESSION_LABEL_SELECTOR,
    )
    executor.v1.delete_namespaced_pod.assert_called_once()
    delete_kwargs = executor.v1.delete_namespaced_pod.call_args.kwargs
    assert delete_kwargs["name"] == "code-session-old"


def test_reap_skips_pods_without_annotation(executor: KubernetesExecutor) -> None:
    pod = _make_pod("code-session-bare", expires_at=None)
    executor.v1.list_namespaced_pod.return_value = MagicMock(items=[pod])

    assert executor.reap_expired_sessions() == 0
    executor.v1.delete_namespaced_pod.assert_not_called()


def test_reap_skips_pods_with_invalid_annotation(executor: KubernetesExecutor) -> None:
    pod = MagicMock()
    pod.metadata.name = "code-session-bad"
    pod.metadata.annotations = {SESSION_EXPIRES_AT_KEY: "not-a-number"}
    executor.v1.list_namespaced_pod.return_value = MagicMock(items=[pod])

    assert executor.reap_expired_sessions() == 0
    executor.v1.delete_namespaced_pod.assert_not_called()


def test_reap_handles_list_failure(executor: KubernetesExecutor) -> None:
    executor.v1.list_namespaced_pod.side_effect = ApiException(status=500)
    assert executor.reap_expired_sessions() == 0


def test_reap_continues_when_individual_delete_404s(executor: KubernetesExecutor) -> None:
    """Race: pod can vanish between list and delete — treat as already-gone."""
    now = time.time()
    expired_a = _make_pod("code-session-a", expires_at=now - 100)
    expired_b = _make_pod("code-session-b", expires_at=now - 100)
    executor.v1.list_namespaced_pod.return_value = MagicMock(items=[expired_a, expired_b])
    executor.v1.delete_namespaced_pod.side_effect = [ApiException(status=404), None]

    assert executor.reap_expired_sessions() == 1


def test_reap_keeps_going_when_delete_errors(executor: KubernetesExecutor) -> None:
    """A 500 on one delete shouldn't prevent others from being reaped."""
    now = time.time()
    expired_a = _make_pod("code-session-a", expires_at=now - 100)
    expired_b = _make_pod("code-session-b", expires_at=now - 100)
    executor.v1.list_namespaced_pod.return_value = MagicMock(items=[expired_a, expired_b])
    executor.v1.delete_namespaced_pod.side_effect = [ApiException(status=500), None]

    assert executor.reap_expired_sessions() == 1
