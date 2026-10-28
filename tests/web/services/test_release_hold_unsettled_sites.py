"""Every "left for TTL recovery" exit records unsettled evidence; no success path does.

The existing resume scenarios in ``test_execution_scope_turn_wiring`` already
drive each retention branch (pool timeouts, unhealthy heartbeat, unresolvable
checkpoint, lease loss) and the normal settle paths with the real
``execute_resume_background``. They are re-run here unchanged, measuring the
process hold's ``unsettled_total`` before and after: one record per
retained lease, none for a proven release.

A source guard pins the instrumented functions, so a new retention branch
added elsewhere in them cannot silently skip the record.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.web.api import test_execution_scope_turn_wiring as wiring
from xagent.web.services import release_hold as release_hold_module
from xagent.web.services import task_command_transport as transport
from xagent.web.services import task_coordinator_runtime as runtime
from xagent.web.services import task_execution

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


@pytest.mark.parametrize(
    "function",
    [
        task_execution.execute_task_background,
        task_execution.execute_resume_background,
        runtime.TaskCoordinator._close,
        transport._dispatch_shared_task_command,
    ],
    ids=lambda function: function.__qualname__,
)
def test_each_retention_site_records_unsettled(function: Callable[..., Any]) -> None:
    source = inspect.getsource(function)
    assert "record_unsettled(" in source
