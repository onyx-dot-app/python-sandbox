"""Tests for executor pod placement, resources and volume sizes.

Unit tests that mock the Kubernetes API, so no cluster is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client import (  # type: ignore[import-untyped]
    ApiClient,
    V1OwnerReference,
    V1Pod,
)

from app.kubernetes_pod_config import (
    parse_pod_overrides,
    parse_pod_resources,
    parse_size_limit,
)
from app.services.executor_base import SESSION_EXPIRES_AT_KEY
from app.services.executor_kubernetes import (
    ExecutorPodSettings,
    KubernetesExecutor,
    executor_container_resources,
)

OVERRIDES: dict[str, Any] = {
    "nodeSelector": {"onyx.app/pool": "sandbox"},
    "tolerations": [
        {"key": "sandbox", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
    ],
    "affinity": {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}
                        ]
                    }
                ]
            }
        }
    },
    "topologySpreadConstraints": [
        {
            "maxSkew": 1,
            "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": "ScheduleAnyway",
            "labelSelector": {"matchLabels": {"app": "code-interpreter"}},
        }
    ],
    "priorityClassName": "sandbox-low",
    "runtimeClassName": "gvisor",
    "labels": {"team": "ai"},
    "annotations": {"example.com/cost-center": "42"},
}

EXECUTOR_LABELS = {"app": "code-interpreter", "component": "executor"}


@pytest.fixture()
def executor() -> KubernetesExecutor:
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst.namespace = "test"
    inst.image = "onyxdotapp/python-executor-sci"
    inst.service_account = ""
    inst.net_admin_lockdown = False
    inst.owner_reference = None
    inst.pod_settings = ExecutorPodSettings()
    return inst


def _manifest(
    executor: KubernetesExecutor,
    *,
    memory_limit_mb: int | None = 256,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> V1Pod:
    return executor._create_pod_manifest(
        pod_name="code-exec-abc",
        command=["sleep", "60"],
        labels=labels or EXECUTOR_LABELS,
        annotations=annotations,
        memory_limit_mb=memory_limit_mb,
    )


def _with(executor: KubernetesExecutor, **settings: Any) -> KubernetesExecutor:  # noqa: ANN401
    executor.pod_settings = ExecutorPodSettings(**settings)
    return executor


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_defaults_add_no_placement(executor: KubernetesExecutor) -> None:
    pod = _manifest(executor)
    spec = pod.spec
    assert spec.node_selector is None
    assert spec.tolerations is None
    assert spec.affinity is None
    assert spec.topology_spread_constraints is None
    assert spec.priority_class_name is None
    assert spec.runtime_class_name is None
    assert pod.metadata.labels == EXECUTOR_LABELS
    assert pod.metadata.annotations is None


def test_every_placement_field_is_applied(executor: KubernetesExecutor) -> None:
    overrides = parse_pod_overrides("X", json.dumps(OVERRIDES))
    pod = _manifest(_with(executor, overrides=overrides))

    assert pod.spec.node_selector == OVERRIDES["nodeSelector"]
    assert pod.spec.tolerations == OVERRIDES["tolerations"]
    assert pod.spec.affinity == OVERRIDES["affinity"]
    assert pod.spec.topology_spread_constraints == OVERRIDES["topologySpreadConstraints"]
    assert pod.spec.priority_class_name == "sandbox-low"
    assert pod.spec.runtime_class_name == "gvisor"
    assert pod.metadata.labels == {**EXECUTOR_LABELS, "team": "ai"}
    assert pod.metadata.annotations == {"example.com/cost-center": "42"}


def test_serialized_pod_uses_api_field_names(executor: KubernetesExecutor) -> None:
    overrides = parse_pod_overrides("X", json.dumps(OVERRIDES))
    body = ApiClient().sanitize_for_serialization(_manifest(_with(executor, overrides=overrides)))
    spec = body["spec"]
    assert spec["nodeSelector"] == OVERRIDES["nodeSelector"]
    assert spec["tolerations"] == OVERRIDES["tolerations"]
    assert spec["priorityClassName"] == "sandbox-low"
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["topologySpreadConstraints"] == OVERRIDES["topologySpreadConstraints"]


def test_controller_fields_win_over_overrides(executor: KubernetesExecutor) -> None:
    executor.owner_reference = V1OwnerReference(
        api_version="apps/v1", kind="Deployment", name="ci", uid="uid-1", controller=True
    )
    overrides = parse_pod_overrides("X", json.dumps({"annotations": {"note": "x"}}))
    pod = _manifest(
        _with(executor, overrides=overrides),
        labels={"app": "code-interpreter", "component": "session"},
        annotations={SESSION_EXPIRES_AT_KEY: "123.0"},
    )
    assert pod.metadata.labels == {"app": "code-interpreter", "component": "session"}
    assert pod.metadata.annotations == {"note": "x", SESSION_EXPIRES_AT_KEY: "123.0"}
    assert [ref.uid for ref in pod.metadata.owner_references] == ["uid-1"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{not json", "not valid JSON"),
        ('{"labels": {"app": "other"}}', "set by the controller"),
        ('{"labels": {"component": "other"}}', "set by the controller"),
        (json.dumps({"annotations": {SESSION_EXPIRES_AT_KEY: "1"}}), "set by the controller"),
        ('{"ownerReferences": []}', "ownerReferences"),
        ('{"nodeSelector": {"bad key!": "x"}}', "not a valid Kubernetes"),
        ('{"nodeSelector": {"pool": "bad value!"}}', "not valid"),
        ('{"tolerations": [{"operator": "Maybe"}]}', "tolerations.0.operator"),
        ('{"tolerations": {"key": "x"}}', "tolerations"),
        ('{"affinity": {"nodeAfinity": {}}}', "affinity.nodeAfinity"),
        ('{"topologySpreadConstraints": [{"maxSkew": 1}]}', "topologyKey"),
        ('{"priorityClassName": "Not_Valid"}', "not a valid Kubernetes object name"),
        ('{"hostNetwork": true}', "hostNetwork"),
        ("[]", "<root>"),
    ],
)
def test_bad_overrides_fail_at_startup(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match="KUBERNETES_EXECUTOR_POD_OVERRIDES") as excinfo:
        parse_pod_overrides("KUBERNETES_EXECUTOR_POD_OVERRIDES", raw)
    assert message in str(excinfo.value)


def test_empty_chart_defaults_parse_to_no_overrides() -> None:
    raw = json.dumps(
        {
            "nodeSelector": {},
            "tolerations": [],
            "affinity": {},
            "topologySpreadConstraints": [],
            "priorityClassName": "",
            "runtimeClassName": "",
            "labels": {},
            "annotations": {},
        }
    )
    overrides = parse_pod_overrides("X", raw)
    assert overrides.priority_class_name is None
    assert overrides.runtime_class_name is None
    assert parse_pod_overrides("X", "") == parse_pod_overrides("X", None)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def test_default_resources(executor: KubernetesExecutor) -> None:
    resources = _manifest(executor).spec.containers[0].resources
    assert resources == {
        "requests": {"cpu": "100m", "memory": "64Mi"},
        "limits": {"cpu": "1", "memory": "256Mi"},
    }


def test_resources_come_from_config(executor: KubernetesExecutor) -> None:
    resources = parse_pod_resources(
        "X",
        json.dumps(
            {
                "requests": {"cpu": "250m", "memory": "128Mi", "ephemeral-storage": "200Mi"},
                "limits": {"cpu": 2, "ephemeral-storage": "1Gi"},
            }
        ),
    )
    container = _manifest(_with(executor, resources=resources), memory_limit_mb=512)
    assert container.spec.containers[0].resources == {
        "requests": {"cpu": "250m", "memory": "128Mi", "ephemeral-storage": "200Mi"},
        "limits": {"cpu": "2", "ephemeral-storage": "1Gi", "memory": "512Mi"},
    }


def test_cpu_limit_does_not_come_from_cpu_time_limit(executor: KubernetesExecutor) -> None:
    executor.v1.read_namespaced_pod.return_value = V1Pod(status=MagicMock(phase="Running"))
    executor._upload_tar_to_pod = MagicMock()  # type: ignore[method-assign]
    executor._stream_pod_exec = MagicMock()  # type: ignore[method-assign]
    executor._cleanup_pod = MagicMock()  # type: ignore[method-assign]
    executor._drain_exec_stream = MagicMock(  # type: ignore[method-assign]
        return_value=(b"", b"", 0, False)
    )
    executor._extract_workspace_snapshot = MagicMock(  # type: ignore[method-assign]
        return_value=()
    )

    executor.execute_python(
        code="print(1)",
        stdin=None,
        timeout_ms=1_000,
        max_output_bytes=100,
        cpu_time_limit_sec=5,
        memory_limit_mb=256,
    )

    pod = executor.v1.create_namespaced_pod.call_args.kwargs["body"]
    assert pod.spec.containers[0].resources["limits"]["cpu"] == "1"


def test_memory_request_is_capped_at_memory_limit(executor: KubernetesExecutor) -> None:
    resources = parse_pod_resources("X", '{"requests": {"memory": "1Gi"}}')
    container = _manifest(_with(executor, resources=resources), memory_limit_mb=256)
    assert container.spec.containers[0].resources == {
        "requests": {"memory": "256Mi"},
        "limits": {"memory": "256Mi"},
    }


def test_no_memory_limit_without_memory_limit_mb(executor: KubernetesExecutor) -> None:
    resources = parse_pod_resources("X", "{}")
    assert (
        _manifest(_with(executor, resources=resources), memory_limit_mb=None)
        .spec.containers[0]
        .resources
        is None
    )


def test_null_resource_removes_a_default() -> None:
    resources = parse_pod_resources("X", '{"requests": {"cpu": "100m"}, "limits": {"cpu": null}}')
    assert executor_container_resources(resources, None) == {"requests": {"cpu": "100m"}}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"limits": {"memory": "1Gi"}}', "MEMORY_LIMIT_MB"),
        ('{"limits": {"cpu": "lots"}}', "not a valid Kubernetes quantity"),
        ('{"limits": {"cpu": "0"}}', "must be positive"),
        ('{"limits": {"gpu": "1"}}', "limits.gpu"),
        ('{"requests": {"cpu": "2"}, "limits": {"cpu": "1"}}', "exceeds limits.cpu"),
        ('{"request": {}}', "request"),
    ],
)
def test_bad_resources_fail_at_startup(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match="KUBERNETES_EXECUTOR_POD_RESOURCES") as excinfo:
        parse_pod_resources("KUBERNETES_EXECUTOR_POD_RESOURCES", raw)
    assert message in str(excinfo.value)


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------


def test_volume_size_limits_are_configurable(executor: KubernetesExecutor) -> None:
    pod = _manifest(_with(executor, workspace_size_limit="1Gi", tmp_size_limit="256Mi"))
    assert pod.spec.volumes == [
        {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
    ]


def test_default_volume_size_limits(executor: KubernetesExecutor) -> None:
    assert [v["emptyDir"]["sizeLimit"] for v in _manifest(executor).spec.volumes] == [
        "100Mi",
        "64Mi",
    ]


def test_bad_size_limit_fails_at_startup() -> None:
    assert parse_size_limit("X", None, "64Mi") == "64Mi"
    with pytest.raises(ValueError, match="KUBERNETES_EXECUTOR_TMP_SIZE_LIMIT"):
        parse_size_limit("KUBERNETES_EXECUTOR_TMP_SIZE_LIMIT", "64 megabytes", "64Mi")


@pytest.mark.parametrize(
    "env_var",
    [
        "KUBERNETES_EXECUTOR_POD_OVERRIDES",
        "KUBERNETES_EXECUTOR_POD_RESOURCES",
        "KUBERNETES_EXECUTOR_WORKSPACE_SIZE_LIMIT",
    ],
)
def test_service_refuses_to_start_with_bad_config(env_var: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.app_configs"],
        env={**os.environ, env_var: "{bad"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert f"ValueError: {env_var}" in result.stderr
