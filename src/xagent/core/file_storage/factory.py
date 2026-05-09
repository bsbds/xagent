from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

import fsspec

from ...config import (
    get_file_materialize_dir,
    get_file_storage_options,
    get_file_storage_uri,
)
from .storage import FsspecFileStorage


@lru_cache
def get_file_storage() -> FsspecFileStorage:
    """Build the configured durable file storage backend."""
    uri = get_file_storage_uri()
    options = get_file_storage_options()
    parsed = urlparse(uri)
    backend = parsed.scheme or "file"

    try:
        fs, root = fsspec.core.url_to_fs(uri, **options)
    except ImportError as exc:
        if backend == "s3":
            raise RuntimeError(
                "XAGENT_FILE_STORAGE_URI uses s3:// but s3fs is not installed"
            ) from exc
        raise

    return FsspecFileStorage(
        fs=fs,
        root=str(root),
        backend=backend,
        base_uri=uri,
        materialize_dir=get_file_materialize_dir(),
    )
