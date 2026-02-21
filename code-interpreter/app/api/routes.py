from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import Response

from app.app_configs import get_settings
from app.models.schemas import (
    ExecuteRequest,
    ExecuteResponse,
    FileMetadataResponse,
    ListFilesResponse,
    UploadFileResponse,
    WorkspaceFile,
)
from app.services.executor_base import EntryKind, WorkspaceEntry
from app.services.executor_factory import execute_python
from app.services.file_storage import FileStorageService

router = APIRouter()

# Initialize file storage service
_file_storage: FileStorageService | None = None


def get_file_storage() -> FileStorageService:
    """Get or create the global FileStorageService instance."""
    global _file_storage
    if _file_storage is None:
        settings = get_settings()
        _file_storage = FileStorageService(Path(settings.file_storage_dir))
    return _file_storage


def _validate_timeout(req: ExecuteRequest) -> None:
    settings = get_settings()
    if req.timeout_ms > settings.max_exec_timeout_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"timeout_ms exceeds maximum of {settings.max_exec_timeout_ms} ms",
        )


def _stage_request_files(
    req: ExecuteRequest,
    storage: FileStorageService,
) -> tuple[list[tuple[str, bytes]], dict[str, bytes]]:
    """Resolve uploaded file IDs into content for the executor.

    Returns (staged_files, input_files_map).
    """
    staged_files: list[tuple[str, bytes]] = []
    input_files_map: dict[str, bytes] = {}
    for file in req.files:
        try:
            content, _ = storage.get_file(file.file_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File with ID '{file.file_id}' not found for path '{file.path}'.",
            ) from exc
        staged_files.append((file.path, content))
        input_files_map[file.path] = content
    return staged_files, input_files_map


def _save_workspace_files(
    entries: tuple[WorkspaceEntry, ...],
    input_files_map: dict[str, bytes],
    storage: FileStorageService,
) -> list[WorkspaceFile]:
    """Filter and save new/modified workspace files to storage."""
    workspace_files: list[WorkspaceFile] = []
    for entry in entries:
        if entry.kind == EntryKind.DIRECTORY:
            continue
        if entry.kind == EntryKind.FILE and entry.content is not None:
            if entry.path in input_files_map and entry.content == input_files_map[entry.path]:
                continue
            file_id = storage.save_file(entry.content, entry.path)
            workspace_files.append(WorkspaceFile(path=entry.path, kind=entry.kind, file_id=file_id))
    return workspace_files


@router.post("/execute", response_model=ExecuteResponse, status_code=status.HTTP_200_OK)
def execute(req: ExecuteRequest) -> ExecuteResponse:
    """Execute provided Python code synchronously within an isolated Docker container."""
    _validate_timeout(req)
    settings = get_settings()
    storage = get_file_storage()
    staged_files, input_files_map = _stage_request_files(req, storage)

    try:
        result = execute_python(
            code=req.code,
            stdin=req.stdin,
            timeout_ms=req.timeout_ms,
            max_output_bytes=settings.max_output_bytes,
            cpu_time_limit_sec=settings.cpu_time_limit_sec,
            memory_limit_mb=settings.memory_limit_mb,
            files=staged_files,
            last_line_interactive=req.last_line_interactive,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ExecuteResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        files=_save_workspace_files(result.files, input_files_map, storage),
    )


@router.post("/files", response_model=UploadFileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile = File(...)) -> UploadFileResponse:  # noqa: B008
    """Upload a file for later use in code execution."""
    settings = get_settings()
    storage = get_file_storage()

    # Read file content
    content = await file.read()

    # Validate file size
    max_size_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(content) > max_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds maximum of {settings.max_file_size_mb} MB",
        )

    # Save file and get ID
    filename = file.filename or "unnamed"
    file_id = storage.save_file(content, filename)

    return UploadFileResponse(
        file_id=file_id,
        filename=filename,
        size_bytes=len(content),
    )


@router.get("/files/{file_id}")
async def download_file(file_id: str) -> Response:
    """Download a previously uploaded file by its ID."""
    storage = get_file_storage()

    try:
        content, metadata = storage.get_file(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with ID '{file_id}' not found",
        ) from exc

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{metadata.filename}"',
        },
    )


@router.get("/files", response_model=ListFilesResponse, status_code=status.HTTP_200_OK)
def list_files() -> ListFilesResponse:
    """List all uploaded files with their metadata."""
    storage = get_file_storage()
    files = storage.list_files()

    return ListFilesResponse(
        files=[
            FileMetadataResponse(
                file_id=f.file_id,
                filename=f.filename,
                size_bytes=f.size_bytes,
                upload_time=f.upload_time,
            )
            for f in files
        ]
    )


@router.delete("/files/{file_id}")
def delete_file(file_id: str) -> Response:
    """Delete a previously uploaded file by its ID."""
    storage = get_file_storage()

    if not storage.delete_file(file_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with ID '{file_id}' not found",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
