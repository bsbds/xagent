from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from ...core.file_storage import FsspecFileStorage, StoredObject, get_file_storage
from ..models.uploaded_file import UploadedFile


class DurableObjectMissingError(FileNotFoundError):
    """Raised when a registered file has no local copy or durable object."""


def safe_storage_filename(filename: str) -> str:
    safe_name = Path(filename).name.strip()
    return safe_name or "file"


def build_upload_storage_key(user_id: int, file_id: str, filename: str) -> str:
    return f"users/{user_id}/uploads/{file_id}/{safe_storage_filename(filename)}"


def build_task_output_storage_key(
    user_id: int, task_id: int, file_id: str, relative_path: str
) -> str:
    safe_relative_path = str(Path(relative_path.strip().lstrip("/")))
    if not safe_relative_path or ".." in Path(safe_relative_path).parts:
        safe_relative_path = safe_storage_filename(relative_path)
    return f"users/{user_id}/tasks/{task_id}/outputs/{file_id}/{safe_relative_path}"


def guess_media_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    return media_type or "application/octet-stream"


def iter_file_handle(handle: Any) -> Any:
    try:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


@dataclass
class ManagedFileRef:
    """Registered file handle with local-first durable fallback semantics."""

    record: UploadedFile
    storage: FsspecFileStorage = field(default_factory=get_file_storage)

    @property
    def local_path(self) -> Path:
        return Path(str(self.record.storage_path))

    @property
    def filename(self) -> str:
        return str(self.record.filename)

    @property
    def storage_key(self) -> str:
        return str(self.record.storage_key or "")

    @property
    def has_durable_object(self) -> bool:
        return bool(self.storage_key and self.record.storage_status == "available")

    def ensure_local(self) -> Path:
        path = self.local_path
        if path.exists() and path.is_file():
            return path

        if not self.has_durable_object:
            raise DurableObjectMissingError(path)

        return self.storage.copy_to_path(self.storage_key, path)

    def materialize(self) -> Path:
        path = self.local_path
        if path.exists() and path.is_file():
            return path

        if not self.has_durable_object:
            raise DurableObjectMissingError(path)

        return self.storage.materialize(self.storage_key, self.filename)

    def open_read(self) -> BinaryIO:
        return self.ensure_local().open("rb")

    def sync_to_durable(
        self,
        *,
        storage_key: str | None = None,
        mime_type: str | None = None,
    ) -> StoredObject:
        path = self.local_path
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(path)

        resolved_key = (
            storage_key
            or self.storage_key
            or build_upload_storage_key(
                int(getattr(self.record, "user_id")),
                str(getattr(self.record, "file_id")),
                self.filename or path.name,
            )
        )
        stored_object = self.storage.put_file(
            path,
            resolved_key,
            mime_type or getattr(self.record, "mime_type", None),
        )
        self.apply_stored_object(stored_object)
        setattr(self.record, "file_size", path.stat().st_size)
        return stored_object

    def apply_stored_object(self, stored_object: StoredObject) -> None:
        setattr(self.record, "storage_backend", stored_object.backend)
        setattr(self.record, "storage_key", stored_object.key)
        setattr(self.record, "storage_uri", stored_object.uri)
        setattr(self.record, "checksum", stored_object.checksum)
        setattr(self.record, "etag", stored_object.etag)
        setattr(self.record, "storage_status", "available")

    def delete_durable(self) -> None:
        if self.has_durable_object:
            self.storage.delete(self.storage_key)


def managed_file_from_record(file_record: UploadedFile) -> ManagedFileRef:
    return ManagedFileRef(file_record)


def ensure_uploaded_file_local_path(file_record: UploadedFile) -> Path:
    try:
        return ManagedFileRef(file_record).ensure_local()
    except DurableObjectMissingError:
        return Path(str(file_record.storage_path))


def create_uploaded_file_from_local_path(
    *,
    local_path: Path,
    user_id: int,
    filename: str | None = None,
    file_id: str | None = None,
    task_id: int | None = None,
    mime_type: str | None = None,
    storage_key: str | None = None,
    workspace_relative_path: str | None = None,
    workspace_category: str | None = None,
) -> UploadedFile:
    from .uploaded_file_store import create_unbound_uploaded_file_from_local_path

    return create_unbound_uploaded_file_from_local_path(
        local_path=local_path,
        user_id=user_id,
        filename=filename,
        file_id=file_id,
        task_id=task_id,
        mime_type=mime_type,
        storage_key=storage_key,
        workspace_relative_path=workspace_relative_path,
        workspace_category=workspace_category,
    )
