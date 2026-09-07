import os
from unittest import mock

import pytest

from multi_agent_system.config import Settings, get_settings


class TestSettings:
    def test_settings_is_frozen(self):
        s = Settings(
            openai_api_key="k",
            openai_base_url="https://x.com",
            model_name="m",
            use_redis=False,
            redis_url="redis://localhost:6379/0",
        )
        with pytest.raises(Exception):
            s.openai_api_key = "new"

    def test_settings_default_values(self):
        s = Settings(
            openai_api_key="k",
            openai_base_url="https://x.com",
            model_name="m",
            use_redis=False,
            redis_url="redis://localhost:6379/0",
        )
        assert s.use_redis is False
        assert s.redis_url == "redis://localhost:6379/0"
        assert s.web_reload is False
        assert s.max_tool_rounds == 4
        assert s.max_tool_calls == 12
        assert s.max_consecutive_errors == 2
        assert s.max_run_seconds == 120.0
        assert s.total_action_budget == 40


class TestGetSettings:
    def test_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.com/v1")
        monkeypatch.setenv("MODEL_NAME", "env-model")
        monkeypatch.setenv("USE_REDIS", "false")
        monkeypatch.setenv("REDIS_URL", "redis://env-redis:6379/1")
        monkeypatch.setenv("WEB_RELOAD", "true")
        monkeypatch.setenv("MAX_TOOL_ROUNDS", "3")
        monkeypatch.setenv("MAX_TOOL_CALLS", "9")
        monkeypatch.setenv("MAX_CONSECUTIVE_ERRORS", "4")
        monkeypatch.setenv("MAX_RUN_SECONDS", "45.5")
        monkeypatch.setenv("TOTAL_ACTION_BUDGET", "22")

        s = get_settings()
        assert s.openai_api_key == "sk-env-key"
        assert s.openai_base_url == "https://env.example.com/v1"
        assert s.model_name == "env-model"
        assert s.use_redis is False
        assert s.redis_url == "redis://env-redis:6379/1"
        assert s.web_reload is True
        assert s.max_tool_rounds == 3
        assert s.max_tool_calls == 9
        assert s.max_consecutive_errors == 4
        assert s.max_run_seconds == 45.5
        assert s.total_action_budget == 22

    def test_use_redis_true(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("USE_REDIS", "true")

        s = get_settings()
        assert s.use_redis is True

    def test_use_redis_mixed_case(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("USE_REDIS", "TRUE")

        s = get_settings()
        assert s.use_redis is True

    def test_missing_api_key_returns_empty_string(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        s = get_settings()
        assert s.openai_api_key == ""

    def test_default_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        s = get_settings()
        assert s.openai_base_url == "https://api.openai.com/v1"

    def test_default_model_name(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.delenv("MODEL_NAME", raising=False)

        s = get_settings()
        assert s.model_name == "gpt-4o-mini"
