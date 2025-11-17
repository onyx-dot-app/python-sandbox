from __future__ import annotations

from typing import Any, Final

import httpx
import pytest

BASE_URL: Final[str] = "http://localhost:8000"


def test_execute_endpoint_basic_flow() -> None:
    timeout = httpx.Timeout(5.0, connect=5.0)

    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        try:
            health_response = client.get("/health")
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert health_response.status_code == 200, health_response.text
        assert health_response.json() == {"status": "ok"}

        execute_payload: dict[str, Any] = {
            "code": "print('hello from e2e')",
            "stdin": None,
            "timeout_ms": 1000,
            "files": [],
        }

        try:
            execute_response = client.post("/v1/execute", json=execute_payload)
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert execute_response.status_code == 200, execute_response.text

        result = execute_response.json()

        # Create a detailed error message with the full result for debugging
        error_msg = f"Test failed. Full result: {result}"

        assert result["stdout"] == "hello from e2e\n", f"stdout mismatch. {error_msg}"
        assert result["stderr"] == "", f"stderr mismatch. {error_msg}"
        assert result["exit_code"] == 0, f"exit_code mismatch. {error_msg}"
        assert result["timed_out"] is False, f"timed_out mismatch. {error_msg}"
        assert isinstance(result["duration_ms"], int), f"duration_ms type mismatch. {error_msg}"
        assert result["duration_ms"] >= 0, f"duration_ms value invalid. {error_msg}"
        assert isinstance(result["files"], list), f"files type mismatch. {error_msg}"


def test_execute_edits_passed_file() -> None:
    timeout = httpx.Timeout(5.0, connect=5.0)

    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        # First check health
        try:
            health_response = client.get("/health")
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert health_response.status_code == 200, health_response.text

        # Upload the file to be edited
        initial_content = "Hello World\nThis is line 2\nThis is line 3"
        upload_files = {
            "file": ("input.txt", initial_content.encode("utf-8"), "text/plain")
        }

        try:
            upload_response = client.post("/v1/files", files=upload_files)
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert upload_response.status_code == 201, upload_response.text
        file_id = upload_response.json()["file_id"]

        execute_payload: dict[str, Any] = {
            "code": """
from pathlib import Path

# Read the existing file
content = Path('input.txt').read_text()
print(f"Original content length: {len(content)}")

# Edit the file by appending a new line
edited_content = content + "\\nThis is a new line added by code"
Path('input.txt').write_text(edited_content)

# Read back to verify
final_content = Path('input.txt').read_text()
print(f"Final content length: {len(final_content)}")
print("File edited successfully")
""".strip(),
            "stdin": None,
            "timeout_ms": 2000,
            "files": [
                {
                    "path": "input.txt",
                    "file_id": file_id,
                }
            ],
        }

        try:
            execute_response = client.post("/v1/execute", json=execute_payload)
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert execute_response.status_code == 200, execute_response.text

        result = execute_response.json()

        # Verify execution succeeded
        assert result["exit_code"] == 0, f"Execution failed: {result}"
        assert result["timed_out"] is False
        assert "File edited successfully" in result["stdout"]

        # Find the edited file in the response
        files = result.get("files", [])
        edited_file = None
        for file_entry in files:
            if file_entry.get("path") == "input.txt":
                edited_file = file_entry
                break

        assert edited_file is not None, "input.txt not found in response files"
        assert edited_file["kind"] == "file"

        # Get the file_id to download the edited file
        file_id = edited_file.get("file_id")
        assert isinstance(file_id, str), "file_id should be present"

        # Download the file using the file_id
        download_response = client.get(f"/v1/files/{file_id}")
        assert download_response.status_code == 200, (
            f"Failed to download file: {download_response.text}"
        )

        returned_content = download_response.content.decode("utf-8")

        expected_content = initial_content + "\nThis is a new line added by code"
        assert returned_content == expected_content, (
            f"Content mismatch. Expected: {expected_content!r}, Got: {returned_content!r}"
        )
