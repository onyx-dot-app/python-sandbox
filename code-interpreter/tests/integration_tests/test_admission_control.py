"""Admission control, the 429/503 overload contract, and /metrics."""

from __future__ import annotations

import json
import threading
from collections.abc import Generator, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client import (  # type: ignore[import-untyped]
    V1Pod,
    V1PodCondition,
    V1PodStatus,
)
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from app.app_configs import CAPACITY_RETRY_AFTER_SEC, EXECUTION_RETRY_AFTER_SEC
from app.main import create_app
from app.services.admission import ExecutionLimiter
from app.services.executor_base import (
    CapacityReason,
    ExecutionResult,
    ExecutorCapacityError,
    StreamChunk,
    StreamEvent,
    StreamResult,
    StreamStarted,
)
from app.services.executor_kubernetes import (
    KubernetesExecutor,
    capacity_error_from_api_exception,
    pod_unschedulable_error,
)

EXECUTE_BODY: dict[str, Any] = {"code": "print(1)", "timeout_ms": 1000}
QUOTA_MESSAGE = (
    'pods "code-exec-abc" is forbidden: exceeded quota: executor-quota, '
    "requested: pods=1, used: pods=1, limited: pods=1"
)


def _app_with_limit(limit: int, queue_timeout_sec: float = 0.2) -> FastAPI:
    app = create_app()
    app.state.execution_limiter = ExecutionLimiter(limit=limit, queue_timeout_sec=queue_timeout_sec)
    return app


def _ok_result() -> ExecutionResult:
    return ExecutionResult(
        stdout="1\n", stderr="", exit_code=0, timed_out=False, duration_ms=5, files=()
    )


