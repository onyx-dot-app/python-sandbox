from __future__ import annotations

from pydantic import BaseModel, Field, StrictInt, StrictStr

from app.services.executor_base import EntryKind


class ExecuteFile(BaseModel):
    path: StrictStr = Field(..., description="Relative file path within the execution workspace.")
    file_id: StrictStr = Field(
        ..., description="UUID of a previously uploaded file to use for execution."
    )


class WorkspaceFile(BaseModel):
    path: StrictStr
    kind: EntryKind
    file_id: StrictStr | None = Field(
        None, description="ID of the file in storage (only for files, not directories)."
    )


class ExecuteRequest(BaseModel):
    code: StrictStr = Field(..., description="Python source to execute.")
    stdin: StrictStr | None = Field(None, description="Optional stdin passed to the program.")
    timeout_ms: StrictInt = Field(2000, ge=1, description="Execution timeout in milliseconds.")
    last_line_interactive: bool = Field(
        True,
        description=(
            "If True, the last line of code will print its value to stdout if it's a bare "
            "expression (like Jupyter notebooks or Python REPL). Only the last line is affected; "
            "earlier expressions are not printed. Default is True."
        ),
    )
    files: list[ExecuteFile] = Field(
        default_factory=list,
        description="Optional collection of files to stage in the execution workspace.",
    )


class ExecuteResponse(BaseModel):
    stdout: StrictStr
    stderr: StrictStr
    exit_code: int | None
    timed_out: bool
    duration_ms: StrictInt
    files: list[WorkspaceFile] = Field(
        default_factory=list,
        description="Snapshot of the execution workspace after completion.",
    )


class UploadFileResponse(BaseModel):
    file_id: StrictStr = Field(..., description="Unique identifier for the uploaded file.")
    filename: StrictStr = Field(..., description="Original filename as provided during upload.")
    size_bytes: StrictInt = Field(..., description="Size of the uploaded file in bytes.")


class FileMetadataResponse(BaseModel):
    file_id: StrictStr
    filename: StrictStr
    size_bytes: StrictInt
    upload_time: float = Field(..., description="Unix timestamp of when the file was uploaded.")


class ListFilesResponse(BaseModel):
    files: list[FileMetadataResponse] = Field(
        default_factory=list,
        description="List of all stored files with their metadata.",
    )
