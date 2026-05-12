import pytest

from xagent.core.file_storage.factory import get_file_storage
from xagent.web.models.uploaded_file import UploadedFile
from xagent.web.services.managed_file_ref import (
    DurableStorageOperationError,
    ManagedFileRef,
)


def _configure_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    monkeypatch.setenv("XAGENT_FILE_MATERIALIZE_DIR", str(tmp_path / "materialized"))
    get_file_storage.cache_clear()


def _record(local_path, **overrides):
    values = {
        "file_id": "file-123",
        "user_id": 7,
        "filename": local_path.name,
        "storage_path": str(local_path),
        "storage_status": "legacy",
        "mime_type": "text/plain",
        "file_size": 0,
    }
    values.update(overrides)
    return UploadedFile(**values)


def test_ensure_local_returns_existing_local_file(tmp_path):
    source = tmp_path / "uploads" / "local.txt"
    source.parent.mkdir()
    source.write_text("local content", encoding="utf-8")
    record = _record(source)

    assert ManagedFileRef(record).ensure_local() == source


def test_ensure_local_restores_missing_file_from_durable_storage(monkeypatch, tmp_path):
    _configure_storage(monkeypatch, tmp_path)
    storage = get_file_storage()
    stored = storage.put_bytes(b"durable content", "users/7/uploads/file-123/local.txt")
    local_path = tmp_path / "uploads" / "local.txt"
    record = _record(
        local_path,
        storage_backend=stored.backend,
        storage_key=stored.key,
        storage_uri=stored.uri,
        storage_status="available",
    )

    restored = ManagedFileRef(record).ensure_local()

    assert restored == local_path
    assert restored.read_text(encoding="utf-8") == "durable content"


def test_materialize_uses_temp_dir_when_original_path_is_missing(monkeypatch, tmp_path):
    _configure_storage(monkeypatch, tmp_path)
    storage = get_file_storage()
    stored = storage.put_bytes(
        b"preview content", "users/7/uploads/file-123/preview.txt"
    )
    local_path = tmp_path / "uploads" / "preview.txt"
    record = _record(
        local_path,
        storage_backend=stored.backend,
        storage_key=stored.key,
        storage_uri=stored.uri,
        storage_status="available",
    )

    materialized = ManagedFileRef(record).materialize()

    assert materialized.is_relative_to(tmp_path / "materialized")
    assert materialized.name == "preview.txt"
    assert materialized.read_text(encoding="utf-8") == "preview content"
    assert not local_path.exists()


def test_open_read_streams_from_durable_when_available(monkeypatch, tmp_path):
    _configure_storage(monkeypatch, tmp_path)
    storage = get_file_storage()
    stored = storage.put_bytes(b"stream me", "users/7/uploads/file-123/stream.txt")
    local_path = tmp_path / "uploads" / "stream.txt"
    record = _record(
        local_path,
        storage_backend=stored.backend,
        storage_key=stored.key,
        storage_uri=stored.uri,
        storage_status="available",
    )

    with ManagedFileRef(record).open_read() as handle:
        assert handle.read() == b"stream me"


def test_open_read_prefers_existing_local_file_over_durable(monkeypatch, tmp_path):
    _configure_storage(monkeypatch, tmp_path)
    storage = get_file_storage()
    stored = storage.put_bytes(
        b"stale durable content", "users/7/uploads/file-123/current.txt"
    )
    local_path = tmp_path / "uploads" / "current.txt"
    local_path.parent.mkdir()
    local_path.write_bytes(b"current local content")
    record = _record(
        local_path,
        storage_backend=stored.backend,
        storage_key=stored.key,
        storage_uri=stored.uri,
        storage_status="available",
    )

    with ManagedFileRef(record).open_read() as handle:
        assert handle.read() == b"current local content"


def test_sync_to_durable_uploads_local_file_and_updates_record(monkeypatch, tmp_path):
    _configure_storage(monkeypatch, tmp_path)
    source = tmp_path / "uploads" / "sync.txt"
    source.parent.mkdir()
    source.write_text("sync content", encoding="utf-8")
    record = _record(source, file_size=source.stat().st_size)

    stored = ManagedFileRef(record).sync_to_durable()

    assert stored.key == "users/7/uploads/file-123/sync.txt"
    assert record.storage_backend == "file"
    assert record.storage_key == stored.key
    assert record.storage_uri == stored.uri
    assert record.checksum is not None
    assert record.storage_status == "available"
    assert record.file_size == len("sync content")
    with get_file_storage().open_read(stored.key) as handle:
        assert handle.read() == b"sync content"


def test_sync_to_durable_accepts_custom_storage_key(monkeypatch, tmp_path):
    _configure_storage(monkeypatch, tmp_path)
    source = tmp_path / "workspace" / "output" / "report.txt"
    source.parent.mkdir(parents=True)
    source.write_text("report", encoding="utf-8")
    record = _record(source, file_id="file-output")

    stored = ManagedFileRef(record).sync_to_durable(
        storage_key="users/7/tasks/42/outputs/file-output/output/report.txt"
    )

    assert stored.key == "users/7/tasks/42/outputs/file-output/output/report.txt"
    assert record.storage_key == stored.key


def test_missing_local_and_missing_durable_key_raises(tmp_path):
    record = _record(tmp_path / "missing.txt")

    with pytest.raises(FileNotFoundError):
        ManagedFileRef(record).ensure_local()


class FailingStorage:
    def put_file(self, source, key, content_type=None):
        raise RuntimeError("remote write unavailable")

    def copy_to_path(self, key, target_path):
        raise RuntimeError("remote read unavailable")

    def materialize(self, key, filename=None):
        raise RuntimeError("remote preview unavailable")


def test_sync_to_durable_wraps_remote_write_failure(tmp_path):
    source = tmp_path / "uploads" / "sync-fails.txt"
    source.parent.mkdir()
    source.write_text("sync content", encoding="utf-8")
    record = _record(source, file_size=source.stat().st_size)

    with pytest.raises(DurableStorageOperationError, match="write durable object"):
        ManagedFileRef(record, storage=FailingStorage()).sync_to_durable()

    assert record.storage_status == "legacy"
    assert record.storage_key is None


def test_ensure_local_wraps_remote_read_failure_when_local_missing(tmp_path):
    local_path = tmp_path / "uploads" / "missing-local.txt"
    record = _record(
        local_path,
        storage_backend="s3",
        storage_key="users/7/uploads/file-123/missing-local.txt",
        storage_status="available",
    )

    with pytest.raises(DurableStorageOperationError, match="restore durable object"):
        ManagedFileRef(record, storage=FailingStorage()).ensure_local()


def test_materialize_wraps_remote_preview_failure_when_local_missing(tmp_path):
    local_path = tmp_path / "uploads" / "missing-preview.txt"
    record = _record(
        local_path,
        storage_backend="s3",
        storage_key="users/7/uploads/file-123/missing-preview.txt",
        storage_status="available",
    )

    with pytest.raises(
        DurableStorageOperationError, match="materialize durable object"
    ):
        ManagedFileRef(record, storage=FailingStorage()).materialize()
