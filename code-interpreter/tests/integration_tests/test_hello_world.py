from fastapi.testclient import TestClient

from app.main import create_app


def test_execute_returns_expected_payload() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/v1/execute",
        json={
            "code": "print('hello')",
            "stdin": None,
            "timeout_ms": 1000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stdout"] == "hello\n"
    assert payload["stderr"] == ""
    assert payload["exit_code"] == 0
    assert payload["timed_out"] is False
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0
