"""Unit tests for KubernetesExecutor session methods.

Mocks the Kubernetes API so the session lifecycle can be exercised without
a real cluster.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.services.executor_base import (
    SESSION_APP_LABEL,
    SESSION_COMPONENT_LABEL,
    SESSION_NAME_PREFIX,
)
from app.services.executor_kubernetes import KubernetesExecutor


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


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


def test_create_session_returns_session_info(executor: KubernetesExecutor) -> None:
    info = executor.create_session()
    assert info.session_id.startswith(SESSION_NAME_PREFIX)


def test_create_session_pod_carries_session_metadata(executor: KubernetesExecutor) -> None:
    info = executor.create_session()

    pod = executor.v1.create_namespaced_pod.call_args.kwargs["body"]
    assert pod.metadata.name == info.session_id
    assert pod.metadata.labels["app"] == SESSION_APP_LABEL
    assert pod.metadata.labels["component"] == SESSION_COMPONENT_LABEL


def test_create_session_stages_files(executor: KubernetesExecutor) -> None:
    with patch.object(executor, "_upload_tar_to_pod") as upload:
        info = executor.create_session(files=[("data.txt", b"hello")])

    upload.assert_called_once()
    pod_name_arg, tar_arg = upload.call_args.args
    assert pod_name_arg == info.session_id
    assert isinstance(tar_arg, bytes)
    assert len(tar_arg) > 0


def test_create_session_skips_upload_when_no_files(executor: KubernetesExecutor) -> None:
    with patch.object(executor, "_upload_tar_to_pod") as upload:
        executor.create_session()

    upload.assert_not_called()


def test_create_session_cleans_up_on_staging_failure(executor: KubernetesExecutor) -> None:
    with (
        patch.object(executor, "_upload_tar_to_pod", side_effect=RuntimeError("boom")),
        patch.object(executor, "_cleanup_pod") as cleanup,
        pytest.raises(RuntimeError, match="boom"),
    ):
        executor.create_session(files=[("data.txt", b"x")])

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
