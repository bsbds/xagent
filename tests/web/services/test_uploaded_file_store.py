from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from xagent.core.file_storage.factory import get_file_storage
from xagent.web.models.database import Base
from xagent.web.models.uploaded_file import UploadedFile
from xagent.web.models.user import User
from xagent.web.services.uploaded_file_store import UploadedFileStore


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def _user(db):
    user = User(username="store-user", password_hash="hash", is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_create_from_local_path_persists_record_and_syncs_durable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()
    db = _session()
    user = _user(db)
    source = tmp_path / "uploads" / "input.txt"
    source.parent.mkdir()
    source.write_text("store content", encoding="utf-8")

    record = UploadedFileStore(db).create_from_local_path(
        local_path=source,
        user_id=int(user.id),
        file_id="file-store",
        filename="input.txt",
        mime_type="text/plain",
    )
    db.commit()

    persisted = db.query(UploadedFile).filter_by(file_id="file-store").one()
    assert persisted.id == record.id
    assert persisted.storage_status == "available"
    assert persisted.storage_key == "users/1/uploads/file-store/input.txt"
    with get_file_storage().open_read(str(persisted.storage_key)) as handle:
        assert handle.read() == b"store content"


def test_sync_existing_refreshes_durable_object(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()
    db = _session()
    user = _user(db)
    source = tmp_path / "uploads" / "refresh.txt"
    source.parent.mkdir()
    source.write_text("old content", encoding="utf-8")
    store = UploadedFileStore(db)
    record = store.create_from_local_path(
        local_path=source,
        user_id=int(user.id),
        file_id="file-refresh",
        filename="refresh.txt",
    )
    storage_key = str(record.storage_key)

    source.write_text("new content", encoding="utf-8")
    store.sync_existing(record)
    db.commit()

    with get_file_storage().open_read(storage_key) as handle:
        assert handle.read() == b"new content"


def test_delete_removes_local_durable_and_db_record(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()
    db = _session()
    user = _user(db)
    source = tmp_path / "uploads" / "delete.txt"
    source.parent.mkdir()
    source.write_text("delete me", encoding="utf-8")
    store = UploadedFileStore(db)
    record = store.create_from_local_path(
        local_path=source,
        user_id=int(user.id),
        file_id="file-delete",
        filename="delete.txt",
    )
    storage_key = str(record.storage_key)
    assert source.exists()
    assert get_file_storage().exists(storage_key)

    store.delete(record, delete_local=True)
    db.commit()

    assert not source.exists()
    assert not get_file_storage().exists(storage_key)
    assert db.query(UploadedFile).filter_by(file_id="file-delete").first() is None


def test_upsert_by_storage_path_reuses_record_and_refreshes_durable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()
    db = _session()
    user = _user(db)
    source = tmp_path / "uploads" / "kb.md"
    source.parent.mkdir()
    source.write_text("first", encoding="utf-8")
    store = UploadedFileStore(db)

    first = store.upsert_by_storage_path(
        user_id=int(user.id),
        filename="kb.md",
        storage_path=source,
        mime_type="text/markdown",
        file_size=source.stat().st_size,
    )
    first_key = str(first.storage_key)
    db.commit()

    source.write_text("second", encoding="utf-8")
    second = store.upsert_by_storage_path(
        user_id=int(user.id),
        filename="kb-renamed.md",
        storage_path=source,
        mime_type="text/markdown",
        file_size=source.stat().st_size,
    )
    db.commit()

    assert second.id == first.id
    assert second.filename == "kb-renamed.md"
    assert second.file_size == len("second")
    with get_file_storage().open_read(first_key) as handle:
        assert handle.read() == b"second"
