from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e.app_harness import (
    configure_e2e_app_environment,
    create_e2e_user,
    disable_external_app_services,
    init_e2e_db,
    run_e2e_app_client,
    seed_registered_local_file,
)
from tests.e2e.minio_harness import MinioStorage, run_minio_storage
from xagent.web.models.uploaded_file import UploadedFile

pytestmark = [pytest.mark.e2e, pytest.mark.docker]


@pytest.fixture
def minio_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[MinioStorage]:
    yield from run_minio_storage(monkeypatch)


def _record(session_factory: sessionmaker[Session], file_id: str) -> UploadedFile:
    db = session_factory()
    try:
        return db.query(UploadedFile).filter(UploadedFile.file_id == file_id).one()
    finally:
        db.close()


def test_startup_sync_repairs_only_files_that_need_durable_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    uploads_dir = configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()

    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="startup-sync-user")
        user_id = user.id

        legacy_file_id = str(uuid4())
        existing_file_id = str(uuid4())
        missing_remote_file_id = str(uuid4())
        missing_local_file_id = str(uuid4())

        existing_key = f"users/{user_id}/uploads/{existing_file_id}/existing.txt"
        missing_remote_key = (
            f"users/{user_id}/uploads/{missing_remote_file_id}/missing-remote.txt"
        )
        missing_local_key = (
            f"users/{user_id}/uploads/{missing_local_file_id}/missing-local.txt"
        )
        minio_storage.put_object(existing_key, b"already durable\n")

        legacy = seed_registered_local_file(
            db,
            uploads_dir=uploads_dir,
            user_id=user_id,
            filename="legacy.txt",
            content=b"legacy needs upload\n",
            file_id=legacy_file_id,
            mime_type="text/plain",
            storage_status="legacy",
        )
        seed_registered_local_file(
            db,
            uploads_dir=uploads_dir,
            user_id=user_id,
            filename="existing.txt",
            content=b"local should not overwrite remote\n",
            file_id=existing_file_id,
            mime_type="text/plain",
            storage_backend="s3",
            storage_key=existing_key,
            storage_status="available",
        )
        seed_registered_local_file(
            db,
            uploads_dir=uploads_dir,
            user_id=user_id,
            filename="missing-remote.txt",
            content=b"remote should be repaired\n",
            file_id=missing_remote_file_id,
            mime_type="text/plain",
            storage_backend="s3",
            storage_key=missing_remote_key,
            storage_status="available",
        )
        missing_local = seed_registered_local_file(
            db,
            uploads_dir=uploads_dir,
            user_id=user_id,
            filename="missing-local.txt",
            content=b"this file disappears before startup\n",
            file_id=missing_local_file_id,
            mime_type="text/plain",
            storage_backend="s3",
            storage_key=missing_local_key,
            storage_status="available",
        )
        missing_local.path.unlink()
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user_id,
    ) as app:
        legacy_record = _record(app.session_factory, legacy_file_id)
        existing_record = _record(app.session_factory, existing_file_id)
        missing_remote_record = _record(app.session_factory, missing_remote_file_id)
        missing_local_record = _record(app.session_factory, missing_local_file_id)

        assert legacy_record.storage_backend == "s3"
        assert legacy_record.storage_status == "available"
        assert legacy_record.storage_key == (
            f"users/{user_id}/uploads/{legacy_file_id}/legacy.txt"
        )
        assert minio_storage.object_bytes(str(legacy_record.storage_key)) == (
            b"legacy needs upload\n"
        )
        assert legacy.path.exists()

        assert existing_record.storage_key == existing_key
        assert minio_storage.object_bytes(existing_key) == b"already durable\n"

        assert missing_remote_record.storage_key == missing_remote_key
        assert minio_storage.object_bytes(missing_remote_key) == (
            b"remote should be repaired\n"
        )

        assert missing_local_record.storage_key == missing_local_key
        assert not minio_storage.exists(missing_local_key)
