"""Owner-scoped access to builtin OAuth credentials.

All ordinary callers pass ``resource_owner_key=None``. Trusted actor callers
pass an exact server-derived key. Centralizing the predicate prevents a direct
ID lookup or provider list from accidentally widening into another namespace.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Query, Session

from ..models.user_oauth import (
    USER_OAUTH_RESOURCE_OWNER_KEY_MAX_LENGTH,
    UserOAuth,
)


def normalize_user_oauth_resource_owner_key(value: Any) -> str | None:
    """Return one valid owner key, preserving ``None`` as ordinary ownership."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("resource_owner_key must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("resource_owner_key must not be blank")
    if len(normalized) > USER_OAUTH_RESOURCE_OWNER_KEY_MAX_LENGTH:
        raise ValueError(
            "resource_owner_key exceeds "
            f"{USER_OAUTH_RESOURCE_OWNER_KEY_MAX_LENGTH} characters"
        )
    return normalized


def scoped_user_oauth_query(
    db: Session,
    *,
    user_id: int,
    resource_owner_key: str | None,
) -> Query[UserOAuth]:
    """Build a query restricted to one xagent user and one owner namespace."""
    owner_key = normalize_user_oauth_resource_owner_key(resource_owner_key)
    query = db.query(UserOAuth).filter(UserOAuth.user_id == int(user_id))
    if owner_key is None:
        return query.filter(UserOAuth.resource_owner_key.is_(None))
    return query.filter(UserOAuth.resource_owner_key == owner_key)


def list_scoped_user_oauth_accounts(
    db: Session,
    *,
    user_id: int,
    resource_owner_key: str | None,
) -> list[UserOAuth]:
    """List one owner's credentials in stable creation order."""
    return (
        scoped_user_oauth_query(
            db,
            user_id=user_id,
            resource_owner_key=resource_owner_key,
        )
        .order_by(UserOAuth.id)
        .all()
    )


def get_scoped_user_oauth_account(
    db: Session,
    *,
    user_id: int,
    account_id: int,
    resource_owner_key: str | None,
) -> UserOAuth | None:
    """Get a credential by ID only when the expected owner also matches."""
    return (
        scoped_user_oauth_query(
            db,
            user_id=user_id,
            resource_owner_key=resource_owner_key,
        )
        .filter(UserOAuth.id == int(account_id))
        .first()
    )


def delete_scoped_user_oauth_accounts(
    db: Session,
    *,
    user_id: int,
    resource_owner_key: str | None,
    providers: Sequence[str] | None = None,
) -> int:
    """Delete matching local credentials without committing the caller's session."""
    query = scoped_user_oauth_query(
        db,
        user_id=user_id,
        resource_owner_key=resource_owner_key,
    )
    if providers is not None:
        provider_keys = tuple(dict.fromkeys(str(provider) for provider in providers))
        if not provider_keys:
            return 0
        query = query.filter(UserOAuth.provider.in_(provider_keys))
    return int(query.delete(synchronize_session=False))
