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
        assert s.context_max_tokens == 320000
        assert s.context_output_reserve == 16000
        assert s.conversation_max_tokens == 4000
        assert s.python_timeout_seconds == 15.0
        assert s.model_request_timeout_seconds == 180.0
        assert s.max_review_rounds == 100
        assert s.long_term_memory_enabled is False
        assert s.memory_db_path.endswith("long_term.sqlite3")
        assert s.memory_db_busy_timeout_ms == 5000


class TestGetSettings:
    def test_long_term_memory_policy_environment(self, monkeypatch):
        for key, value in {
            "MEMORY_USER_ID": "local-person", "MEMORY_PROJECT_ID": "project-one",
            "MEMORY_TOP_K": "3", "MEMORY_MAX_TOKENS": "2400", "MEMORY_SCAN_LIMIT": "50",
            "MEMORY_AUTO_EXTRACT": "true",
        }.items():
            monkeypatch.setenv(key, value)
        configured = get_settings()
        assert (configured.memory_user_id, configured.memory_project_id) == ("local-person", "project-one")
        assert (configured.memory_top_k, configured.memory_max_tokens, configured.memory_scan_limit) == (3, 2400, 50)
        assert configured.memory_auto_extract is True

    @pytest.mark.parametrize("value, expected", [("100", 100), ("3", 3), ("1", 1), ("0", 1), ("invalid", 2)])
    def test_review_rounds_configuration(self, monkeypatch, value, expected):
        monkeypatch.setenv("MAX_REVIEW_ROUNDS", value)
        assert get_settings().max_review_rounds == expected

    def test_context_and_tool_settings(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_MAX_TOKENS", "24000")
        monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE", "3000")
        monkeypatch.setenv("CONVERSATION_MAX_TOKENS", "5000")
        monkeypatch.setenv("SUMMARY_MAX_TOKENS", "900")
        monkeypatch.setenv("MEMORY_RECENT_TURNS", "4")
        monkeypatch.setenv("UPSTREAM_MAX_TOKENS", "3500")
        monkeypatch.setenv("TOOL_OUTPUT_MAX_CHARS", "6000")
        monkeypatch.setenv("PYTHON_TIMEOUT_SECONDS", "7.5")
        monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "33.5")
        settings = get_settings()
        assert (settings.context_max_tokens, settings.context_output_reserve) == (24000, 3000)
        assert (settings.conversation_max_tokens, settings.summary_max_tokens, settings.recent_turns) == (5000, 900, 4)
        assert settings.upstream_max_tokens == 3500
        assert settings.tool_output_max_chars == 6000
        assert settings.python_timeout_seconds == 7.5
        assert settings.model_request_timeout_seconds == 33.5

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
