from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...core.file_storage.factory import get_file_storage
from ..models.uploaded_file import UploadedFile


def uploaded_file_storage_path(file_record: UploadedFile) -> str:
    return str(getattr(file_record, "storage_path"))


def uploaded_file_name(file_record: UploadedFile) -> str:
    return str(getattr(file_record, "filename"))


def uploaded_file_storage_key(file_record: UploadedFile) -> str:
    return str(getattr(file_record, "storage_key", "") or "")


def uploaded_file_storage_status(file_record: UploadedFile) -> str:
    return str(getattr(file_record, "storage_status", "") or "")


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


def has_durable_object(file_record: UploadedFile) -> bool:
    return bool(
        uploaded_file_storage_key(file_record)
        and uploaded_file_storage_status(file_record) == "available"
    )


def iter_file_handle(handle: Any) -> Any:
    try:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


def open_uploaded_file(file_record: UploadedFile) -> Any:
    if has_durable_object(file_record):
        return get_file_storage().open_read(uploaded_file_storage_key(file_record))
    return Path(uploaded_file_storage_path(file_record)).open("rb")


def delete_uploaded_file_object(file_record: UploadedFile) -> None:
    if has_durable_object(file_record):
        get_file_storage().delete(uploaded_file_storage_key(file_record))


def delete_uploaded_file_storage_key(storage_key: str) -> None:
    if storage_key:
        get_file_storage().delete(storage_key)


def materialize_uploaded_file(file_record: UploadedFile) -> Path:
    local_path = Path(uploaded_file_storage_path(file_record))
    if local_path.exists() and local_path.is_file():
        return local_path

    if has_durable_object(file_record):
        return get_file_storage().materialize(
            uploaded_file_storage_key(file_record),
            uploaded_file_name(file_record),
        )

    return local_path


def ensure_uploaded_file_local_path(file_record: UploadedFile) -> Path:
    local_path = Path(uploaded_file_storage_path(file_record))
    if local_path.exists() and local_path.is_file():
        return local_path

    if has_durable_object(file_record):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            get_file_storage().open_read(uploaded_file_storage_key(file_record)) as src,
            local_path.open("wb") as dst,
        ):
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        return local_path

    return local_path


def guess_media_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    return media_type or "application/octet-stream"


def upload_uploaded_file_to_durable(
    file_record: UploadedFile,
    *,
    local_path: Path,
    user_id: int | None = None,
    mime_type: str | None = None,
    storage_key: str | None = None,
) -> UploadedFile:
    owner_id = int(user_id if user_id is not None else getattr(file_record, "user_id"))
    file_id = str(getattr(file_record, "file_id"))
    filename = uploaded_file_name(file_record) or local_path.name
    resolved_storage_key = storage_key or build_upload_storage_key(
        owner_id, file_id, filename
    )
    stored_object = get_file_storage().put_file(
        local_path,
        resolved_storage_key,
        mime_type or getattr(file_record, "mime_type", None),
    )
    setattr(file_record, "storage_backend", stored_object.backend)
    setattr(file_record, "storage_key", stored_object.key)
    setattr(file_record, "storage_uri", stored_object.uri)
    setattr(file_record, "checksum", stored_object.checksum)
    setattr(file_record, "etag", stored_object.etag)
    setattr(file_record, "storage_status", "available")
    return file_record


def create_uploaded_file_from_local_path(
    *,
    local_path: Path,
    user_id: int,
    filename: str | None = None,
    file_id: str | None = None,
    task_id: int | None = None,
    mime_type: str | None = None,
    storage_key: str | None = None,
) -> UploadedFile:
    resolved_filename = filename or local_path.name
    resolved_mime_type = mime_type or guess_media_type(resolved_filename)
    file_record = UploadedFile(
        file_id=file_id or str(uuid4()),
        user_id=user_id,
        task_id=task_id,
        filename=Path(resolved_filename).name,
        storage_path=str(local_path),
        mime_type=resolved_mime_type,
        file_size=local_path.stat().st_size,
        storage_status="pending",
    )
    upload_uploaded_file_to_durable(
        file_record,
        local_path=local_path,
        user_id=user_id,
        mime_type=resolved_mime_type,
        storage_key=storage_key,
    )
    return file_record
