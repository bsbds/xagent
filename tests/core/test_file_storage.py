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
    assert materialized.is_relative_to(materialize_dir)
    assert materialized.name == "source.txt"
    assert materialized.read_text(encoding="utf-8") == "hello durable storage"

    listed = storage.list("users/1/uploads")
    assert [item.key for item in listed] == [stored.key]

    storage.delete(stored.key)
    assert not storage.exists(stored.key)


def test_put_file_hashes_while_copying(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()

    storage = get_file_storage()
    source = tmp_path / "single-pass.txt"
    source.write_bytes(b"hash while copying")

    def fail_second_read(path: Path) -> str:
        raise AssertionError(f"unexpected second read for checksum: {path}")

    monkeypatch.setattr(storage, "_sha256", fail_second_read)

    stored = storage.put_file(source, "uploads/single-pass.txt", "text/plain")

    assert stored.checksum
    assert storage.open_read(stored.key).read() == b"hash while copying"


def test_local_file_storage_put_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()

    storage = get_file_storage()
    stored = storage.put_bytes(b"abc", "bytes/data.bin")

    assert stored.size == 3
    assert Path(stored.uri.removeprefix("file://")).read_bytes() == b"abc"


def test_object_uri_quotes_key_without_backend_branch(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()

    storage = get_file_storage()
    stored = storage.put_bytes(b"abc", "uploads/file with spaces.txt")

    assert stored.uri.endswith("/uploads/file%20with%20spaces.txt")


def test_local_file_storage_copies_object_to_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    get_file_storage.cache_clear()

    storage = get_file_storage()
    stored = storage.put_bytes(b"restore me", "copies/data.txt")
    target = tmp_path / "restored" / "data.txt"

    copied = storage.copy_to_path(stored.key, target)

    assert copied == target
    assert target.read_bytes() == b"restore me"


def test_materialize_isolates_objects_with_same_filename(monkeypatch, tmp_path):
    materialize_dir = tmp_path / "materialized"
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", (tmp_path / "objects").as_uri())
    monkeypatch.setenv("XAGENT_FILE_MATERIALIZE_DIR", str(materialize_dir))
    get_file_storage.cache_clear()

    storage = get_file_storage()
    first = storage.put_bytes(b"first content", "users/1/tasks/1/output/report.txt")
    second = storage.put_bytes(b"second content", "users/2/tasks/2/output/report.txt")

    first_path = storage.materialize(first.key, "report.txt")
    second_path = storage.materialize(second.key, "report.txt")

    assert first_path != second_path
    assert first_path.is_relative_to(materialize_dir)
    assert second_path.is_relative_to(materialize_dir)
    assert first_path.name == "report.txt"
    assert second_path.name == "report.txt"
    assert first_path.read_bytes() == b"first content"
    assert second_path.read_bytes() == b"second content"
