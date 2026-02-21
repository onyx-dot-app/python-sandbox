from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app


def _parse_sse_events(raw: str) -> list[dict[str, Any]]:
    """Parse SSE text into a list of {event, data} dicts."""
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data = ""

    for line in raw.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            current_data = line[6:]
        elif line == "" and current_event is not None:
            events.append({"event": current_event, "data": json.loads(current_data)})
            current_event = None
            current_data = ""

    return events


def test_streaming_basic_output() -> None:
    client = TestClient(create_app())

    with client.stream(
        "POST",
        "/v1/execute/stream",
        json={"code": "print('hello')", "timeout_ms": 5000},
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        body = response.read().decode()

    events = _parse_sse_events(body)
    output_events = [e for e in events if e["event"] == "output"]
    result_events = [e for e in events if e["event"] == "result"]

    assert len(result_events) == 1
    result = result_events[0]["data"]
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert isinstance(result["duration_ms"], int)

    full_stdout = "".join(
        e["data"]["data"] for e in output_events if e["data"]["stream"] == "stdout"
    )
    assert full_stdout == "hello\n"


def test_streaming_stderr_output() -> None:
    client = TestClient(create_app())

    with client.stream(
        "POST",
        "/v1/execute/stream",
        json={"code": "import sys; sys.stderr.write('oops\\n')", "timeout_ms": 5000},
    ) as response:
        body = response.read().decode()

    events = _parse_sse_events(body)
    stderr_events = [
        e for e in events if e["event"] == "output" and e["data"]["stream"] == "stderr"
    ]
    full_stderr = "".join(e["data"]["data"] for e in stderr_events)
    assert "oops" in full_stderr


def test_streaming_timeout() -> None:
    client = TestClient(create_app())

    with client.stream(
        "POST",
        "/v1/execute/stream",
        json={"code": "import time; time.sleep(30)", "timeout_ms": 1000},
    ) as response:
        body = response.read().decode()

    events = _parse_sse_events(body)
    result_events = [e for e in events if e["event"] == "result"]

    assert len(result_events) == 1
    result = result_events[0]["data"]
    assert result["timed_out"] is True
    assert result["exit_code"] is None


def test_streaming_with_files() -> None:
    client = TestClient(create_app())

    code = "with open('output.txt', 'w') as f: f.write('hello file')"
    with client.stream(
        "POST",
        "/v1/execute/stream",
        json={"code": code, "timeout_ms": 5000},
    ) as response:
        body = response.read().decode()

    events = _parse_sse_events(body)
    result_events = [e for e in events if e["event"] == "result"]

    assert len(result_events) == 1
    result = result_events[0]["data"]
    assert result["exit_code"] == 0

    file_paths = [f["path"] for f in result["files"]]
    assert "output.txt" in file_paths


def test_streaming_empty_output() -> None:
    client = TestClient(create_app())

    with client.stream(
        "POST",
        "/v1/execute/stream",
        json={"code": "x = 1 + 1", "timeout_ms": 5000, "last_line_interactive": False},
    ) as response:
        body = response.read().decode()

    events = _parse_sse_events(body)
    output_events = [e for e in events if e["event"] == "output"]
    result_events = [e for e in events if e["event"] == "result"]

    assert len(output_events) == 0
    assert len(result_events) == 1
    assert result_events[0]["data"]["exit_code"] == 0


def test_streaming_timeout_validation() -> None:
    """Timeout exceeding max should return 422, not a stream."""
    client = TestClient(create_app())

    response = client.post(
        "/v1/execute/stream",
        json={"code": "print('hi')", "timeout_ms": 999_999_999},
    )
    assert response.status_code == 422


def test_streaming_syntax_error() -> None:
    client = TestClient(create_app())

    with client.stream(
        "POST",
        "/v1/execute/stream",
        json={"code": "def foo(", "timeout_ms": 5000},
    ) as response:
        body = response.read().decode()

    events = _parse_sse_events(body)
    result_events = [e for e in events if e["event"] == "result"]

    assert len(result_events) == 1
    result = result_events[0]["data"]
    assert result["exit_code"] != 0
