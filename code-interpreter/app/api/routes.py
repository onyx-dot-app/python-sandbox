from __future__ import annotations

import itertools
import weakref
from collections.abc import Generator, Iterator
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, SkipValidation

from app.app_configs import (
    CAPACITY_RETRY_AFTER_SEC,
    EXECUTION_RETRY_AFTER_SEC,
    get_settings,
)
from app.metrics import (
    EXECUTIONS_COMPLETED,
    EXECUTIONS_REJECTED,
    OPERATION_EXECUTE,
    OPERATION_EXECUTE_STREAM,
    OPERATION_SESSION_BASH,
    OPERATION_SESSION_CREATE,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_TIMED_OUT,
    REJECT_REASON_CONCURRENCY_LIMIT,
)
from app.models.schemas import (
    BashExecRequest,
    BashExecResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ExecuteFile,
    ExecuteRequest,
    ExecuteResponse,
    FileMetadataResponse,
    ListFilesResponse,
    StreamErrorEvent,
    StreamOutputEvent,
    StreamResultEvent,
    UploadFileResponse,
    WorkspaceFile,
)
from app.services.admission import ExecutionLimiter, ExecutionSlot
from app.services.executor_base import (
    EntryKind,
    ExecutorCapacityError,
    SessionNotFoundError,
    StreamChunk,
    StreamEvent,
    StreamResult,
    WorkspaceEntry,
)
from app.services.executor_factory import execute_python, execute_python_streaming, get_executor
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"timeout_ms exceeds maximum of {settings.max_exec_timeout_ms} ms",
        )


def _resolve_uploaded_files(
    files: list[ExecuteFile],
    storage: FileStorageService,
) -> tuple[list[tuple[str, bytes]], dict[str, bytes]]:
    """Resolve uploaded file IDs into content for the executor.

    Returns (staged_files, input_files_map).
    """
    staged_files: list[tuple[str, bytes]] = []
    input_files_map: dict[str, bytes] = {}
    for file in files:
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


def _stage_request_files(
    req: ExecuteRequest,
    storage: FileStorageService,
) -> tuple[list[tuple[str, bytes]], dict[str, bytes]]:
    """Resolve uploaded file IDs into content for the executor.

    Returns (staged_files, input_files_map).
    """
    return _resolve_uploaded_files(req.files, storage)


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


def _too_many_requests(operation: str) -> HTTPException:
    EXECUTIONS_REJECTED.labels(operation, "429", REJECT_REASON_CONCURRENCY_LIMIT).inc()
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            "Execution capacity on this replica is saturated. Retry after the Retry-After delay."
        ),
        headers={"Retry-After": str(EXECUTION_RETRY_AFTER_SEC)},
    )


def _capacity_unavailable(operation: str, exc: ExecutorCapacityError) -> HTTPException:
    EXECUTIONS_REJECTED.labels(operation, "503", exc.reason.value).inc()
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Executor capacity unavailable ({exc.reason.value}): {exc}",
        headers={"Retry-After": str(CAPACITY_RETRY_AFTER_SEC)},
    )


async def _admit(request: Request, operation: str) -> ExecutionSlot:
    limiter: ExecutionLimiter = request.app.state.execution_limiter
    slot = await limiter.acquire(operation)
    if slot is None:
        raise _too_many_requests(operation)
    return slot


def _record_outcome(operation: str, *, timed_out: bool) -> None:
    outcome = OUTCOME_TIMED_OUT if timed_out else OUTCOME_OK
    EXECUTIONS_COMPLETED.labels(operation, outcome).inc()


def _run_execute(req: ExecuteRequest) -> ExecuteResponse:
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ExecutorCapacityError as exc:
        raise _capacity_unavailable(OPERATION_EXECUTE, exc) from exc

    return ExecuteResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        files=_save_workspace_files(result.files, input_files_map, storage),
    )


@router.post("/execute", response_model=ExecuteResponse, status_code=status.HTTP_200_OK)
async def execute(req: ExecuteRequest, request: Request) -> ExecuteResponse:
    """Execute provided Python code synchronously within an isolated sandbox.

    Returns 429 when this replica is at MAX_CONCURRENT_EXECUTIONS and 503 when
    the executor backend has no capacity; both carry a Retry-After header.
    """
    _validate_timeout(req)
    slot = await _admit(request, OPERATION_EXECUTE)
    try:
        response = await run_in_threadpool(_run_execute, req)
    except HTTPException:
        raise
    except Exception:
        EXECUTIONS_COMPLETED.labels(OPERATION_EXECUTE, OUTCOME_ERROR).inc()
        raise
    finally:
        slot.release()
    _record_outcome(OPERATION_EXECUTE, timed_out=response.timed_out)
    return response


