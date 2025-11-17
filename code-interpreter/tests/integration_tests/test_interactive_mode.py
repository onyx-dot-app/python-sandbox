"""Test last-line-interactive mode functionality."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_last_line_interactive_enabled_by_default() -> None:
    """Test that last-line-interactive mode is enabled by default."""
    client = TestClient(create_app())

    response = client.post(
        "/v1/execute",
        json={
            "code": "1 + 1\n2 + 2",
            "stdin": None,
            "timeout_ms": 1000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    # With last-line-interactive mode, only the last expression prints
    assert payload["stdout"] == "4\n"
    assert payload["exit_code"] == 0


def test_last_line_interactive_enabled() -> None:
    """Test that last-line-interactive mode prints only the last expression value."""
    client = TestClient(create_app())

    response = client.post(
        "/v1/execute",
        json={
            "code": "1 + 1\n2 + 2\nx = 5\nx\nprint('hello')\nx * 2",
            "stdin": None,
            "timeout_ms": 1000,
            "last_line_interactive": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()

    # With last-line-interactive mode, only the last expression prints (plus any print statements)
    expected_lines = ["hello", "10"]
    actual_lines = payload["stdout"].strip().split("\n")

    assert actual_lines == expected_lines
    assert payload["exit_code"] == 0


def test_last_line_interactive_with_print_statements() -> None:
    """Test that print statements work alongside last expression output."""
    client = TestClient(create_app())

    response = client.post(
        "/v1/execute",
        json={
            "code": "x = 10\nprint(f'x is {x}')\nx + 5",
            "stdin": None,
            "timeout_ms": 1000,
            "last_line_interactive": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()

    lines = payload["stdout"].strip().split("\n")
    # Print statement executes, then the last expression prints
    assert lines == ["x is 10", "15"]


def test_last_line_interactive_with_errors() -> None:
    """Test that errors still work properly in last-line-interactive mode."""
    client = TestClient(create_app())

    response = client.post(
        "/v1/execute",
        json={
            "code": "x = 5\nx\n1/0\ny = 10",
            "stdin": None,
            "timeout_ms": 1000,
            "last_line_interactive": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()

    # Should NOT have output for intermediate expressions (x is not the last expression)
    # The error happens before we reach the end
    assert payload["stdout"] == ""
    # Should have error in stderr
    assert "ZeroDivisionError" in payload["stderr"]
    assert payload["exit_code"] == 1
