import json
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph

from multi_agent_system.storage import RedisCheckpointSaver, RedisStateStore


class FakeRedis:
    """Redis 3.2 command subset used by the persistence implementation."""

    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.sets = {}
        self.zsets = {}

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        return True

    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    def delete(self, *keys):
        count = 0
        for key in keys:
            for store in (self.values, self.hashes, self.sets, self.zsets):
                if key in store:
                    del store[key]
                    count += 1
                    break
        return count

    def sadd(self, key, *values):
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.update(values)
        return len(target) - before

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, *values):
        target = self.sets.get(key, set())
        before = len(target)
        target.difference_update(values)
        return before - len(target)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def _zrange(self, key, start, end, reverse=False):
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
            reverse=reverse,
        )
        if end == -1:
            end = len(items) - 1
        return [member for member, _ in items[start : end + 1]]

    def zrange(self, key, start, end):
        return self._zrange(key, start, end)

    def zrevrange(self, key, start, end):
        return self._zrange(key, start, end, reverse=True)

    def zrem(self, key, *members):
        target = self.zsets.get(key, {})
        count = 0
        for member in members:
            if member in target:
                del target[member]
                count += 1
        return count

    def hset(self, key, field, value):
        target = self.hashes.setdefault(key, {})
        is_new = field not in target
        target[field] = value
        return int(is_new)

    def hsetnx(self, key, field, value):
        target = self.hashes.setdefault(key, {})
        if field in target:
            return 0
        target[field] = value
        return 1

    def hvals(self, key):
        return list(self.hashes.get(key, {}).values())


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
                "redis://test:6379/0", decode_responses=True, protocol=2
            )

    def test_checkpoint_constructor_forces_redis_3_2_compatible_resp2(self):
        with patch("redis.from_url") as mock_from_url:
            RedisCheckpointSaver("redis://test:6379/0")
            mock_from_url.assert_called_once_with(
                "redis://test:6379/0", decode_responses=False, protocol=2
            )

    def test_task_records_survive_store_recreation(self):
        client = FakeRedis()
        first = RedisStateStore("redis://unused", client=client)
        first.save_task(
            "thread-1",
            {"task": "persist me", "status": "running", "created_at": 10.0},
        )

        second = RedisStateStore("redis://unused", client=client)

        assert second.load_task("thread-1")["status"] == "running"
        assert second.list_tasks()[0][0] == "thread-1"
        assert second.ping() is True
        assert second.delete_task("thread-1") is True
        assert second.load_task("thread-1") is None


class TestRedisCheckpointSaver:
    @staticmethod
    def _checkpoint(value):
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"value": value}
        checkpoint["channel_versions"] = {"value": value}
        checkpoint["updated_channels"] = ["value"]
        return checkpoint

    def test_round_trips_parent_chain_and_pending_writes(self):
        client = FakeRedis()
        first = RedisCheckpointSaver("redis://unused", client=client)
        base_config = {
            "configurable": {"thread_id": "thread/a", "checkpoint_ns": ""}
        }
        first_checkpoint = self._checkpoint(1)
        first_config = first.put(
            base_config, first_checkpoint, {"source": "input", "step": -1}, {}
        )
        first.put_writes(
            first_config,
            [("worker_result", {"ok": True})],
            task_id="worker-1",
            task_path="push/0",
        )
        second_checkpoint = self._checkpoint(2)
        second_config = first.put(
            first_config, second_checkpoint, {"source": "loop", "step": 0}, {}
        )

        recreated = RedisCheckpointSaver("redis://unused", client=client)
        latest = recreated.get_tuple(base_config)
        original = recreated.get_tuple(first_config)

        assert latest.checkpoint["channel_values"]["value"] == 2
        assert latest.parent_config["configurable"]["checkpoint_id"] == first_checkpoint["id"]
        assert original.pending_writes == [
            ("worker-1", "worker_result", {"ok": True})
        ]
        assert second_config["configurable"]["checkpoint_id"] == second_checkpoint["id"]

    def test_lists_filters_and_deletes_thread(self):
        client = FakeRedis()
        saver = RedisCheckpointSaver("redis://unused", client=client)
        config = {"configurable": {"thread_id": "thread-2", "checkpoint_ns": ""}}
        first = saver.put(
            config, self._checkpoint(1), {"source": "input", "step": -1}, {}
        )
        saver.put(first, self._checkpoint(2), {"source": "loop", "step": 0}, {})

        assert len(list(saver.list(config))) == 2
        filtered = list(saver.list(config, filter={"source": "loop"}, limit=1))
        assert len(filtered) == 1
        assert filtered[0].metadata["step"] == 0

        saver.delete_thread("thread-2")

        assert saver.get_tuple(config) is None
        assert list(saver.list(config)) == []

    def test_new_graph_instance_resumes_failed_run(self):
        class RecoveryState(TypedDict):
            value: int

        fail_once = {"enabled": True}

        def unstable_node(state: RecoveryState):
            if fail_once["enabled"]:
                raise RuntimeError("simulated process failure")
            return {"value": state["value"] + 1}

        def compile_graph(checkpointer):
            builder = StateGraph(RecoveryState)
            builder.add_node("unstable", unstable_node)
            builder.add_edge(START, "unstable")
            builder.add_edge("unstable", END)
            return builder.compile(checkpointer=checkpointer)

        client = FakeRedis()
        config = {"configurable": {"thread_id": "recover-me"}}
        first_graph = compile_graph(
            RedisCheckpointSaver("redis://unused", client=client)
        )
        with pytest.raises(RuntimeError, match="simulated process failure"):
            first_graph.invoke({"value": 1}, config=config)

        fail_once["enabled"] = False
        second_graph = compile_graph(
            RedisCheckpointSaver("redis://unused", client=client)
        )

        assert second_graph.invoke(None, config=config) == {"value": 2}
