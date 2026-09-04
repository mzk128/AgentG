import json
from unittest.mock import MagicMock, patch

import pytest

from multi_agent_system.storage import RedisStateStore


class TestRedisStateStore:
    def test_save_state_calls_redis_set(self):
        mock_client = MagicMock()
        with patch("redis.from_url", return_value=mock_client):
            store = RedisStateStore("redis://test:6379/0")
            state = {"task": "test task", "plan": "do stuff"}

            store.save_state("thread-123", state)

            mock_client.set.assert_called_once()
            args = mock_client.set.call_args[0]
            assert args[0] == "agent:state:thread-123"
            decoded = json.loads(args[1])
            assert decoded["task"] == "test task"
            assert decoded["plan"] == "do stuff"

    def test_save_state_with_special_characters(self):
        mock_client = MagicMock()
        with patch("redis.from_url", return_value=mock_client):
            store = RedisStateStore("redis://test:6379/0")
            state = {"task": "中文字符 ✨", "code": "print('hello')"}

            store.save_state("thread-abc", state)

            decoded = json.loads(mock_client.set.call_args[0][1])
            assert decoded["task"] == "中文字符 ✨"

    def test_load_state_returns_dict(self):
        mock_client = MagicMock()
        saved_state = {"task": "analyze", "plan": "step 1"}
        mock_client.get.return_value = json.dumps(saved_state, ensure_ascii=False)

        with patch("redis.from_url", return_value=mock_client):
            store = RedisStateStore("redis://test:6379/0")
            result = store.load_state("thread-456")

            assert result == saved_state
            mock_client.get.assert_called_once_with("agent:state:thread-456")

    def test_load_state_returns_none_when_missing(self):
        mock_client = MagicMock()
        mock_client.get.return_value = None

        with patch("redis.from_url", return_value=mock_client):
            store = RedisStateStore("redis://test:6379/0")
            result = store.load_state("nonexistent")

            assert result is None

    def test_load_state_returns_none_when_empty_string(self):
        mock_client = MagicMock()
        mock_client.get.return_value = ""

        with patch("redis.from_url", return_value=mock_client):
            store = RedisStateStore("redis://test:6379/0")
            result = store.load_state("empty")

            assert result is None

    def test_constructor_uses_decode_responses(self):
        with patch("redis.from_url") as mock_from_url:
            RedisStateStore("redis://test:6379/0")
            mock_from_url.assert_called_once_with(
                "redis://test:6379/0", decode_responses=True
            )
