from pathlib import Path

from xagent.core.file_storage.factory import get_file_storage


def test_local_file_storage_round_trips_file(monkeypatch, tmp_path):
    storage_root = tmp_path / "objects"
    materialize_dir = tmp_path / "materialized"
    source = tmp_path / "source.txt"
    source.write_text("hello durable storage", encoding="utf-8")

    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", storage_root.as_uri())
    monkeypatch.setenv("XAGENT_FILE_MATERIALIZE_DIR", str(materialize_dir))
    get_file_storage.cache_clear()

    storage = get_file_storage()
    stored = storage.put_file(
        source, "users/1/uploads/file-id/source.txt", "text/plain"
    )

    assert stored.backend == "file"
    assert stored.key == "users/1/uploads/file-id/source.txt"
    assert stored.size == len("hello durable storage")
    assert storage.exists(stored.key)

    with storage.open_read(stored.key) as handle:
        assert handle.read() == b"hello durable storage"

    materialized = storage.materialize(stored.key, "source.txt")
    assert materialized.parent == materialize_dir
    assert materialized.read_text(encoding="utf-8") == "hello durable storage"

    listed = storage.list("users/1/uploads")
    assert [item.key for item in listed] == [stored.key]

    storage.delete(stored.key)
    assert not storage.exists(stored.key)


def test_local_file_storage_put_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()

    storage = get_file_storage()
    stored = storage.put_bytes(b"abc", "bytes/data.bin")

    assert stored.size == 3
    assert Path(stored.uri.removeprefix("file://")).read_bytes() == b"abc"
