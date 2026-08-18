"""Process-local admission control for durable upload registration.

The gate lives at the asynchronous HTTP boundary so requests wait without
occupying the shared ``asyncio.to_thread`` executor. A lease covers the full
cancellation-safe registration call: if the client disconnects, capacity is
released only after the worker has settled and can no longer write storage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from ...config import (
    get_file_upload_max_concurrency,
    get_file_upload_queue_timeout_seconds,
)


class UploadStorageCapacityError(TimeoutError):
    """Raised when an upload cannot enter durable registration in time."""


class UploadStorageGate:
    """Bound active durable upload registrations within one backend process."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        queue_timeout_seconds: float,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._queue_timeout_seconds = queue_timeout_seconds
        self._active = 0

    @property
    def active(self) -> int:
        """Return the number of leases currently performing registration."""

        return self._active

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        """Acquire registration capacity and release it on every scope exit."""

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._queue_timeout_seconds,
            )
        except TimeoutError as exc:
            raise UploadStorageCapacityError(
                "Timed out waiting for durable upload capacity"
            ) from exc

        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()


@lru_cache(maxsize=1)
def get_upload_storage_gate() -> UploadStorageGate:
    """Return the configured gate shared by upload requests in this process."""

    return UploadStorageGate(
        max_concurrency=get_file_upload_max_concurrency(),
        queue_timeout_seconds=get_file_upload_queue_timeout_seconds(),
    )
