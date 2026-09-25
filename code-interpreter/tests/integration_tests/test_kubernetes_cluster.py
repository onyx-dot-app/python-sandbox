"""Executor pod checks against a real cluster.

Opt-in: set KUBERNETES_EXECUTOR_TEST_NAMESPACE to a namespace in the current
kubeconfig context. The executor image must be pullable, or already on the
nodes with KUBERNETES_EXECUTOR_TEST_PULL_POLICY=IfNotPresent (e.g. kind).
Label the namespace pod-security.kubernetes.io/enforce=restricted to check
restricted Pod Security admission. Set KUBERNETES_EXECUTOR_TEST_PLACEMENT_NODE to a
node name to run the placement tests, which label and taint that node for the
duration of the test.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from typing import Final
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.app_configs import CAPACITY_RETRY_AFTER_SEC
from app.kubernetes_pod_config import ExecutorPodOverrides, parse_pod_overrides
from app.main import create_app
from app.services.executor_base import CapacityReason, ExecutorCapacityError
from app.services.executor_kubernetes import (
    ExecutorPodSettings,
    ExecutorPodStartError,
    KubernetesExecutor,
)

NAMESPACE: Final[str] = os.environ.get("KUBERNETES_EXECUTOR_TEST_NAMESPACE") or ""
PULL_POLICY: Final[str | None] = os.environ.get("KUBERNETES_EXECUTOR_TEST_PULL_POLICY") or None
PSS_ENFORCE_LABEL: Final[str] = "pod-security.kubernetes.io/enforce"
PLACEMENT_NODE: Final[str] = os.environ.get("KUBERNETES_EXECUTOR_TEST_PLACEMENT_NODE") or ""
PLACEMENT_KEY: Final[str] = "code-interpreter-test/sandbox"

pytestmark = pytest.mark.skipif(
    not NAMESPACE, reason="set KUBERNETES_EXECUTOR_TEST_NAMESPACE to run cluster tests"
)

WORKLOAD: Final[str] = """
import os, pathlib, socket, tempfile
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

plt.plot([1, 2, 3])
plt.savefig("plot.png")
pd.DataFrame({"a": [1, 2]}).to_csv("out.csv")
with tempfile.NamedTemporaryFile() as f:
    f.write(b"x")
pathlib.Path(os.path.expanduser("~/.cache/probe")).parent.mkdir(parents=True, exist_ok=True)
try:
    open("/opt/probe", "w")
    print("root-writable")
except OSError:
    print("root-readonly")
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    print("egress-open")
except OSError:
    print("egress-blocked")
