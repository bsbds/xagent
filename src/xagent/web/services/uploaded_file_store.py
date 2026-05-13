from __future__ import annotations

from pathlib import Path
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from ..models.uploaded_file import UploadedFile
from .managed_file_ref import ManagedFileRef, guess_media_type


def create_unbound_uploaded_file_from_local_path(
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
    file_record = build_uploaded_file_record(
        local_path=local_path,
        user_id=user_id,
        filename=filename,
        file_id=file_id,
        task_id=task_id,
        mime_type=mime_type,
        workspace_relative_path=workspace_relative_path,
        workspace_category=workspace_category,
    )
    ManagedFileRef(file_record).sync_to_durable(
        storage_key=storage_key,
        mime_type=str(file_record.mime_type),
    )
    return file_record


def build_uploaded_file_record(
    *,
    local_path: Path,
    user_id: int,
    filename: str | None = None,
    file_id: str | None = None,
    task_id: int | None = None,
    mime_type: str | None = None,
    workspace_relative_path: str | None = None,
    workspace_category: str | None = None,
) -> UploadedFile:
    resolved_filename = filename or local_path.name
    resolved_mime_type = mime_type or guess_media_type(resolved_filename)
    return UploadedFile(
        file_id=file_id or str(uuid4()),
        user_id=user_id,
        task_id=task_id,
        filename=Path(resolved_filename).name,
        storage_path=str(local_path),
        mime_type=resolved_mime_type,
        file_size=local_path.stat().st_size,
        storage_status="pending",
        workspace_relative_path=workspace_relative_path,
        workspace_category=workspace_category,
    )


class UploadedFileStore:
    """Coordinates UploadedFile rows with durable object storage."""

    def __init__(self, db: Session):
        self.db = db

    def create_from_local_path(
        self,
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
        file_record = build_uploaded_file_record(
            local_path=local_path,
            user_id=user_id,
            filename=filename,
            file_id=file_id,
            task_id=task_id,
            mime_type=mime_type,
            workspace_relative_path=workspace_relative_path,
            workspace_category=workspace_category,
        )
        self.db.add(file_record)
        self.db.flush()
        self.sync_existing(
            file_record,
            storage_key=storage_key,
            mime_type=str(file_record.mime_type),
        )
        return file_record

    def sync_existing(
        self,
        file_record: UploadedFile,
        *,
        storage_key: str | None = None,
        mime_type: str | None = None,
    ) -> UploadedFile:
        ManagedFileRef(file_record).sync_to_durable(
            storage_key=storage_key,
            mime_type=mime_type,
        )
        self.db.flush()
        return file_record

    def upsert_by_storage_path(
        self,
        *,
        user_id: int,
        filename: str,
        storage_path: Path,
        mime_type: str | None,
        file_size: int,
    ) -> UploadedFile:
        storage_path_str = str(storage_path)
        file_record = (
            self.db.query(UploadedFile)
            .filter(UploadedFile.storage_path == storage_path_str)
            .first()
        )
        if file_record is None:
            file_record = build_uploaded_file_record(
                local_path=storage_path,
                user_id=user_id,
                filename=filename,
                mime_type=mime_type,
            )
            self.db.add(file_record)
            self.db.flush()
        else:
            unchanged = self._has_current_durable_object(
                file_record, file_size=file_size, mime_type=mime_type
            )
            file_record.filename = filename  # type: ignore[assignment]
            file_record.file_size = int(file_size)  # type: ignore[assignment]
            if mime_type is not None:
                file_record.mime_type = mime_type  # type: ignore[assignment]
            if unchanged:
                self.db.flush()
                return file_record

        return self.sync_existing(file_record, mime_type=mime_type)

    def delete(
        self,
        file_record: UploadedFile,
        *,
        delete_local: bool = True,
        local_root: Optional[Path] = None,
    ) -> None:
        self.db.delete(file_record)
        self.db.flush()
        ManagedFileRef(file_record).delete_durable()
        if delete_local:
            self._delete_local(file_record, local_root=local_root)

    @staticmethod
    def ensure_local(file_record: UploadedFile) -> Path:
        return ManagedFileRef(file_record).ensure_local()

    @staticmethod
    def _has_current_durable_object(
        file_record: UploadedFile,
        *,
        file_size: int,
        mime_type: str | None,
    ) -> bool:
        if getattr(file_record, "storage_status", None) != "available":
            return False
        if not getattr(file_record, "storage_key", None):
            return False
        if int(getattr(file_record, "file_size", 0) or 0) != int(file_size):
            return False
        if mime_type is None:
            return True
        return getattr(file_record, "mime_type", None) == mime_type

    @staticmethod
    def _delete_local(
        file_record: UploadedFile, *, local_root: Optional[Path] = None
    ) -> None:
        local_path = Path(str(file_record.storage_path))
        if local_root is not None:
            resolved_path = local_path.resolve()
            if not resolved_path.is_relative_to(local_root.resolve()):
                return
            local_path = resolved_path
        if local_path.exists() and local_path.is_file():
            local_path.unlink()
