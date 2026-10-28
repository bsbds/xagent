"""Every "left for TTL recovery" exit records unsettled evidence; no success path does.

The existing resume scenarios in ``test_execution_scope_turn_wiring`` already
drive each retention branch (pool timeouts, unhealthy heartbeat, unresolvable
checkpoint, lease loss) and the normal settle paths with the real
``execute_resume_background``. They are re-run here unchanged, measuring the
process hold's ``unsettled_total`` before and after: one record per
retained lease, none for a proven release.

A source guard pins the instrumented functions, so a new retention branch
added elsewhere in them cannot silently skip the record. The behavioural
proof for the Web-local sites lives beside each site's own scenarios
(``test_managed_task_lease``, ``test_execute_task_manage_task_lease``,
``test_task_orchestrator``, ``test_channel_runtime``, v1 ``test_task_reply``
and ``test_a2a_api``), which own their fixtures.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.web.api import test_execution_scope_turn_wiring as wiring
from xagent.web.services import (
    agent_service_manager,
    channel_runtime,
    managed_task_lease,
)
from xagent.web.services import release_hold as release_hold_module
from xagent.web.services import task_command_transport as transport
from xagent.web.services import task_coordinator_runtime as runtime
from xagent.web.services import (
    task_execution,
    task_orchestrator,
    task_resume,
)

# (scenario, unsettled records it must leave)
RESUME_SCENARIOS: list[tuple[Callable[..., Awaitable[None]], int]] = [
    (wiring.test_resume_background_adopts_preacquired_lease_without_reacquiring, 0),
    (wiring.test_resume_final_usage_and_heartbeat_finish_before_lease_release, 0),
    (wiring.test_resume_background_restores_prior_status_for_preacquired_lease, 0),
    (wiring.test_resume_pool_timeout_does_not_start_secondary_db_cleanup, 1),
    (wiring.test_resume_tracker_pool_timeout_skips_result_and_lease_checkouts, 1),
    (wiring.test_resume_heartbeat_pool_timeout_skips_result_and_lease_checkouts, 1),
    (wiring.test_resume_error_delivery_pool_timeout_skips_lease_checkout, 1),
    (
        wiring.test_resume_background_checkpoint_unavailable_from_pool_timeout_keeps_lease,
        1,
    ),
    (wiring.test_resume_lease_loss_cancels_execution_without_stale_side_effects, 1),
]


@pytest.mark.parametrize(
    ("scenario", "expected"),
    RESUME_SCENARIOS,
    ids=[scenario.__name__ for scenario, _ in RESUME_SCENARIOS],
)
async def test_resume_exits_record_unsettled_only_when_the_lease_is_retained(
    scenario: Callable[..., Awaitable[None]],
    expected: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hold = release_hold_module.release_hold
    before = hold.unsettled_total
    kwargs: dict[str, Any] = (
        {"caplog": caplog} if "caplog" in inspect.signature(scenario).parameters else {}
    )
    await scenario(**kwargs)
    assert hold.unsettled_total - before == expected
    if expected:
        assert hold.snapshot()["unsettled"][-1]["kind"] == "resume_lease"


# (function, the call through which it records): direct calls, or the one
# helper each site routes every retained outcome through.
RETENTION_SITES: list[tuple[Callable[..., Any], str]] = [
    (task_execution.execute_task_background, "release_hold.record_unsettled("),
    (task_execution.execute_resume_background, "release_hold.record_unsettled("),
    (runtime.TaskCoordinator._close, "release_hold.record_unsettled("),
    (transport._dispatch_shared_task_command, "release_hold.record_unsettled("),
    # Web-local retention sites (parent acceptance R2 audit).
    (managed_task_lease.ManagedTaskLease.finalize_result, "self._confirmed("),
    (managed_task_lease.ManagedTaskLease.close, "self._confirmed("),
    (
        managed_task_lease.ManagedTaskLease._record_unsettled,
        "release_hold.record_unsettled(",
    ),
    (
        agent_service_manager.AgentServiceManager.execute_task,
        "release_hold.record_unsettled(",
    ),
    (task_orchestrator._schedule_bg, "release_hold.record_unsettled("),
    (task_resume.resume_task_reply, "_record_unless_restored("),
    (task_resume.resume_a2a_task, "_record_unless_restored("),
    (task_resume._record_unless_restored, "release_hold.record_unsettled("),
    (channel_runtime.prepare_channel_task, "_record_uncompensated_claim("),
    (channel_runtime._record_uncompensated_claim, "release_hold.record_unsettled("),
]


@pytest.mark.parametrize(
    ("function", "marker"),
    RETENTION_SITES,
    ids=[function.__qualname__ for function, _ in RETENTION_SITES],
)
def test_each_retention_site_records_unsettled(
    function: Callable[..., Any], marker: str
) -> None:
    assert marker in inspect.getsource(function)


@pytest.mark.parametrize(
    ("outcome", "expected"), [("restored", 0), ("not_restored", 1), ("raises", 1)]
)
async def test_prelease_restore_records_unless_confirmed(
    outcome: str, expected: int
) -> None:
    hold = release_hold_module.release_hold
    before = hold.unsettled_total

    async def restore() -> bool:
        if outcome == "raises":
            raise RuntimeError("restore write fault")
        return outcome == "restored"

    if outcome == "raises":
        with pytest.raises(RuntimeError):
            await task_resume._record_unless_restored(41, restore)
    else:
        assert await task_resume._record_unless_restored(41, restore) is (
            outcome == "restored"
        )
    assert hold.unsettled_total - before == expected
