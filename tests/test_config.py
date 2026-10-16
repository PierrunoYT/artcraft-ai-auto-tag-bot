import pytest

from artcraft.config import Config


@pytest.fixture(autouse=True)
def config_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "private-discord-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-openrouter-key")
    monkeypatch.setenv("FORUM_CHANNEL_IDS", "100, 200,100")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("MAX_AI_TAGS", raising=False)


def test_defaults_and_secrets_are_not_in_repr():
    config = Config.from_env()
    assert config.model == "openai/gpt-4.1-mini"
    assert config.forum_channel_ids == frozenset({100, 200})
    assert config.max_ai_tags == 3
    assert "private" not in repr(config)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FORUM_CHANNEL_IDS", ""),
        ("FORUM_CHANNEL_IDS", "100,"),
        ("FORUM_CHANNEL_IDS", "-1"),
        ("FORUM_CHANNEL_IDS", "hello"),
        ("MAX_AI_TAGS", "0"),
        ("MAX_AI_TAGS", "6"),
        ("DISCORD_TOKEN", ""),
        ("OPENROUTER_API_KEY", ""),
        ("OPENROUTER_MODEL", ""),
        ("OPENROUTER_MODEL", "openai/gpt-4.1-mini:online"),
    ],
)
def test_invalid_config_fails_before_connecting(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Config.from_env()


def test_model_is_configurable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "another/model")
    assert Config.from_env().model == "another/model"
