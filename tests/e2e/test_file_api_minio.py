from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e.app_harness import (
    build_access_token,
    configure_e2e_app_environment,
    create_e2e_user,
    disable_external_app_services,
    init_e2e_db,
    run_e2e_app_client,
)
from tests.e2e.minio_harness import MinioStorage, run_minio_storage
from xagent.web.models.uploaded_file import UploadedFile

pytestmark = [pytest.mark.e2e, pytest.mark.docker]


@pytest.fixture
def minio_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[MinioStorage]:
    yield from run_minio_storage(monkeypatch)


def _upload_text_file(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    response = client.post(
        "/api/files/upload",
        files={"file": ("source.txt", b"source from minio\n", "text/plain")},
        data={"task_type": "general"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def _record(session_factory: sessionmaker[Session], file_id: str) -> UploadedFile:
    db = session_factory()
    try:
        return db.query(UploadedFile).filter(UploadedFile.file_id == file_id).one()
    finally:
        db.close()


def test_download_and_preview_materialize_uploaded_file_from_minio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    uploads_dir = configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    del uploads_dir, minio_storage
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="file-api-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user.id,
    ) as app:
        uploaded = _upload_text_file(app.client, app.headers)
        file_id = uploaded["file_id"]
        record = _record(app.session_factory, file_id)
        local_path = Path(str(record.storage_path))
        assert local_path.exists()
        local_path.unlink()

        download = app.client.get(
            f"/api/files/download/{file_id}",
            headers=app.headers,
        )
        assert download.status_code == 200
        assert download.content == b"source from minio\n"
        assert local_path.read_bytes() == b"source from minio\n"

        local_path.unlink()
        preview = app.client.get(
            f"/api/files/preview/{file_id}",
            headers=app.headers,
        )
        assert preview.status_code == 200
        assert preview.content == b"source from minio\n"
        assert not local_path.exists()


def test_upload_returns_503_and_rolls_back_when_minio_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    del minio_storage
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="file-upload-outage-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user.id,
    ) as app:
        from xagent.core.file_storage.storage import FsspecFileStorage

        def fail_put_file(
            self: FsspecFileStorage,
            source: Path,
            key: str,
            content_type: str | None = None,
        ) -> None:
            del source, key, content_type
            raise RuntimeError("simulated MinIO write outage")

        monkeypatch.setattr(FsspecFileStorage, "put_file", fail_put_file)

        upload = app.client.post(
            "/api/files/upload",
            files={"file": ("outage.txt", b"outage content\n", "text/plain")},
            data={"task_type": "general"},
            headers=app.headers,
        )

        assert upload.status_code == 503
        assert "durable storage" in upload.json()["detail"].lower()
        assert not list((tmp_path / "uploads").rglob("outage.txt"))

        db = app.session_factory()
        try:
            assert (
                db.query(UploadedFile)
                .filter(
                    UploadedFile.user_id == user.id,
                    UploadedFile.filename == "outage.txt",
                )
                .first()
                is None
            )
        finally:
            db.close()


def test_download_and_preview_return_503_when_minio_read_fails_without_local_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    del minio_storage
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="file-read-outage-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user.id,
    ) as app:
        uploaded = _upload_text_file(app.client, app.headers)
        file_id = uploaded["file_id"]
        record = _record(app.session_factory, file_id)
        local_path = Path(str(record.storage_path))
        assert local_path.exists()
        local_path.unlink()

        from xagent.core.file_storage.storage import FsspecFileStorage

        def fail_open_read(self: FsspecFileStorage, key: str) -> None:
            del key
            raise RuntimeError("simulated MinIO read outage")

        monkeypatch.setattr(FsspecFileStorage, "open_read", fail_open_read)

        download = app.client.get(
            f"/api/files/download/{file_id}",
            headers=app.headers,
        )
        preview = app.client.get(
            f"/api/files/preview/{file_id}",
            headers=app.headers,
        )

        assert download.status_code == 503
        assert "durable storage" in download.json()["detail"].lower()
        assert preview.status_code == 503
        assert "durable storage" in preview.json()["detail"].lower()
        assert not local_path.exists()


