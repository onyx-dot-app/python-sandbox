from __future__ import annotations

from typing import Final

from prometheus_client import Counter, Gauge, Histogram

OPERATION_EXECUTE: Final[str] = "execute"
OPERATION_EXECUTE_STREAM: Final[str] = "execute_stream"
OPERATION_SESSION_CREATE: Final[str] = "session_create"
OPERATION_SESSION_BASH: Final[str] = "session_bash"

OUTCOME_OK: Final[str] = "ok"
OUTCOME_TIMED_OUT: Final[str] = "timed_out"
OUTCOME_ERROR: Final[str] = "error"

REJECT_REASON_CONCURRENCY_LIMIT: Final[str] = "concurrency_limit"

_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)

EXECUTIONS_ACTIVE: Final[Gauge] = Gauge(
    "code_interpreter_executions_active",
    "Executions currently holding an admission slot on this replica.",
    ["operation"],
)
EXECUTIONS_LIMIT: Final[Gauge] = Gauge(
    "code_interpreter_executions_limit",
    "Configured MAX_CONCURRENT_EXECUTIONS for this replica.",
)
EXECUTIONS_REJECTED: Final[Counter] = Counter(
    "code_interpreter_executions_rejected_total",
    "Executions rejected before they ran, by HTTP status and reason.",
    ["operation", "status", "reason"],
)
EXECUTIONS_COMPLETED: Final[Counter] = Counter(
    "code_interpreter_executions_completed_total",
    "Executions that were admitted, by outcome (ok, timed_out, error).",
    ["operation", "outcome"],
)
ADMISSION_WAIT_SECONDS: Final[Histogram] = Histogram(
    "code_interpreter_admission_wait_seconds",
    "Time spent waiting for an admission slot, including rejected requests.",
    ["operation"],
    buckets=_LATENCY_BUCKETS,
)
EXECUTION_DURATION_SECONDS: Final[Histogram] = Histogram(
    "code_interpreter_execution_duration_seconds",
    "Wall time an admitted execution held its slot, including sandbox startup.",
    ["operation"],
    buckets=_LATENCY_BUCKETS,
)
