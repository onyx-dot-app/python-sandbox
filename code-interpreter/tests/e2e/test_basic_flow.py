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
        upload_files = {"file": ("input.txt", initial_content.encode("utf-8"), "text/plain")}

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


def test_matplotlib_sine_wave_plot() -> None:
    timeout = httpx.Timeout(10.0, connect=5.0)

    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        # First check health
        try:
            health_response = client.get("/health")
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert health_response.status_code == 200, health_response.text

        code = """
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import numpy as np

# Generate data
x = np.linspace(0, 10, 100)
y = np.sin(x)

# Create plot
plt.figure(figsize=(10, 6))
plt.plot(x, y)
plt.title('Sine Wave')
plt.xlabel('x')
plt.ylabel('sin(x)')
plt.grid(True)

# Save plot
plt.savefig('sine_wave.png')
plt.close()
print("Plot saved successfully")
""".strip()

        execute_payload: dict[str, Any] = {
            "code": code,
            "stdin": None,
            "timeout_ms": 5000,
            "files": [],
        }

        try:
            execute_response = client.post("/v1/execute", json=execute_payload)
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert execute_response.status_code == 200, execute_response.text

        result = execute_response.json()

        # Verify execution succeeded
        assert result["stdout"] == "Plot saved successfully\n", f"stdout mismatch: {result}"
        assert result["stderr"] == "", f"stderr should be empty: {result}"
        assert result["exit_code"] == 0, f"exit_code should be 0: {result}"
        assert result["timed_out"] is False, f"should not timeout: {result}"

        # Verify the PNG file was created and returned
        files = result.get("files")
        assert isinstance(files, list), "files should be a list"

        # Find the sine_wave.png file
        png_file = None
        for file_entry in files:
            if isinstance(file_entry, dict) and file_entry.get("path") == "sine_wave.png":
                png_file = file_entry
                break

        assert png_file is not None, f"sine_wave.png not found in response files: {files}"
        assert png_file["kind"] == "file"

        # Verify the file has a file_id
        file_id = png_file.get("file_id")
        assert isinstance(file_id, str), "file_id should be present"

        # Download the file and verify it's a valid PNG
        download_response = client.get(f"/v1/files/{file_id}")
        assert download_response.status_code == 200, (
            f"Failed to download file: {download_response.text}"
        )
        png_bytes = download_response.content

        # PNG files start with these magic bytes
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "File should be a valid PNG"

        # Verify the file has reasonable size (should be several KB for a plot)
        assert len(png_bytes) > 1000, f"PNG file too small: {len(png_bytes)} bytes"


def test_create_multiple_files() -> None:
    timeout = httpx.Timeout(5.0, connect=5.0)

    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        # First check health
        try:
            health_response = client.get("/health")
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert health_response.status_code == 200, health_response.text

        code = """
# Create multiple files
with open('file1.txt', 'w') as f:
    f.write('Content of file 1')

with open('file2.txt', 'w') as f:
    f.write('Content of file 2')

with open('file3.txt', 'w') as f:
    f.write('Content of file 3')

print("Created 3 files")
""".strip()

        execute_payload: dict[str, Any] = {
            "code": code,
            "stdin": None,
            "timeout_ms": 2000,
            "files": [],
        }

        try:
            execute_response = client.post("/v1/execute", json=execute_payload)
        except httpx.TransportError as exc:  # pragma: no cover - network failure path
            pytest.fail(f"Failed to reach Code Interpreter service at {BASE_URL}: {exc!s}")

        assert execute_response.status_code == 200, execute_response.text

        result = execute_response.json()

        # Verify execution succeeded
        assert result["stdout"] == "Created 3 files\n", f"stdout mismatch: {result}"
        assert result["stderr"] == "", f"stderr should be empty: {result}"
        assert result["exit_code"] == 0, f"exit_code should be 0: {result}"
        assert result["timed_out"] is False, f"should not timeout: {result}"

        # Verify all three files were created and returned
        files = result.get("files")
        assert isinstance(files, list), "files should be a list"
        assert len(files) == 3, f"Expected 3 files, got {len(files)}: {files}"

        # Check that all expected files are present
        file_paths = {file_entry["path"] for file_entry in files}
        expected_paths = {"file1.txt", "file2.txt", "file3.txt"}
        assert file_paths == expected_paths, (
            f"File paths mismatch. Expected: {expected_paths}, Got: {file_paths}"
        )

        # Verify each file has correct content
        expected_contents = {
            "file1.txt": "Content of file 1",
            "file2.txt": "Content of file 2",
            "file3.txt": "Content of file 3",
        }

        for file_entry in files:
            path = file_entry["path"]
            assert file_entry["kind"] == "file", f"{path} should be a file"

            file_id = file_entry.get("file_id")
            assert isinstance(file_id, str), f"{path} should have a file_id"

            # Download and verify content
            download_response = client.get(f"/v1/files/{file_id}")
            assert download_response.status_code == 200, (
                f"Failed to download {path}: {download_response.text}"
            )

            content = download_response.content.decode("utf-8")
            expected_content = expected_contents[path]
            assert content == expected_content, (
                f"Content mismatch for {path}. Expected: {expected_content!r}, Got: {content!r}"
            )