print("uid", os.getuid(), "gid", os.getgid())
"""


@pytest.fixture()
def executor() -> KubernetesExecutor:
    inst = KubernetesExecutor()
    inst.namespace = NAMESPACE
    inst.owner_reference = None
    inst.net_admin_lockdown = False
    inst.pod_settings = ExecutorPodSettings(image_pull_policy=PULL_POLICY)
    return inst


def _restricted(executor: KubernetesExecutor) -> bool:
    namespace = executor.v1.read_namespace(NAMESPACE)
    return (namespace.metadata.labels or {}).get(PSS_ENFORCE_LABEL) == "restricted"


def _run(executor: KubernetesExecutor) -> str:
    result = executor.execute_python(
        code=WORKLOAD, stdin=None, timeout_ms=60_000, max_output_bytes=100_000
    )
    assert result.exit_code == 0, result.stderr
    assert {entry.path for entry in result.files} >= {"plot.png", "out.csv"}
    return result.stdout


def test_execute_with_default_ids(executor: KubernetesExecutor) -> None:
    stdout = _run(executor)
    assert "uid 65532 gid 65532" in stdout
    assert "root-readonly" in stdout


def test_execute_with_platform_assigned_ids(executor: KubernetesExecutor) -> None:
    """Emulates OpenShift restricted-v2, which assigns a high UID, GID 0 and no fsGroup."""
    executor.pod_settings = ExecutorPodSettings(
        image_pull_policy=PULL_POLICY, run_as_user=1000700000, run_as_group=0, fs_group=None
    )
    assert "uid 1000700000 gid 0" in _run(executor)


def test_platform_mode_without_assigned_ids_fails_fast(executor: KubernetesExecutor) -> None:
    """Without an admission plugin that assigns a UID, the root image cannot start."""
    executor.pod_settings = ExecutorPodSettings(
        image_pull_policy=PULL_POLICY, run_as_user=None, run_as_group=None, fs_group=None
    )
    start = time.monotonic()
    with pytest.raises(ExecutorPodStartError, match="CreateContainerConfigError"):
        _run(executor)
    assert time.monotonic() - start < 20


def test_missing_image_fails_fast(executor: KubernetesExecutor) -> None:
    executor.image = "onyxdotapp/python-executor-sci:does-not-exist"
    executor.pod_settings = ExecutorPodSettings(image_pull_policy="IfNotPresent")
    start = time.monotonic()
    with pytest.raises(ExecutorPodStartError, match="ErrImagePull|ImagePullBackOff"):
        _run(executor)
    assert time.monotonic() - start < executor.pod_settings.ready_timeout_sec


def test_net_admin_lockdown(executor: KubernetesExecutor) -> None:
    executor.net_admin_lockdown = True
    if _restricted(executor):
        with pytest.raises(ApiException) as excinfo:
            _run(executor)
        assert excinfo.value.status == 403
        return
    assert "egress-blocked" in _run(executor)


def test_session_bash_timeout_kills_command(executor: KubernetesExecutor) -> None:
    session = executor.create_session(ttl_seconds=120)
    try:
        result = executor.execute_bash_in_session(
            session.session_id, cmd="sleep 31337", timeout_ms=2_000, max_output_bytes=10_000
        )
        assert result.timed_out is True

        # The bracket keeps this command's own cmdline from matching.
        check = executor.execute_bash_in_session(
            session.session_id,
            cmd='for p in /proc/[0-9]*; do tr "\\0" " " < "$p/cmdline"; echo; done '
            '| grep -c "sleep 3133[7]" || true',
            timeout_ms=10_000,
            max_output_bytes=10_000,
        )
        assert check.exit_code == 0, check.stderr
        assert check.stdout.strip() == "0"
    finally:
        executor.delete_session(session.session_id)


@pytest.fixture()
def sandbox_node(executor: KubernetesExecutor) -> Generator[str, None, None]:
    """Label and taint PLACEMENT_NODE as a dedicated sandbox node, then restore it."""
    if not PLACEMENT_NODE:
        pytest.skip("set KUBERNETES_EXECUTOR_TEST_PLACEMENT_NODE to run placement tests")
    v1 = executor.v1
    taints = v1.read_node(PLACEMENT_NODE).spec.taints or []
    sandbox_taint = {"key": PLACEMENT_KEY, "value": "true", "effect": "NoSchedule"}
    v1.patch_node(
        PLACEMENT_NODE,
        {
            "metadata": {"labels": {PLACEMENT_KEY: "true"}},
            "spec": {"taints": [*taints, sandbox_taint]},
        },
    )
    try:
        yield PLACEMENT_NODE
    finally:
        current = v1.read_node(PLACEMENT_NODE).spec.taints or []
        v1.patch_node(
            PLACEMENT_NODE,
            {
                "metadata": {"labels": {PLACEMENT_KEY: None}},
                "spec": {"taints": [t for t in current if t.key != PLACEMENT_KEY] or None},
            },
        )


def _placement(*, tolerate: bool) -> ExecutorPodOverrides:
    tolerations = [{"key": PLACEMENT_KEY, "operator": "Exists", "effect": "NoSchedule"}]
    return parse_pod_overrides(
        "overrides",
        json.dumps(
            {
                "nodeSelector": {PLACEMENT_KEY: "true"},
                "tolerations": tolerations if tolerate else [],
            }
        ),
    )


def test_pod_runs_on_tainted_sandbox_node(executor: KubernetesExecutor, sandbox_node: str) -> None:
    executor.pod_settings = ExecutorPodSettings(
        image_pull_policy=PULL_POLICY, overrides=_placement(tolerate=True)
    )
    nodes: list[str] = []
    wait = executor._wait_for_pod_ready

    def wait_and_record(pod_name: str) -> None:
        wait(pod_name)
        nodes.append(executor.v1.read_namespaced_pod(pod_name, NAMESPACE).spec.node_name)

    executor._wait_for_pod_ready = wait_and_record  # type: ignore[method-assign]
    _run(executor)
    assert nodes == [sandbox_node]


def test_pod_without_toleration_stays_pending(
    executor: KubernetesExecutor, sandbox_node: str
) -> None:
    executor.pod_settings = ExecutorPodSettings(
        image_pull_policy=PULL_POLICY, ready_timeout_sec=5, overrides=_placement(tolerate=False)
    )
    with pytest.raises(ExecutorCapacityError) as excinfo:
        _run(executor)
    assert excinfo.value.reason is CapacityReason.UNSCHEDULABLE


EXECUTE_BODY: Final[dict[str, object]] = {"code": "print(1)", "timeout_ms": 10_000}


def _post_execute(executor: KubernetesExecutor, path: str) -> tuple[int, dict[str, str], str]:
    with patch("app.services.executor_factory.get_executor", return_value=executor):
        http = TestClient(create_app())
        response = http.post(path, json=EXECUTE_BODY)
        return response.status_code, dict(response.headers), response.text


@pytest.mark.parametrize("path", ["/v1/execute", "/v1/execute/stream"])
def test_unschedulable_pod_returns_503(executor: KubernetesExecutor, path: str) -> None:
    executor.pod_settings = ExecutorPodSettings(
        image_pull_policy=PULL_POLICY,
        ready_timeout_sec=3,
        overrides=parse_pod_overrides(
            "overrides", json.dumps({"nodeSelector": {PLACEMENT_KEY: "no-such-node"}})
        ),
    )
    status, headers, body = _post_execute(executor, path)
    assert status == 503, body
    assert headers["retry-after"] == str(CAPACITY_RETRY_AFTER_SEC)
    assert "unschedulable" in json.loads(body)["detail"]


@pytest.fixture()
def exhausted_quota(executor: KubernetesExecutor) -> Generator[None, None, None]:
    name = "code-interpreter-test-exhausted"
    executor.v1.create_namespaced_resource_quota(
        NAMESPACE,
        client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1ResourceQuotaSpec(hard={"pods": "0"}),
        ),
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            quota = executor.v1.read_namespaced_resource_quota(name, NAMESPACE)
            if quota.status and quota.status.hard:
                break
            time.sleep(0.2)
        yield
    finally:
        executor.v1.delete_namespaced_resource_quota(name, NAMESPACE)


@pytest.mark.parametrize("path", ["/v1/execute", "/v1/execute/stream"])
def test_exhausted_quota_returns_503(
    executor: KubernetesExecutor, exhausted_quota: None, path: str
) -> None:
    status, headers, body = _post_execute(executor, path)
    assert status == 503, body
    assert headers["retry-after"] == str(CAPACITY_RETRY_AFTER_SEC)
    assert "quota_exceeded" in json.loads(body)["detail"]
