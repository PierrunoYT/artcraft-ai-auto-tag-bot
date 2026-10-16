import pytest
from cryptography.fernet import Fernet

from artcraft.config import Config


@pytest.fixture(autouse=True)
def config_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "private-discord-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-openrouter-key")
    monkeypatch.setenv("FORUM_CHANNEL_IDS", "100, 200,100")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("MAX_AI_TAGS", raising=False)
    monkeypatch.delenv("TAG_MODE", raising=False)
    monkeypatch.delenv("COMMAND_USER_IDS", raising=False)
    monkeypatch.delenv("TAG_CONCURRENCY", raising=False)
    for name in ("DUPLICATES", "DUPLICATE_DB_KEY", "DUPLICATE_DB_PATH", "EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DUPLICATE_THRESHOLD", raising=False)
    monkeypatch.delenv("DUPLICATE_MAX_MATCHES", raising=False)


def test_defaults_and_secrets_are_not_in_repr():
    config = Config.from_env()
    assert config.model == "openai/gpt-6-luna"
    assert config.forum_channel_ids == frozenset({100, 200})
    assert config.max_ai_tags == 3
    assert config.tag_mode == "add"
    assert config.command_user_ids == frozenset()
    assert config.tag_concurrency == 4
    assert config.duplicates is False
    assert config.duplicate_threshold == 0.7
    assert config.duplicate_max_matches == 5
    assert config.embedding_model == "openai/text-embedding-3-small"
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
        ("OPENROUTER_MODEL", "openai/gpt-6-luna:online"),
        ("TAG_MODE", "merge"),
        ("COMMAND_USER_IDS", "123,abc"),
        ("COMMAND_USER_IDS", "0"),
        ("TAG_CONCURRENCY", "0"),
        ("TAG_CONCURRENCY", "11"),
        ("TAG_CONCURRENCY", "fast"),
        ("DUPLICATES", "maybe"),
        ("DUPLICATE_THRESHOLD", "0.2"),
        ("DUPLICATE_THRESHOLD", "1"),
        ("DUPLICATE_DB_PATH", " "),
        ("DUPLICATE_MAX_MATCHES", "0"),
        ("DUPLICATE_MAX_MATCHES", "11"),
    ],
)
def test_invalid_config_fails_before_connecting(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Config.from_env()


def test_model_is_configurable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "another/model")
    assert Config.from_env().model == "another/model"


def test_tag_mode_is_configurable(monkeypatch):
    monkeypatch.setenv("TAG_MODE", " Replace ")
    assert Config.from_env().tag_mode == "replace"


def test_command_users_are_configurable(monkeypatch):
    monkeypatch.setenv("COMMAND_USER_IDS", "293486003245809664, 42")
    assert Config.from_env().command_user_ids == frozenset({293486003245809664, 42})


@pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
def test_duplicates_require_a_valid_encryption_key(monkeypatch, key):
    monkeypatch.setenv("DUPLICATES", "on")
    monkeypatch.setenv("DUPLICATE_DB_KEY", key)
    with pytest.raises(ValueError, match="DUPLICATE_DB_KEY"):
        Config.from_env()


def test_duplicates_can_be_enabled_and_key_is_not_in_repr(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DUPLICATES", "on")
    monkeypatch.setenv("DUPLICATE_DB_KEY", key)
    monkeypatch.setenv("DUPLICATE_THRESHOLD", "0.75")
    config = Config.from_env()
    assert (config.duplicates, config.duplicate_threshold) == (True, 0.75)
    assert key not in repr(config)


def test_tag_concurrency_is_configurable(monkeypatch):
    monkeypatch.setenv("TAG_CONCURRENCY", "8")
    assert Config.from_env().tag_concurrency == 8
