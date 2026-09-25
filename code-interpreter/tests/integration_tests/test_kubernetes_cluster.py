"""Executor pod checks against a real cluster.

Opt-in: set KUBERNETES_EXECUTOR_TEST_NAMESPACE to a namespace in the current
kubeconfig context. The executor image must be pullable, or already on the
nodes with KUBERNETES_EXECUTOR_TEST_PULL_POLICY=IfNotPresent (e.g. kind).
Label the namespace pod-security.kubernetes.io/enforce=restricted to check
restricted Pod Security admission.
"""

from __future__ import annotations

import os
import time
from typing import Final

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.services.executor_kubernetes import (
    ExecutorPodSettings,
    ExecutorPodStartError,
    KubernetesExecutor,
)

NAMESPACE: Final[str] = os.environ.get("KUBERNETES_EXECUTOR_TEST_NAMESPACE") or ""
PULL_POLICY: Final[str | None] = os.environ.get("KUBERNETES_EXECUTOR_TEST_PULL_POLICY") or None
PSS_ENFORCE_LABEL: Final[str] = "pod-security.kubernetes.io/enforce"

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
