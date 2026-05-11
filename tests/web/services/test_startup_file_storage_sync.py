from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from xagent.core.file_storage.types import StoredObject
from xagent.web.models.database import Base
from xagent.web.models.uploaded_file import UploadedFile
from xagent.web.models.user import User
from xagent.web.services.startup_file_storage_sync import (
    sync_registered_files_to_durable_storage,
)


class FakeStorage:
    def __init__(self, existing_keys: set[str] | None = None, backend: str = "s3"):
        self.backend = backend
        self.existing_keys = set(existing_keys or set())
        self.list_calls: list[str] = []
        self.put_calls: list[tuple[Path, str]] = []

    def list(self, prefix: str) -> list[StoredObject]:
        self.list_calls.append(prefix)
        normalized = prefix.rstrip("/")
        return [
            StoredObject(
                backend=self.backend,
                key=key,
                uri=f"s3://bucket/{key}",
                size=1,
            )
            for key in sorted(self.existing_keys)
            if key.startswith(normalized + "/") or key == normalized
        ]

    def put_file(
        self, source: Path, key: str, content_type: str | None = None
    ) -> StoredObject:
        del content_type
        self.put_calls.append((source, key))
        self.existing_keys.add(key)
        return StoredObject(
            backend=self.backend,
            key=key,
            uri=f"s3://bucket/{key}",
            size=source.stat().st_size,
            checksum="checksum",
            etag="etag",
        )


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def _user(db, username="sync-user"):
    user = User(username=username, password_hash="hash", is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _record(db, *, user, local_path: Path, file_id: str, **overrides) -> UploadedFile:
    values = {
        "file_id": file_id,
        "user_id": int(user.id),
        "filename": local_path.name,
        "storage_path": str(local_path),
        "mime_type": "text/plain",
        "file_size": local_path.stat().st_size if local_path.exists() else 0,
        "storage_status": "legacy",
    }
    values.update(overrides)
    record = UploadedFile(**values)
    db.add(record)
    db.flush()
    return record


def test_sync_uploads_registered_local_file_missing_remote_object(tmp_path):
    db = _session()
    user = _user(db)
    local_path = tmp_path / "uploads" / "input.txt"
    local_path.parent.mkdir()
    local_path.write_text("content", encoding="utf-8")
    record = _record(db, user=user, local_path=local_path, file_id="file-123")
    db.commit()
    storage = FakeStorage()

    result = sync_registered_files_to_durable_storage(db, storage=storage)

    db.refresh(record)
    expected_key = f"users/{int(user.id)}/uploads/file-123/input.txt"
    assert result.scanned == 1
    assert result.uploaded == 1
    assert result.already_present == 0
    assert result.skipped_missing_local == 0
    assert storage.list_calls == [f"users/{int(user.id)}"]
    assert storage.put_calls == [(local_path, expected_key)]
    assert record.storage_backend == "s3"
    assert record.storage_key == expected_key
    assert record.storage_uri == f"s3://bucket/{expected_key}"
    assert record.checksum == "checksum"
    assert record.etag == "etag"
    assert record.storage_status == "available"


def test_sync_does_not_upload_when_remote_key_is_present(tmp_path):
    db = _session()
    user = _user(db)
    local_path = tmp_path / "uploads" / "input.txt"
    local_path.parent.mkdir()
    local_path.write_text("content", encoding="utf-8")
    key = f"users/{int(user.id)}/uploads/file-123/input.txt"
    record = _record(
        db,
        user=user,
        local_path=local_path,
        file_id="file-123",
        storage_backend="s3",
        storage_key=key,
        storage_uri=f"s3://bucket/{key}",
        storage_status="available",
    )
    db.commit()
    storage = FakeStorage({key})

    result = sync_registered_files_to_durable_storage(db, storage=storage)

    db.refresh(record)
    assert result.scanned == 1
    assert result.already_present == 1
    assert result.uploaded == 0
    assert storage.put_calls == []
    assert record.storage_status == "available"


def test_sync_reuploads_available_row_when_remote_key_is_missing(tmp_path):
    db = _session()
    user = _user(db)
    local_path = tmp_path / "uploads" / "input.txt"
    local_path.parent.mkdir()
    local_path.write_text("content", encoding="utf-8")
    key = f"users/{int(user.id)}/uploads/file-123/input.txt"
    record = _record(
        db,
        user=user,
        local_path=local_path,
        file_id="file-123",
        storage_backend="s3",
        storage_key=key,
        storage_uri=f"s3://bucket/{key}",
        storage_status="available",
    )
    db.commit()
    storage = FakeStorage()

    result = sync_registered_files_to_durable_storage(db, storage=storage)

    db.refresh(record)
    assert result.uploaded == 1
    assert storage.put_calls == [(local_path, key)]
    assert record.storage_status == "available"
    assert record.etag == "etag"


def test_sync_skips_missing_local_file(tmp_path):
    db = _session()
    user = _user(db)
    local_path = tmp_path / "uploads" / "missing.txt"
    record = _record(db, user=user, local_path=local_path, file_id="file-missing")
    db.commit()
    storage = FakeStorage()

    result = sync_registered_files_to_durable_storage(db, storage=storage)

    db.refresh(record)
    assert result.scanned == 1
    assert result.uploaded == 0
    assert result.skipped_missing_local == 1
    assert storage.put_calls == []
    assert record.storage_status == "legacy"


def test_sync_lists_once_per_user_for_many_files(tmp_path):
    db = _session()
    user = _user(db)
    for index in range(3):
        local_path = tmp_path / "uploads" / f"input-{index}.txt"
        local_path.parent.mkdir(exist_ok=True)
        local_path.write_text(f"content {index}", encoding="utf-8")
        _record(db, user=user, local_path=local_path, file_id=f"file-{index}")
    db.commit()
    storage = FakeStorage()

    result = sync_registered_files_to_durable_storage(db, storage=storage)

    assert result.scanned == 3
    assert result.uploaded == 3
    assert storage.list_calls == [f"users/{int(user.id)}"]


def test_sync_skips_non_s3_backend(tmp_path):
    db = _session()
    user = _user(db)
    local_path = tmp_path / "uploads" / "input.txt"
    local_path.parent.mkdir()
    local_path.write_text("content", encoding="utf-8")
    _record(db, user=user, local_path=local_path, file_id="file-123")
    db.commit()
    storage = FakeStorage(backend="file")

    result = sync_registered_files_to_durable_storage(db, storage=storage)

    assert result.scanned == 0
    assert result.uploaded == 0
    assert result.skipped_backend == 1
    assert storage.list_calls == []
    assert storage.put_calls == []
