"""Reproduce: concurrent /v1/execute requests fail with "Handshake status 200 OK".

The KubernetesExecutor uses a single CoreV1Api client for both REST and
streaming operations.  stream.stream() temporarily monkey-patches the
shared api_client.request to use WebSocket.  Under concurrent load, a
REST call from one request can land during another request's patch window,
causing a WebSocket handshake against a non-WebSocket endpoint.

This test fires multiple concurrent requests at the code interpreter and
asserts that all succeed.  With the current bug, at least one will fail
with an error containing "Handshake status" or a 500 status.

After the fix (separate ApiClient instances for REST vs streaming), all
requests should succeed.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any, Final

import httpx
import pytest

BASE_URL: Final[str] = "http://localhost:8000"
# Number of concurrent requests — enough to reliably trigger the race.
CONCURRENCY: Final[int] = 5


def _execute_request(index: int) -> dict[str, Any]:
    """Send a single /v1/execute request and return the parsed result.

    Raises on transport errors or non-200 status so the caller can
    collect failures.
    """
    timeout = httpx.Timeout(60.0, connect=10.0)
    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        response = client.post(
            "/v1/execute",
            json={
                "code": f"print('request {index}')",
                "timeout_ms": 30000,
            },
        )
        response.raise_for_status()
        return {"index": index, "result": response.json()}


def test_concurrent_execute_requests_all_succeed() -> None:
    """Fire N concurrent /v1/execute requests.

    With the shared-client bug, overlapping stream.stream() calls cause
    REST calls to be routed through the WebSocket path, producing errors
    like "Handshake status 200 OK".

    All N requests must return exit_code == 0 for this test to pass.
    """
    # Verify the service is reachable first
    timeout = httpx.Timeout(10.0, connect=5.0)
    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        try:
            health = client.get("/health")
        except httpx.TransportError as exc:
            pytest.fail(f"Code interpreter not reachable at {BASE_URL}: {exc}")
        assert health.status_code == 200 and health.json()["status"] == "ok"

    # Fire concurrent requests
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_execute_request, i): i for i in range(CONCURRENCY)}

        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(f"request {idx}: {exc}")

    # Report all failures together for easier debugging
    assert not errors, (
        f"{len(errors)}/{CONCURRENCY} concurrent requests failed:\n"
        + "\n".join(errors)
    )

    # Every successful response should have exit_code == 0
    for r in results:
        result = r["result"]
        assert result["exit_code"] == 0, (
            f"request {r['index']} failed: "
            f"stdout={result.get('stdout')!r} "
            f"stderr={result.get('stderr')!r}"
        )
