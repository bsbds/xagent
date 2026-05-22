"""Disk-backed temporary file store for live build preview sessions."""

from __future__ import annotations

import json
import mimetypes
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal, Optional, cast

from ...config import get_preview_tmp_dir

PreviewFileSource = Literal["upload", "generated"]


@dataclass(frozen=True)
class PreviewFileRef:
    file_id: str
    owner_user_id: int
    task_id: Optional[int]
    session_id: str
    filename: str
    path: Path
    mime_type: str
    file_size: int
    created_at: datetime
    source: PreviewFileSource
    workspace_relative_path: Optional[str] = None


@dataclass(frozen=True)
class PreviewPendingFile:
    file_id: str
    owner_user_id: int
    task_id: Optional[int]
    session_id: str
    filename: str
    path: Path
    mime_type: str
    created_at: datetime
    source: PreviewFileSource
    workspace_relative_path: Optional[str] = None


class PreviewFileStore:
    """Temporary preview file metadata and paths, backed by the filesystem."""

    def __init__(self) -> None:
        self._lock = RLock()

    def prepare_path(
        self,
        *,
        owner_user_id: int,
        filename: str,
        mime_type: Optional[str],
        task_id: Optional[int] = None,
        session_id: Optional[str] = None,
        file_id: Optional[str] = None,
        source: PreviewFileSource = "upload",
        workspace_relative_path: Optional[str] = None,
    ) -> PreviewPendingFile:
        resolved_file_id = self._safe_segment(file_id or str(uuid.uuid4()))
        resolved_session_id = self._safe_segment(session_id or str(uuid.uuid4()))
        clean_filename = Path(filename).name
        file_dir = self._file_dir(resolved_file_id)
        file_dir.mkdir(parents=True, exist_ok=True)
        return PreviewPendingFile(
            file_id=resolved_file_id,
            owner_user_id=owner_user_id,
            task_id=task_id,
            session_id=resolved_session_id,
            filename=clean_filename,
            path=file_dir / "content",
            mime_type=mime_type
            or mimetypes.guess_type(clean_filename)[0]
            or "application/octet-stream",
            created_at=datetime.now(timezone.utc),
            source=source,
            workspace_relative_path=workspace_relative_path,
        )

    def commit(self, pending: PreviewPendingFile, *, file_size: int) -> PreviewFileRef:
        preview_file = PreviewFileRef(
            file_id=pending.file_id,
            owner_user_id=pending.owner_user_id,
            task_id=pending.task_id,
            session_id=pending.session_id,
            filename=pending.filename,
            path=pending.path,
            mime_type=pending.mime_type,
            file_size=file_size,
            created_at=pending.created_at,
            source=pending.source,
            workspace_relative_path=pending.workspace_relative_path,
        )
        with self._lock:
            self._write_metadata(preview_file)
            self._add_to_session(preview_file)
        return preview_file

    def register_generated_file(
        self,
        *,
        owner_user_id: int,
        source_path: Path,
        filename: Optional[str],
        mime_type: Optional[str],
        task_id: int,
        session_id: str,
        file_id: Optional[str] = None,
        workspace_relative_path: Optional[str] = None,
    ) -> PreviewFileRef:
        pending = self.prepare_path(
            owner_user_id=owner_user_id,
            filename=filename or source_path.name,
            mime_type=mime_type,
            task_id=task_id,
            session_id=session_id,
            file_id=file_id,
            source="generated",
            workspace_relative_path=workspace_relative_path,
        )
        try:
            shutil.copy2(source_path, pending.path)
            return self.commit(pending, file_size=pending.path.stat().st_size)
        except Exception:
            self.discard_pending(pending)
            raise

    def discard_pending(self, pending: PreviewPendingFile) -> None:
        shutil.rmtree(self._file_dir(pending.file_id), ignore_errors=True)

    def get(self, file_id: str) -> Optional[PreviewFileRef]:
        metadata_path = self._metadata_path(file_id)
        if not metadata_path.exists():
            return None
        try:
            return self._read_metadata(metadata_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def bind_to_task(
        self, file_ids: list[str], task_id: int, owner_user_id: int
    ) -> None:
        with self._lock:
            for file_id in file_ids:
                preview_file = self.get(file_id)
                if preview_file is None or preview_file.owner_user_id != owner_user_id:
                    continue
                rebound = PreviewFileRef(
                    file_id=preview_file.file_id,
                    owner_user_id=preview_file.owner_user_id,
                    task_id=task_id,
                    session_id=preview_file.session_id,
                    filename=preview_file.filename,
                    path=preview_file.path,
                    mime_type=preview_file.mime_type,
                    file_size=preview_file.file_size,
                    created_at=preview_file.created_at,
                    source=preview_file.source,
                    workspace_relative_path=preview_file.workspace_relative_path,
                )
                self._write_metadata(rebound)
                self._add_to_session(rebound)

    def find_session_file(
        self,
        *,
        owner_user_id: int,
        session_id: str,
        workspace_relative_path: str,
    ) -> Optional[PreviewFileRef]:
        normalized_path = self._normalize_workspace_path(workspace_relative_path)
        if normalized_path is None:
            return None

        session_path = self._session_path(owner_user_id, session_id)
        with self._lock:
            for file_id in self._read_session_file_ids(session_path):
                preview_file = self.get(file_id)
                if (
                    preview_file is not None
                    and preview_file.owner_user_id == owner_user_id
                    and preview_file.session_id == session_id
                    and self._normalize_workspace_path(
                        preview_file.workspace_relative_path
                    )
                    == normalized_path
                ):
                    return preview_file
        return None

    def clear_session(
        self, session_id: str, owner_user_id: Optional[int] = None
    ) -> None:
        owners: list[int]
        if owner_user_id is not None:
            owners = [owner_user_id]
        else:
            sessions_root = self._sessions_root()
            owners = (
                [
                    int(path.name)
                    for path in sessions_root.iterdir()
                    if path.is_dir() and path.name.isdigit()
                ]
                if sessions_root.exists()
                else []
            )

        with self._lock:
            for owner in owners:
                session_path = self._session_path(owner, session_id)
                file_ids = self._read_session_file_ids(session_path)
                for file_id in file_ids:
                    shutil.rmtree(self._file_dir(file_id), ignore_errors=True)
                try:
                    session_path.unlink()
                except FileNotFoundError:
                    pass

    def _root(self) -> Path:
        return get_preview_tmp_dir()

    def _files_root(self) -> Path:
        return self._root() / "files"

    def _sessions_root(self) -> Path:
        return self._root() / "sessions"

    def _file_dir(self, file_id: str) -> Path:
        return self._files_root() / self._safe_segment(file_id)

    def _metadata_path(self, file_id: str) -> Path:
        return self._file_dir(file_id) / "metadata.json"

    def _session_path(self, owner_user_id: int, session_id: str) -> Path:
        return (
            self._sessions_root()
            / str(owner_user_id)
            / f"{self._safe_segment(session_id)}.json"
        )

    def _write_metadata(self, preview_file: PreviewFileRef) -> None:
        metadata_path = self._metadata_path(preview_file.file_id)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(preview_file)
        data["path"] = str(preview_file.path)
        data["created_at"] = preview_file.created_at.isoformat()
        metadata_path.write_text(json.dumps(data), encoding="utf-8")

    def _read_metadata(self, metadata_path: Path) -> PreviewFileRef:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        path = Path(str(data["path"]))
        return PreviewFileRef(
            file_id=str(data["file_id"]),
            owner_user_id=int(data["owner_user_id"]),
            task_id=int(data["task_id"]) if data.get("task_id") is not None else None,
            session_id=str(data["session_id"]),
            filename=Path(str(data["filename"])).name,
            path=path,
            mime_type=str(data["mime_type"]),
            file_size=int(data["file_size"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            source=self._source_from_value(data.get("source")),
            workspace_relative_path=self._normalize_workspace_path(
                data.get("workspace_relative_path")
            ),
        )

    def _add_to_session(self, preview_file: PreviewFileRef) -> None:
        session_path = self._session_path(
            preview_file.owner_user_id, preview_file.session_id
        )
        file_ids = self._read_session_file_ids(session_path)
        if preview_file.file_id not in file_ids:
            file_ids.append(preview_file.file_id)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps({"file_ids": file_ids}), encoding="utf-8")

    def _read_session_file_ids(self, session_path: Path) -> list[str]:
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        file_ids = data.get("file_ids", [])
        if not isinstance(file_ids, list):
            return []
        return [str(file_id) for file_id in file_ids]

    def _safe_segment(self, value: str) -> str:
        clean = Path(str(value)).name.strip()
        return clean or str(uuid.uuid4())

    def _source_from_value(self, value: object) -> PreviewFileSource:
        if value in {"upload", "generated"}:
            return cast(PreviewFileSource, value)
        return "upload"

    def _normalize_workspace_path(self, value: object) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value.strip())
        if path.is_absolute():
            return None
        path_parts = [part for part in path.parts if part not in ("", ".")]
        if not path_parts or ".." in path_parts:
            return None
        return "/".join(path_parts)


preview_file_store = PreviewFileStore()
