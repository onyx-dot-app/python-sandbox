"""Tests for executor pod ownerReferences.

Unit tests that mock the Kubernetes API to exercise owner resolution
without requiring a real cluster.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import V1OwnerReference  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.services.executor_kubernetes import ExecutorPodSettings, KubernetesExecutor

MODULE = "app.services.executor_kubernetes"


@pytest.fixture()
def executor() -> KubernetesExecutor:
    """Create a KubernetesExecutor bypassing __init__ (no cluster needed)."""
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst._rest_api_client = MagicMock()
    inst.namespace = "onyx"
    inst.image = "test:latest"
    inst.service_account = ""
    inst.net_admin_lockdown = True
    inst.owner_reference = None
    inst.pod_settings = ExecutorPodSettings()
    return inst


def _deployment(uid: str = "dep-uid-123") -> MagicMock:
    deployment = MagicMock()
    deployment.metadata.name = "onyx-code-interpreter"
    deployment.metadata.uid = uid
    return deployment


def test_owner_reference_resolves_to_deployment(executor: KubernetesExecutor) -> None:
    apps_api = MagicMock()
    apps_api.read_namespaced_deployment.return_value = _deployment()

    with (
        patch(f"{MODULE}.KUBERNETES_OWN_NAMESPACE", "onyx"),
        patch(f"{MODULE}.KUBERNETES_OWNER_DEPLOYMENT_NAME", "onyx-code-interpreter"),
        patch(f"{MODULE}.client.AppsV1Api", return_value=apps_api),
    ):
        owner = executor._resolve_owner_reference()

    assert owner is not None
    assert owner.kind == "Deployment"
    assert owner.api_version == "apps/v1"
    assert owner.name == "onyx-code-interpreter"
    assert owner.uid == "dep-uid-123"
    assert owner.controller is True
    # Setting this needs "update" on the owner's finalizers, which we lack.
    assert owner.block_owner_deletion is False


def test_owner_reference_is_none_when_unconfigured(executor: KubernetesExecutor) -> None:
    with (
        patch(f"{MODULE}.KUBERNETES_OWN_NAMESPACE", ""),
        patch(f"{MODULE}.KUBERNETES_OWNER_DEPLOYMENT_NAME", ""),
    ):
        assert executor._resolve_owner_reference() is None


def test_owner_reference_is_none_across_namespaces(executor: KubernetesExecutor) -> None:
    """A cross-namespace owner reads as deleted, so GC would drop the pod at once."""
    apps_api = MagicMock()
    apps_api.read_namespaced_deployment.return_value = _deployment()

    with (
        patch(f"{MODULE}.KUBERNETES_OWN_NAMESPACE", "onyx"),
        patch(f"{MODULE}.KUBERNETES_OWNER_DEPLOYMENT_NAME", "onyx-code-interpreter"),
        patch(f"{MODULE}.client.AppsV1Api", return_value=apps_api),
    ):
        executor.namespace = "code-exec"
        assert executor._resolve_owner_reference() is None

    apps_api.read_namespaced_deployment.assert_not_called()


def test_owner_reference_is_none_when_read_forbidden(executor: KubernetesExecutor) -> None:
    """Without the apps/deployments RBAC rule, pods are created as before."""
    apps_api = MagicMock()
    apps_api.read_namespaced_deployment.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch(f"{MODULE}.KUBERNETES_OWN_NAMESPACE", "onyx"),
        patch(f"{MODULE}.KUBERNETES_OWNER_DEPLOYMENT_NAME", "onyx-code-interpreter"),
        patch(f"{MODULE}.client.AppsV1Api", return_value=apps_api),
    ):
        assert executor._resolve_owner_reference() is None


def test_owner_reference_is_none_when_api_unreachable(executor: KubernetesExecutor) -> None:
    """__init__ must not raise: /health builds the executor via get_executor()."""
    apps_api = MagicMock()
    apps_api.read_namespaced_deployment.side_effect = OSError("connection refused")

    with (
        patch(f"{MODULE}.KUBERNETES_OWN_NAMESPACE", "onyx"),
        patch(f"{MODULE}.KUBERNETES_OWNER_DEPLOYMENT_NAME", "onyx-code-interpreter"),
        patch(f"{MODULE}.client.AppsV1Api", return_value=apps_api),
    ):
        assert executor._resolve_owner_reference() is None


def test_manifest_carries_owner_reference(executor: KubernetesExecutor) -> None:
    executor.owner_reference = V1OwnerReference(
        api_version="apps/v1",
        kind="Deployment",
        name="onyx-code-interpreter",
        uid="dep-uid-123",
        controller=True,
        block_owner_deletion=False,
    )

    manifest = executor._create_pod_manifest(
        pod_name="code-exec-abc",
        command=["sleep", "3600"],
        labels={"app": "code-interpreter", "component": "executor"},
    )

    assert manifest.metadata.owner_references is not None
    assert len(manifest.metadata.owner_references) == 1
    assert manifest.metadata.owner_references[0].uid == "dep-uid-123"


def test_manifest_omits_owner_reference_when_unresolved(executor: KubernetesExecutor) -> None:
    manifest = executor._create_pod_manifest(
        pod_name="code-exec-abc",
        command=["sleep", "3600"],
        labels={"app": "code-interpreter", "component": "executor"},
    )

    assert manifest.metadata.owner_references is None
