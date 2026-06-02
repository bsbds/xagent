from xagent.web.models.user_channel import UserChannel


def test_user_channel_constructor_accepts_config_dict() -> None:
    channel = UserChannel(
        user_id=1,
        channel_type="telegram",
        channel_name="Telegram Bot",
        config={"bot_token": "plain-token"},
        is_active=True,
    )

    assert channel.config["bot_token"] == "plain-token"
    assert channel._config["bot_token"] != "plain-token"
