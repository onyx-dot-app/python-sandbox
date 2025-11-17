from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileMetadata:
    file_id: str
    filename: str
    size_bytes: int
    upload_time: float


class FileStorageService:
    """Service for managing uploaded files with UUID-based storage."""

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, file_id: str) -> Path:
        """Get the filesystem path for a file ID."""
        return self.storage_dir / file_id

    def _get_metadata_path(self, file_id: str) -> Path:
        """Get the filesystem path for file metadata."""
        return self.storage_dir / f"{file_id}.meta.json"

    def save_file(self, content: bytes, filename: str) -> str:
        """Save file content and return a unique file ID.

        Args:
            content: Raw file bytes to store
            filename: Original filename for metadata

        Returns:
            UUID string identifying the stored file
        """
        file_id = str(uuid.uuid4())
        file_path = self._get_file_path(file_id)
        metadata_path = self._get_metadata_path(file_id)

        # Write file content
        file_path.write_bytes(content)

        # Write metadata
        metadata = FileMetadata(
            file_id=file_id,
            filename=filename,
            size_bytes=len(content),
            upload_time=time.time(),
        )
        metadata_path.write_text(json.dumps(asdict(metadata)), encoding="utf-8")

        return file_id

    def get_file(self, file_id: str) -> tuple[bytes, FileMetadata]:
        """Retrieve file content and metadata by ID.

        Args:
            file_id: UUID of the file to retrieve

        Returns:
            Tuple of (file_content, metadata)

        Raises:
            FileNotFoundError: If file_id doesn't exist
        """
        file_path = self._get_file_path(file_id)
        metadata_path = self._get_metadata_path(file_id)

        if not file_path.exists():
            raise FileNotFoundError(f"File with ID '{file_id}' not found")

        content = file_path.read_bytes()

        # Read metadata if available, otherwise create minimal metadata
        if metadata_path.exists():
            meta_dict = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata = FileMetadata(**meta_dict)
        else:
            # Fallback for files without metadata
            metadata = FileMetadata(
                file_id=file_id,
                filename="unknown",
                size_bytes=len(content),
                upload_time=file_path.stat().st_mtime,
            )

        return content, metadata

    def delete_file(self, file_id: str) -> bool:
        """Delete a file and its metadata by ID.

        Args:
            file_id: UUID of the file to delete

        Returns:
            True if file was deleted, False if it didn't exist
        """
        file_path = self._get_file_path(file_id)
        metadata_path = self._get_metadata_path(file_id)

        existed = file_path.exists()

        # Remove both file and metadata if they exist
        if file_path.exists():
            file_path.unlink()
        if metadata_path.exists():
            metadata_path.unlink()

        return existed

    def list_files(self) -> list[FileMetadata]:
        """List all stored files with their metadata.

        Returns:
            List of FileMetadata objects for all stored files
        """
        result: list[FileMetadata] = []

        for metadata_path in self.storage_dir.glob("*.meta.json"):
            try:
                meta_dict = json.loads(metadata_path.read_text(encoding="utf-8"))
                result.append(FileMetadata(**meta_dict))
            except (json.JSONDecodeError, TypeError):
                # Skip invalid metadata files
                continue

        return result

    def cleanup_expired_files(self, max_age_sec: int) -> int:
        """Remove files older than the specified age.

        Args:
            max_age_sec: Maximum age in seconds before files are deleted

        Returns:
            Number of files deleted
        """
        current_time = time.time()
        deleted_count = 0

        for metadata_path in self.storage_dir.glob("*.meta.json"):
            try:
                meta_dict = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata = FileMetadata(**meta_dict)

                if (
                    current_time - metadata.upload_time > max_age_sec 
                    and self.delete_file(metadata.file_id)
                ):
                    deleted_count += 1
            except (json.JSONDecodeError, TypeError, FileNotFoundError):
                # Skip invalid or already-deleted files
                continue

        return deleted_count