class _BlockingExecute:
    """Stands in for execute_python and holds its slot until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **_: object) -> ExecutionResult:
        self.entered.set()
        assert self.release.wait(timeout=10)
        return _ok_result()


@pytest.fixture()
def blocking_execute() -> Iterator[_BlockingExecute]:
    blocker = _BlockingExecute()
    with patch("app.api.routes.execute_python", side_effect=blocker):
        yield blocker
    blocker.release.set()


def _hold_slot(client: TestClient, blocker: _BlockingExecute) -> tuple[threading.Thread, list[int]]:
    statuses: list[int] = []

    def _run() -> None:
        statuses.append(client.post("/v1/execute", json=EXECUTE_BODY).status_code)

    thread = threading.Thread(target=_run)
    thread.start()
    assert blocker.entered.wait(timeout=10)
    return thread, statuses


def _metric_value(client: TestClient, name: str, labels: dict[str, str]) -> float:
    text = client.get("/metrics").text
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name + "{"):
            continue
        series, value = line.rsplit(" ", 1)
        series_labels = series[len(name) + 1 : -1].split(",")
        if ",".join(sorted(series_labels)) == label_str:
            return float(value)
    return 0.0


def test_execute_returns_429_with_retry_after_when_saturated(
    blocking_execute: _BlockingExecute,
) -> None:
    client = TestClient(_app_with_limit(1))
    rejected_before = _metric_value(
        client,
        "code_interpreter_executions_rejected_total",
        {"operation": "execute", "status": "429", "reason": "concurrency_limit"},
    )
    thread, statuses = _hold_slot(client, blocking_execute)

    response = client.post("/v1/execute", json=EXECUTE_BODY)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(EXECUTION_RETRY_AFTER_SEC)
    assert "saturated" in response.json()["detail"]
    assert set(response.json()) == {"detail"}

    blocking_execute.release.set()
    thread.join(timeout=10)
    assert statuses == [200]

    # The slot is freed once the first run finishes.
    assert client.post("/v1/execute", json=EXECUTE_BODY).status_code == 200
    assert (
        _metric_value(
            client,
            "code_interpreter_executions_rejected_total",
            {"operation": "execute", "status": "429", "reason": "concurrency_limit"},
        )
        == rejected_before + 1
    )


def test_queued_request_runs_when_slot_frees_within_queue_timeout(
    blocking_execute: _BlockingExecute,
) -> None:
    client = TestClient(_app_with_limit(1, queue_timeout_sec=5.0))
    thread, _ = _hold_slot(client, blocking_execute)

    timer = threading.Timer(0.3, blocking_execute.release.set)
    timer.start()
    response = client.post("/v1/execute", json=EXECUTE_BODY)
    thread.join(timeout=10)

    assert response.status_code == 200


def test_stream_returns_429_before_streaming_when_saturated(
    blocking_execute: _BlockingExecute,
) -> None:
    client = TestClient(_app_with_limit(1))
    thread, _ = _hold_slot(client, blocking_execute)

    with client.stream("POST", "/v1/execute/stream", json=EXECUTE_BODY) as response:
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(EXECUTION_RETRY_AFTER_SEC)
        assert "text/event-stream" not in response.headers["content-type"]
        body = json.loads(response.read())
    assert "detail" in body

    blocking_execute.release.set()
    thread.join(timeout=10)


def test_execute_returns_503_on_executor_capacity_error() -> None:
    error = ExecutorCapacityError(QUOTA_MESSAGE, reason=CapacityReason.QUOTA_EXCEEDED)
    with patch("app.api.routes.execute_python", side_effect=error):
        client = TestClient(create_app())
        response = client.post("/v1/execute", json=EXECUTE_BODY)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(CAPACITY_RETRY_AFTER_SEC)
    assert "quota_exceeded" in response.json()["detail"]
    assert client.app.state.execution_limiter.active == 0  # type: ignore[attr-defined]


def test_stream_returns_503_before_streaming_on_capacity_error() -> None:
    error = ExecutorCapacityError("no nodes", reason=CapacityReason.UNSCHEDULABLE)
    with patch("app.api.routes.execute_python_streaming", side_effect=error):
        client = TestClient(create_app())
        with client.stream("POST", "/v1/execute/stream", json=EXECUTE_BODY) as response:
            assert response.status_code == 503
            assert response.headers["Retry-After"] == str(CAPACITY_RETRY_AFTER_SEC)
            body = json.loads(response.read())

    assert "unschedulable" in body["detail"]
    assert client.app.state.execution_limiter.active == 0  # type: ignore[attr-defined]


def test_stream_releases_slot_after_run_and_hides_started_event() -> None:
    def _stream(**_: object) -> Generator[StreamEvent, None, None]:
        yield StreamStarted()
        yield StreamChunk(stream="stdout", data="hi\n")
        yield StreamResult(exit_code=0, timed_out=False, duration_ms=1, files=())

    with patch("app.api.routes.execute_python_streaming", side_effect=_stream):
        client = TestClient(_app_with_limit(1))
        for _ in range(2):
            with client.stream("POST", "/v1/execute/stream", json=EXECUTE_BODY) as response:
                assert response.status_code == 200
                body = response.read().decode()
            assert [line for line in body.splitlines() if line.startswith("event:")] == [
                "event: output",
                "event: result",
            ]

    assert client.app.state.execution_limiter.active == 0  # type: ignore[attr-defined]


def test_session_create_returns_503_on_capacity_error() -> None:
    executor = MagicMock()
    executor.create_session.side_effect = ExecutorCapacityError(
        QUOTA_MESSAGE, reason=CapacityReason.QUOTA_EXCEEDED
    )
    with patch("app.api.routes.get_executor", return_value=executor):
        client = TestClient(create_app())
        response = client.post("/v1/sessions", json={})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(CAPACITY_RETRY_AFTER_SEC)


def test_metrics_endpoint_exposes_execution_series() -> None:
    with patch("app.api.routes.execute_python", return_value=_ok_result()):
        client = TestClient(create_app())
        assert client.post("/v1/execute", json=EXECUTE_BODY).status_code == 200
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for name in (
        "code_interpreter_executions_active",
        "code_interpreter_executions_limit",
        "code_interpreter_executions_rejected_total",
        "code_interpreter_executions_completed_total",
        "code_interpreter_admission_wait_seconds_bucket",
        "code_interpreter_execution_duration_seconds_bucket",
    ):
        assert name in text
    assert 'code_interpreter_executions_completed_total{operation="execute",outcome="ok"}' in text


# ---------------------------------------------------------------------------
# Kubernetes error mapping
# ---------------------------------------------------------------------------


def _forbidden(message: str) -> ApiException:
    exc = ApiException(status=403, reason="Forbidden")
    exc.body = json.dumps({"kind": "Status", "reason": "Forbidden", "message": message})
    return exc


def test_quota_exceeded_403_maps_to_capacity_error() -> None:
    error = capacity_error_from_api_exception(_forbidden(QUOTA_MESSAGE))
    assert error is not None
    assert error.reason is CapacityReason.QUOTA_EXCEEDED
    assert "exceeded quota" in str(error)


@pytest.mark.parametrize(
    "exc",
    [
        _forbidden('pods is forbidden: User "x" cannot create resource "pods"'),
        _forbidden("pods is forbidden: failed quota: q: must specify limits.cpu"),
        ApiException(status=500, reason="Internal Server Error"),
    ],
)
def test_other_api_errors_are_not_capacity_errors(exc: ApiException) -> None:
    assert capacity_error_from_api_exception(exc) is None


@pytest.fixture()
def kube_executor() -> KubernetesExecutor:
    inst = KubernetesExecutor.__new__(KubernetesExecutor)
    inst.v1 = MagicMock()
    inst.namespace = "test"
    inst.image = "test:latest"
    inst.service_account = ""
    inst.net_admin_lockdown = True
    inst.owner_reference = None
    inst.v1.create_namespaced_pod.side_effect = _forbidden(QUOTA_MESSAGE)
    inst.v1.read_namespaced_pod.side_effect = ApiException(status=404)
    return inst


def test_kubernetes_execute_raises_capacity_error_on_quota(
    kube_executor: KubernetesExecutor,
) -> None:
    with pytest.raises(ExecutorCapacityError) as info:
        kube_executor.execute_python(
            code="print(1)", stdin=None, timeout_ms=1000, max_output_bytes=100
        )
    assert info.value.reason is CapacityReason.QUOTA_EXCEEDED


def test_kubernetes_stream_and_session_raise_capacity_error_on_quota(
    kube_executor: KubernetesExecutor,
) -> None:
    with pytest.raises(ExecutorCapacityError):
        next(
            kube_executor.execute_python_streaming(
                code="print(1)", stdin=None, timeout_ms=1000, max_output_bytes=100
            )
        )
    with pytest.raises(ExecutorCapacityError):
        kube_executor.create_session(ttl_seconds=60)


def test_quota_exceeded_through_api_returns_503(kube_executor: KubernetesExecutor) -> None:
    with patch("app.services.executor_factory.get_executor", return_value=kube_executor):
        client = TestClient(create_app())
        response = client.post("/v1/execute", json=EXECUTE_BODY)
        with client.stream("POST", "/v1/execute/stream", json=EXECUTE_BODY) as stream:
            stream_status = stream.status_code
            stream.read()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(CAPACITY_RETRY_AFTER_SEC)
    assert "exceeded quota" in response.json()["detail"]
    assert stream_status == 503


def test_pod_unschedulable_error_detects_scheduler_condition() -> None:
    pod = V1Pod(
        status=V1PodStatus(
            phase="Pending",
            conditions=[
                V1PodCondition(
                    type="PodScheduled",
                    status="False",
                    reason="Unschedulable",
                    message="0/3 nodes are available: 3 Insufficient cpu.",
                )
            ],
        )
    )
    error = pod_unschedulable_error(pod)
    assert error is not None
    assert error.reason is CapacityReason.UNSCHEDULABLE
    assert "Insufficient cpu" in str(error)


def test_pod_unschedulable_error_ignores_pending_pod_still_scheduling() -> None:
    assert pod_unschedulable_error(V1Pod(status=V1PodStatus(phase="Pending"))) is None
    assert pod_unschedulable_error(V1Pod(status=V1PodStatus(phase="Running"))) is None


def test_limiter_rejects_immediately_with_zero_queue_timeout() -> None:
    limiter = ExecutionLimiter(limit=1, queue_timeout_sec=0.0)

    async def _run() -> None:
        first = await limiter.acquire("execute")
        assert first is not None
        assert await limiter.acquire("execute") is None
        first.release()
        first.release()  # idempotent
        assert limiter.active == 0

    anyio.run(_run)
