"""In-memory file registry for live build preview sessions."""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Optional


@dataclass(frozen=True)
class PreviewFile:
    file_id: str
    owner_user_id: int
    task_id: Optional[int]
    session_id: str
    filename: str
    content: bytes
    mime_type: str
    created_at: datetime

    @property
    def file_size(self) -> int:
        return len(self.content)


class PreviewFileRegistry:
    """Process-local store for preview uploads and generated artifacts."""

    def __init__(self) -> None:
        self._files: dict[str, PreviewFile] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        owner_user_id: int,
        filename: str,
        content: bytes,
        mime_type: Optional[str],
        task_id: Optional[int] = None,
        session_id: Optional[str] = None,
        file_id: Optional[str] = None,
    ) -> PreviewFile:
        preview_file = PreviewFile(
            file_id=file_id or str(uuid.uuid4()),
            owner_user_id=owner_user_id,
            task_id=task_id,
            session_id=session_id or str(uuid.uuid4()),
            filename=Path(filename).name,
            content=content,
            mime_type=mime_type
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._files[preview_file.file_id] = preview_file
        return preview_file

    def get(self, file_id: str) -> Optional[PreviewFile]:
        with self._lock:
            return self._files.get(file_id)

    def bind_to_task(
        self, file_ids: list[str], task_id: int, owner_user_id: int
    ) -> None:
        with self._lock:
            for file_id in file_ids:
                preview_file = self._files.get(file_id)
                if preview_file is None or preview_file.owner_user_id != owner_user_id:
                    continue
                self._files[file_id] = PreviewFile(
                    file_id=preview_file.file_id,
                    owner_user_id=preview_file.owner_user_id,
                    task_id=task_id,
                    session_id=preview_file.session_id,
                    filename=preview_file.filename,
                    content=preview_file.content,
                    mime_type=preview_file.mime_type,
                    created_at=preview_file.created_at,
                )

    def clear_session(
        self, session_id: str, owner_user_id: Optional[int] = None
    ) -> None:
        with self._lock:
            to_delete = [
                file_id
                for file_id, preview_file in self._files.items()
                if preview_file.session_id == session_id
                and (
                    owner_user_id is None or preview_file.owner_user_id == owner_user_id
                )
            ]
            for file_id in to_delete:
                self._files.pop(file_id, None)


preview_file_registry = PreviewFileRegistry()
