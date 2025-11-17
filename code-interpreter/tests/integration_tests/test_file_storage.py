from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_upload_file_returns_file_id() -> None:
    """Test that uploading a file returns a valid file ID."""
    client = _client()

    content = b"test file content"
    files = {"file": ("test.txt", io.BytesIO(content), "text/plain")}

    response = client.post("/v1/files", files=files)

    assert response.status_code == 201
    payload = response.json()
    assert "file_id" in payload
    assert "filename" in payload
    assert payload["filename"] == "test.txt"
    assert "size_bytes" in payload
    assert payload["size_bytes"] == len(content)


def test_download_file_by_id() -> None:
    """Test that a file can be downloaded after upload."""
    client = _client()

    # Upload a file
    content = b"hello world"
    files = {"file": ("data.txt", io.BytesIO(content), "text/plain")}
    upload_response = client.post("/v1/files", files=files)
    assert upload_response.status_code == 201
    file_id = upload_response.json()["file_id"]

    # Download the file
    download_response = client.get(f"/v1/files/{file_id}")
    assert download_response.status_code == 200
    assert download_response.content == content
    assert "data.txt" in download_response.headers.get("content-disposition", "")


def test_download_nonexistent_file_returns_404() -> None:
    """Test that downloading a non-existent file returns 404."""
    client = _client()

    response = client.get("/v1/files/nonexistent-id")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_delete_file_by_id() -> None:
    """Test that a file can be deleted after upload."""
    client = _client()

    # Upload a file
    content = b"to be deleted"
    files = {"file": ("temp.txt", io.BytesIO(content), "text/plain")}
    upload_response = client.post("/v1/files", files=files)
    assert upload_response.status_code == 201
    file_id = upload_response.json()["file_id"]

    # Delete the file
    delete_response = client.delete(f"/v1/files/{file_id}")
    assert delete_response.status_code == 204

    # Verify it's gone
    download_response = client.get(f"/v1/files/{file_id}")
    assert download_response.status_code == 404


def test_delete_nonexistent_file_returns_404() -> None:
    """Test that deleting a non-existent file returns 404."""
    client = _client()

    response = client.delete("/v1/files/nonexistent-id")
    assert response.status_code == 404


def test_list_files() -> None:
    """Test that listing files returns uploaded files."""
    client = _client()

    # Upload two files
    files1 = {"file": ("file1.txt", io.BytesIO(b"content1"), "text/plain")}
    files2 = {"file": ("file2.txt", io.BytesIO(b"content2"), "text/plain")}

    response1 = client.post("/v1/files", files=files1)
    response2 = client.post("/v1/files", files=files2)
    assert response1.status_code == 201
    assert response2.status_code == 201

    file_id1 = response1.json()["file_id"]
    file_id2 = response2.json()["file_id"]

    # List files
    list_response = client.get("/v1/files")
    assert list_response.status_code == 200
    payload = list_response.json()
    assert "files" in payload

    file_ids = [f["file_id"] for f in payload["files"]]
    assert file_id1 in file_ids
    assert file_id2 in file_ids

    # Clean up
    client.delete(f"/v1/files/{file_id1}")
    client.delete(f"/v1/files/{file_id2}")


def test_execute_with_file_id() -> None:
    """Test that execution can reference uploaded files by ID."""
    client = _client()

    # Upload a file
    content = b"seeded via file_id\n"
    files = {"file": ("data.txt", io.BytesIO(content), "text/plain")}
    upload_response = client.post("/v1/files", files=files)
    assert upload_response.status_code == 201
    file_id = upload_response.json()["file_id"]

    # Execute code that reads the file
    exec_response = client.post(
        "/v1/execute",
        json={
            "code": "from pathlib import Path\nprint(Path('input.txt').read_text())",
            "stdin": None,
            "timeout_ms": 1000,
            "files": [
                {
                    "path": "input.txt",
                    "file_id": file_id,
                }
            ],
        },
    )

    assert exec_response.status_code == 200
    payload = exec_response.json()
    assert payload["stdout"] == "seeded via file_id\n\n"
    assert payload["stderr"] == ""

    # Clean up
    client.delete(f"/v1/files/{file_id}")


def test_execute_with_nonexistent_file_id_returns_404() -> None:
    """Test that execution fails with 404 when file_id doesn't exist."""
    client = _client()

    response = client.post(
        "/v1/execute",
        json={
            "code": "print('should not execute')",
            "stdin": None,
            "timeout_ms": 1000,
            "files": [
                {
                    "path": "missing.txt",
                    "file_id": "nonexistent-id",
                }
            ],
        },
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_execute_with_multiple_files() -> None:
    """Test that execution can use multiple uploaded files."""
    client = _client()

    # Upload first file
    content1 = b"from upload 1"
    files1 = {"file": ("file1.txt", io.BytesIO(content1), "text/plain")}
    upload_response1 = client.post("/v1/files", files=files1)
    assert upload_response1.status_code == 201
    file_id1 = upload_response1.json()["file_id"]

    # Upload second file
    content2 = b"from upload 2"
    files2 = {"file": ("file2.txt", io.BytesIO(content2), "text/plain")}
    upload_response2 = client.post("/v1/files", files=files2)
    assert upload_response2.status_code == 201
    file_id2 = upload_response2.json()["file_id"]

    # Execute with both files
    exec_response = client.post(
        "/v1/execute",
        json={
            "code": (
                "from pathlib import Path\n"
                "print('file1:', Path('file1.txt').read_text())\n"
                "print('file2:', Path('file2.txt').read_text())"
            ),
            "stdin": None,
            "timeout_ms": 1000,
            "files": [
                {
                    "path": "file1.txt",
                    "file_id": file_id1,
                },
                {
                    "path": "file2.txt",
                    "file_id": file_id2,
                },
            ],
        },
    )

    assert exec_response.status_code == 200
    payload = exec_response.json()
    assert "file1: from upload 1" in payload["stdout"]
    assert "file2: from upload 2" in payload["stdout"]

    # Clean up
    client.delete(f"/v1/files/{file_id1}")
    client.delete(f"/v1/files/{file_id2}")


def test_upload_file_size_limit() -> None:
    """Test that uploading a file exceeding size limit fails."""
    client = _client()

    # Create a large file (assuming default limit is 100MB, we'll test with larger)
    # For testing purposes, we'll use a reasonable size that we know exceeds the limit
    # This test assumes MAX_FILE_SIZE_MB is set to a reasonable value for testing
    large_content = b"x" * (101 * 1024 * 1024)  # 101 MB
    files = {"file": ("large.bin", io.BytesIO(large_content), "application/octet-stream")}

    response = client.post("/v1/files", files=files)

    # Should fail with 413 Request Entity Too Large
    assert response.status_code == 413
    assert "exceeds maximum" in response.json()["detail"].lower()
