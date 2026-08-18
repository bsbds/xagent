"""Storage identity tests for ordinary and actor-owned builtin OAuth rows."""

from xagent.web.models.user_oauth import UserOAuth


def _where(index) -> str:
    clause = index.dialect_options["sqlite"].get("where")
    if clause is None:
        clause = index.dialect_options["postgresql"].get("where")
    return str(clause if clause is not None else "").lower()


def test_resource_owner_key_is_nullable_and_bounded() -> None:
    column = UserOAuth.__table__.columns["resource_owner_key"]

    assert column.nullable is True
    assert column.type.length == 512


def test_model_declares_ordinary_and_actor_owned_partial_uniqueness() -> None:
    indexes = {index.name: index for index in UserOAuth.__table__.indexes}

    ordinary = indexes["uq_user_oauth_ordinary_account"]
    assert ordinary.unique is True
    assert tuple(column.name for column in ordinary.columns) == (
        "user_id",
        "provider",
        "provider_user_id",
    )
    assert "resource_owner_key is null" in _where(ordinary)

    actor = indexes["uq_user_oauth_actor_account"]
    assert actor.unique is True
    assert tuple(column.name for column in actor.columns) == (
        "user_id",
        "resource_owner_key",
        "provider",
        "provider_user_id",
    )
    assert "resource_owner_key is not null" in _where(actor)

    lookup = indexes["ix_user_oauth_owner_provider"]
    assert lookup.unique is False
    assert tuple(column.name for column in lookup.columns) == (
        "user_id",
        "resource_owner_key",
        "provider",
    )
