from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import quote

from .types import StoredObject


class FsspecFileStorage:
    """Small fsspec-backed storage wrapper using keys relative to a root URI."""

    def __init__(
        self,
        *,
        fs: Any,
        root: str,
        backend: str,
        base_uri: str,
        materialize_dir: Path,
    ) -> None:
        self._fs = fs
        self._root = root.rstrip("/")
        self._backend = backend
        self._base_uri = base_uri.rstrip("/")
        self._materialize_dir = materialize_dir

    def put_file(
        self, source: Path, key: str, content_type: str | None = None
    ) -> StoredObject:
        del content_type
        normalized_key = self._normalize_key(key)
        destination = self._full_path(normalized_key)
        self._makedirs_for_key(normalized_key)
        digest = hashlib.sha256()
        with source.open("rb") as src, self._fs.open(destination, "wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
                dst.write(chunk)
        return self._stored_object(normalized_key, checksum=digest.hexdigest())

    def put_bytes(
        self, data: bytes, key: str, content_type: str | None = None
    ) -> StoredObject:
        del content_type
        normalized_key = self._normalize_key(key)
        destination = self._full_path(normalized_key)
        self._makedirs_for_key(normalized_key)
        with self._fs.open(destination, "wb") as dst:
            dst.write(data)
        return self._stored_object(
            normalized_key, checksum=hashlib.sha256(data).hexdigest()
        )

    def open_read(self, key: str) -> BinaryIO:
        return cast(
            BinaryIO,
            self._fs.open(self._full_path(self._normalize_key(key)), "rb"),
        )

    def exists(self, key: str) -> bool:
        return bool(self._fs.exists(self._full_path(self._normalize_key(key))))

    def stat(self, key: str) -> StoredObject:
        return self._stored_object(self._normalize_key(key))

    def list(self, prefix: str) -> list[StoredObject]:
        normalized_prefix = self._normalize_key(prefix).rstrip("/")
        full_prefix = self._full_path(normalized_prefix)
        if not self._fs.exists(full_prefix):
            return []
        entries = self._fs.find(full_prefix, detail=True)
        return [
            self._stored_object_from_info(self._relative_key(path), info)
            for path, info in sorted(entries.items())
            if not self._is_directory_entry(path, info)
        ]

    def delete(self, key: str) -> None:
        full_path = self._full_path(self._normalize_key(key))
        if self._fs.exists(full_path):
            self._fs.rm(full_path)

    def materialize(self, key: str, filename: str | None = None) -> Path:
        normalized_key = self._normalize_key(key)
        target_name = Path(filename or normalized_key).name or "file"
        key_digest = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()[:16]
        target_path = self._materialize_dir / key_digest / target_name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self.open_read(normalized_key) as src, target_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return target_path

    def copy_to_path(self, key: str, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self.open_read(key) as src, target_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return target_path

    def _full_path(self, key: str) -> str:
        if not self._root:
            return key
        return f"{self._root}/{key}"

    def _relative_key(self, full_path: str) -> str:
        normalized = str(full_path).lstrip("/")
        root = self._root.lstrip("/")
        if root and normalized.startswith(root.rstrip("/") + "/"):
            return normalized[len(root.rstrip("/") + "/") :]
        return normalized

    def _makedirs_for_key(self, key: str) -> None:
        parent = str(Path(self._full_path(key)).parent)
        if parent and parent != ".":
            self._fs.makedirs(parent, exist_ok=True)

    def _stored_object(self, key: str, checksum: str | None = None) -> StoredObject:
        info = self._fs.info(self._full_path(key))
        return self._stored_object_from_info(key, info, checksum=checksum)

    def _stored_object_from_info(
        self,
        key: str,
        info: dict[str, Any],
        checksum: str | None = None,
    ) -> StoredObject:
        etag = info.get("ETag") or info.get("etag")
        return StoredObject(
            backend=self._backend,
            key=key,
            uri=self._object_uri(key),
            size=int(info.get("size", 0)),
            checksum=checksum,
            etag=str(etag) if etag is not None else None,
        )

    @staticmethod
    def _is_directory_entry(path: str, info: dict[str, Any]) -> bool:
        entry_type = str(info.get("type", "")).lower()
        return entry_type == "directory" or str(path).rstrip("/").endswith("/")

    def _object_uri(self, key: str) -> str:
        quoted_key = quote(key, safe="/")
        return f"{self._base_uri}/{quoted_key}"

    @staticmethod
    def _normalize_key(key: str) -> str:
        normalized = key.strip().lstrip("/")
        if not normalized or ".." in Path(normalized).parts:
            raise ValueError(f"Invalid storage key: {key!r}")
        return normalized

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
