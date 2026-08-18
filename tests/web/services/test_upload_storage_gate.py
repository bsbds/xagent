"""Concurrency and cancellation contracts for durable-upload admission."""

import asyncio

import pytest
from xagent.web.services.upload_storage_gate import (
    UploadStorageCapacityError,
    UploadStorageGate,
)


@pytest.mark.asyncio
async def test_gate_bounds_concurrent_leases() -> None:
    gate = UploadStorageGate(max_concurrency=2, queue_timeout_seconds=1)
    active = 0
    maximum_active = 0
    release = asyncio.Event()
    first_pair_active = asyncio.Event()

    async def use_gate() -> None:
        nonlocal active, maximum_active
        async with gate.lease():
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                first_pair_active.set()
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(use_gate()) for _ in range(8)]
    await asyncio.wait_for(first_pair_active.wait(), timeout=1)

    assert maximum_active == 2
    assert gate.active == 2

    release.set()
    await asyncio.gather(*tasks)
    assert gate.active == 0


@pytest.mark.asyncio
async def test_gate_times_out_without_consuming_capacity() -> None:
    gate = UploadStorageGate(max_concurrency=1, queue_timeout_seconds=0.01)

    async with gate.lease():
        with pytest.raises(UploadStorageCapacityError):
            async with gate.lease():
                raise AssertionError("timed-out lease must not enter")
        assert gate.active == 1

    async with gate.lease():
        assert gate.active == 1


@pytest.mark.asyncio
async def test_gate_releases_capacity_when_lease_owner_is_cancelled() -> None:
    gate = UploadStorageGate(max_concurrency=1, queue_timeout_seconds=1)
    acquired = asyncio.Event()
    block = asyncio.Event()

    async def hold_lease() -> None:
        async with gate.lease():
            acquired.set()
            await block.wait()

    task = asyncio.create_task(hold_lease())
    await asyncio.wait_for(acquired.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.active == 0
    async with gate.lease():
        assert gate.active == 1