def test_download_serves_local_copy_when_minio_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    del minio_storage
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="file-local-read-outage-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user.id,
    ) as app:
        uploaded = _upload_text_file(app.client, app.headers)
        file_id = uploaded["file_id"]
        record = _record(app.session_factory, file_id)
        local_path = Path(str(record.storage_path))
        assert local_path.exists()

        from xagent.core.file_storage.storage import FsspecFileStorage

        def fail_open_read(self: FsspecFileStorage, key: str) -> None:
            del key
            raise RuntimeError("simulated MinIO read outage")

        monkeypatch.setattr(FsspecFileStorage, "open_read", fail_open_read)

        download = app.client.get(
            f"/api/files/download/{file_id}",
            headers=app.headers,
        )

        assert download.status_code == 200
        assert download.content == b"source from minio\n"


def test_delete_removes_uploaded_file_from_db_local_disk_and_minio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="file-delete-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user.id,
    ) as app:
        uploaded = _upload_text_file(app.client, app.headers)
        file_id = uploaded["file_id"]
        record = _record(app.session_factory, file_id)
        storage_key = str(record.storage_key)
        local_path = Path(str(record.storage_path))

        assert local_path.exists()
        assert minio_storage.exists(storage_key)

        delete = app.client.delete(f"/api/files/{file_id}", headers=app.headers)
        assert delete.status_code == 200
        assert not local_path.exists()
        assert not minio_storage.exists(storage_key)

        db = app.session_factory()
        try:
            assert (
                db.query(UploadedFile).filter(UploadedFile.file_id == file_id).first()
                is None
            )
        finally:
            db.close()


def test_delete_keeps_db_row_when_durable_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        user = create_e2e_user(db, username="file-delete-failure-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=user.username,
        user_id=user.id,
    ) as app:
        uploaded = _upload_text_file(app.client, app.headers)
        file_id = uploaded["file_id"]
        record = _record(app.session_factory, file_id)
        storage_key = str(record.storage_key)
        local_path = Path(str(record.storage_path))

        from xagent.core.file_storage.storage import FsspecFileStorage

        real_delete = FsspecFileStorage.delete

        def fail_target_delete(self: FsspecFileStorage, key: str) -> None:
            if key == storage_key:
                raise RuntimeError("simulated durable delete failure")
            real_delete(self, key)

        monkeypatch.setattr(FsspecFileStorage, "delete", fail_target_delete)

        assert local_path.exists()
        assert minio_storage.exists(storage_key)

        delete = app.client.delete(f"/api/files/{file_id}", headers=app.headers)
        assert delete.status_code == 503

        assert local_path.exists()
        assert minio_storage.exists(storage_key)
        assert minio_storage.object_bytes(storage_key) == b"source from minio\n"

        db = app.session_factory()
        try:
            assert (
                db.query(UploadedFile).filter(UploadedFile.file_id == file_id).first()
                is not None
            )
        finally:
            db.close()


def test_file_routes_reject_cross_user_access_and_keep_minio_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minio_storage: MinioStorage,
) -> None:
    configure_e2e_app_environment(monkeypatch, tmp_path=tmp_path)
    disable_external_app_services(monkeypatch)
    SessionLocal = init_e2e_db()
    db = SessionLocal()
    try:
        owner = create_e2e_user(db, username="owner-user")
        other = create_e2e_user(db, username="other-user")
    finally:
        db.close()

    with run_e2e_app_client(
        monkeypatch,
        username=owner.username,
        user_id=owner.id,
    ) as app:
        uploaded = _upload_text_file(app.client, app.headers)
        file_id = uploaded["file_id"]
        record = _record(app.session_factory, file_id)
        storage_key = str(record.storage_key)
        other_headers = {
            "Authorization": (
                f"Bearer {build_access_token(username=other.username, user_id=other.id)}"
            )
        }

        for method, path in [
            ("GET", f"/api/files/download/{file_id}"),
            ("GET", f"/api/files/preview/{file_id}"),
            ("DELETE", f"/api/files/{file_id}"),
        ]:
            response = app.client.request(method, path, headers=other_headers)
            assert response.status_code == 403

        assert minio_storage.exists(storage_key)
        assert minio_storage.object_bytes(storage_key) == b"source from minio\n"
