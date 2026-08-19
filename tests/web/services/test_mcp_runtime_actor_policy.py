from __future__ import annotations

import pytest

from xagent.web.models.user_oauth import USER_OAUTH_RESOURCE_OWNER_KEY_MAX_LENGTH
from xagent.web.services.mcp_runtime import MCPRuntimeAuthorizationPolicy


def test_actor_policy_normalizes_builtin_oauth_owner_key() -> None:
    policy = MCPRuntimeAuthorizationPolicy(
        builtin_oauth_resource_owner_key="  actor:alice  ",
        allowed_server_ids=frozenset({7}),
    )

    assert policy.builtin_oauth_resource_owner_key == "actor:alice"


@pytest.mark.parametrize("value", [None, 7, True, "", "   "])
def test_actor_policy_rejects_invalid_builtin_oauth_owner_key(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        MCPRuntimeAuthorizationPolicy(
            builtin_oauth_resource_owner_key=value,  # type: ignore[arg-type]
            allowed_server_ids=frozenset(),
        )


def test_actor_policy_rejects_oversize_builtin_oauth_owner_key() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        MCPRuntimeAuthorizationPolicy(
            builtin_oauth_resource_owner_key=(
                "x" * (USER_OAUTH_RESOURCE_OWNER_KEY_MAX_LENGTH + 1)
            ),
            allowed_server_ids=frozenset(),
        )