class _StartedStream(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # SkipValidation keeps pydantic from wrapping the live generator in a validator.
    events: SkipValidation[Generator[StreamEvent, None, None] | None]
    first_event: SkipValidation[StreamEvent | None]
    input_files_map: dict[str, bytes]
    setup_error: SkipValidation[Exception | None] = None


def _start_stream(req: ExecuteRequest) -> _StartedStream:
    """Stage files and run the executor until the sandbox has started.

    Setup errors raised here become real HTTP statuses. Any other error is
    reported as an SSE error event, as before.
    """
    settings = get_settings()
    staged_files, input_files_map = _stage_request_files(req, get_file_storage())
    events: Generator[StreamEvent, None, None] | None = None
    try:
        events = execute_python_streaming(
            code=req.code,
            stdin=req.stdin,
            timeout_ms=req.timeout_ms,
            max_output_bytes=settings.max_output_bytes,
            cpu_time_limit_sec=settings.cpu_time_limit_sec,
            memory_limit_mb=settings.memory_limit_mb,
            files=staged_files,
            last_line_interactive=req.last_line_interactive,
        )
        first_event = next(events, None)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ExecutorCapacityError as exc:
        raise _capacity_unavailable(OPERATION_EXECUTE_STREAM, exc) from exc
    except Exception as exc:
        return _StartedStream(
            events=events, first_event=None, input_files_map=input_files_map, setup_error=exc
        )
    return _StartedStream(events=events, first_event=first_event, input_files_map=input_files_map)


def _sse_frames(started: _StartedStream, slot: ExecutionSlot) -> Iterator[str]:
    storage = get_file_storage()
    pending = [started.first_event] if started.first_event is not None else []
    try:
        if started.setup_error is not None:
            raise started.setup_error
        for event in itertools.chain(pending, started.events or ()):
            if isinstance(event, StreamChunk):
                yield StreamOutputEvent(stream=event.stream, data=event.data).to_sse()
            elif isinstance(event, StreamResult):
                _record_outcome(OPERATION_EXECUTE_STREAM, timed_out=event.timed_out)
                yield StreamResultEvent(
                    exit_code=event.exit_code,
                    timed_out=event.timed_out,
                    duration_ms=event.duration_ms,
                    files=_save_workspace_files(event.files, started.input_files_map, storage),
                ).to_sse()
    except Exception as exc:
        EXECUTIONS_COMPLETED.labels(OPERATION_EXECUTE_STREAM, OUTCOME_ERROR).inc()
        yield StreamErrorEvent(message=str(exc)).to_sse()
    finally:
        if started.events is not None:
            started.events.close()
        slot.release()


@router.post("/execute/stream")
async def execute_stream(req: ExecuteRequest, request: Request) -> StreamingResponse:
    """Execute Python code with streaming output via Server-Sent Events.

    Admission (429) and backend capacity (503) errors are returned as HTTP
    statuses before the stream opens. Errors after the sandbox starts arrive
    as an SSE ``error`` event.
    """
    _validate_timeout(req)
    slot = await _admit(request, OPERATION_EXECUTE_STREAM)
    try:
        started = await run_in_threadpool(_start_stream, req)
    except BaseException:
        slot.release()
        raise

    frames = _sse_frames(started, slot)
    # A generator that never starts never runs its finally block, e.g. when
    # the client disconnects before the first body chunk.
    weakref.finalize(frames, slot.release)
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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


def _run_create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    settings = get_settings()
    storage = get_file_storage()
    staged_files, _ = _resolve_uploaded_files(req.files, storage)

    try:
        info = get_executor().create_session(
            ttl_seconds=req.ttl_seconds,
            files=staged_files,
            cpu_time_limit_sec=settings.cpu_time_limit_sec,
            memory_limit_mb=settings.memory_limit_mb,
        )
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ExecutorCapacityError as exc:
        raise _capacity_unavailable(OPERATION_SESSION_CREATE, exc) from exc

    return CreateSessionResponse(
        session_id=info.session_id,
        expires_at=info.expires_at,
    )


@router.post(
    "/sessions",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(req: CreateSessionRequest, request: Request) -> CreateSessionResponse:
    """Create a long-lived code-executor pod with the given TTL.

    The pod is guaranteed to be torn down at or before the TTL expires, even
    if the API service crashes and restarts. The admission slot covers pod
    creation only, not the session lifetime.
    """
    slot = await _admit(request, OPERATION_SESSION_CREATE)
    try:
        response = await run_in_threadpool(_run_create_session, req)
    except HTTPException:
        raise
    except Exception:
        EXECUTIONS_COMPLETED.labels(OPERATION_SESSION_CREATE, OUTCOME_ERROR).inc()
        raise
    finally:
        slot.release()
    _record_outcome(OPERATION_SESSION_CREATE, timed_out=False)
    return response


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str) -> Response:
    """Tear down a session pod by ID."""
    try:
        deleted = get_executor().delete_session(session_id)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _run_session_bash(session_id: str, req: BashExecRequest) -> BashExecResponse:
    settings = get_settings()
    try:
        result = get_executor().execute_bash_in_session(
            session_id,
            cmd=req.cmd,
            timeout_ms=req.timeout_ms,
            max_output_bytes=settings.max_output_bytes,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc

    return BashExecResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
    )


@router.post(
    "/sessions/{session_id}/bash",
    response_model=BashExecResponse,
    status_code=status.HTTP_200_OK,
)
async def session_exec_bash(
    session_id: str, req: BashExecRequest, request: Request
) -> BashExecResponse:
    """Run a bash command inside an existing session.

    The session pod has no network access (enforced at session creation), and
    that restriction continues to apply for every command run via this route.
    """
    settings = get_settings()
    if req.timeout_ms > settings.max_exec_timeout_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"timeout_ms exceeds maximum of {settings.max_exec_timeout_ms} ms",
        )

    slot = await _admit(request, OPERATION_SESSION_BASH)
    try:
        response = await run_in_threadpool(_run_session_bash, session_id, req)
    except HTTPException:
        raise
    except Exception:
        EXECUTIONS_COMPLETED.labels(OPERATION_SESSION_BASH, OUTCOME_ERROR).inc()
        raise
    finally:
        slot.release()
    _record_outcome(OPERATION_SESSION_BASH, timed_out=response.timed_out)
    return response
