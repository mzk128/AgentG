"""Redis-backed LangGraph checkpoints and Web task records.

The checkpointer intentionally uses only commands available in Redis 3.2.  It
does not require RedisJSON or RediSearch, so the project can use the user's
existing local Redis installation while still implementing LangGraph's native
thread/checkpoint recovery contract.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
import json
from typing import Any, Dict, Optional
from urllib.parse import quote

import redis
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


def _component(value: str) -> str:
    """Encode user-controlled identifiers into a single Redis key component."""
    return quote(value, safe="") or "__empty__"


class RedisCheckpointSaver(BaseCheckpointSaver):
    """LangGraph checkpointer implemented with Redis String/Hash/Set/ZSet.

    Each checkpoint stores a complete serialized snapshot.  Pending writes are
    stored independently, which lets LangGraph avoid re-running successful
    branches when another parallel branch fails.
    """

    PREFIX = "agentg:checkpoint"

    def __init__(self, redis_url: str, *, client: Any | None = None) -> None:
        super().__init__()
        self.client = client or redis.from_url(
            redis_url, decode_responses=False, protocol=2
        )

    @staticmethod
    def _config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    @classmethod
    def _data_key(cls, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return ":".join(
            (
                cls.PREFIX,
                "data",
                _component(thread_id),
                _component(checkpoint_ns),
                _component(checkpoint_id),
            )
        )

    @classmethod
    def _writes_key(cls, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return ":".join(
            (
                cls.PREFIX,
                "writes",
                _component(thread_id),
                _component(checkpoint_ns),
                _component(checkpoint_id),
            )
        )

    @classmethod
    def _index_key(cls, thread_id: str, checkpoint_ns: str) -> str:
        return ":".join(
            (
                cls.PREFIX,
                "index",
                _component(thread_id),
                _component(checkpoint_ns),
            )
        )

    @classmethod
    def _namespace_key(cls, thread_id: str) -> str:
        return f"{cls.PREFIX}:namespaces:{_component(thread_id)}"

    @classmethod
    def _threads_key(cls) -> str:
        return f"{cls.PREFIX}:threads"

    @classmethod
    def _sequence_key(cls) -> str:
        return f"{cls.PREFIX}:sequence"

    def _dump_typed(self, value: Any) -> dict[str, str]:
        type_name, payload = self.serde.dumps_typed(value)
        return {
            "type": type_name,
            "data": base64.b64encode(payload).decode("ascii"),
        }

    def _load_typed(self, value: dict[str, str]) -> Any:
        return self.serde.loads_typed(
            (value["type"], base64.b64decode(value["data"]))
        )

    @staticmethod
    def _decode_text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = checkpoint["id"]
        parent_id = get_checkpoint_id(config)
        envelope = {
            "checkpoint": self._dump_typed(checkpoint),
            "metadata": self._dump_typed(get_checkpoint_metadata(config, metadata)),
            "parent_checkpoint_id": parent_id,
        }

        data_key = self._data_key(thread_id, checkpoint_ns, checkpoint_id)
        index_key = self._index_key(thread_id, checkpoint_ns)
        sequence = self.client.incr(self._sequence_key())
        self.client.set(
            data_key,
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
        )
        self.client.zadd(index_key, {checkpoint_id: sequence})
        self.client.sadd(self._threads_key(), thread_id)
        self.client.sadd(self._namespace_key(thread_id), checkpoint_ns)
        return self._config(thread_id, checkpoint_ns, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = str(configurable["checkpoint_id"])
        key = self._writes_key(thread_id, checkpoint_ns, checkpoint_id)

        for index, (channel, value) in enumerate(writes):
            write_index = WRITES_IDX_MAP.get(channel, index)
            field = f"{_component(task_id)}:{write_index}"
            payload = json.dumps(
                {
                    "task_id": task_id,
                    "task_path": task_path,
                    "index": write_index,
                    "channel": channel,
                    "value": self._dump_typed(value),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if write_index < 0:
                self.client.hset(key, field, payload)
            else:
                self.client.hsetnx(key, field, payload)

    def _pending_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        raw_items = self.client.hvals(
            self._writes_key(thread_id, checkpoint_ns, checkpoint_id)
        )
        records = [json.loads(self._decode_text(item)) for item in raw_items]
        records.sort(key=lambda item: (item["task_id"], item["index"]))
        return [
            (item["task_id"], item["channel"], self._load_typed(item["value"]))
            for item in records
        ]

    def _tuple_for(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> CheckpointTuple | None:
        raw = self.client.get(
            self._data_key(thread_id, checkpoint_ns, checkpoint_id)
        )
        if not raw:
            return None
        envelope = json.loads(self._decode_text(raw))
        parent_id = envelope.get("parent_checkpoint_id")
        return CheckpointTuple(
            config=self._config(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint=self._load_typed(envelope["checkpoint"]),
            metadata=self._load_typed(envelope["metadata"]),
            parent_config=(
                self._config(thread_id, checkpoint_ns, parent_id)
                if parent_id
                else None
            ),
            pending_writes=self._pending_writes(
                thread_id, checkpoint_ns, checkpoint_id
            ),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)
        if not checkpoint_id:
            latest = self.client.zrevrange(
                self._index_key(thread_id, checkpoint_ns), 0, 0
            )
            if not latest:
                return None
            checkpoint_id = self._decode_text(latest[0])
        return self._tuple_for(thread_id, checkpoint_ns, checkpoint_id)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if limit is not None and limit <= 0:
            return

        if config:
            thread_ids = [str(config["configurable"]["thread_id"])]
        else:
            thread_ids = sorted(
                self._decode_text(value)
                for value in self.client.smembers(self._threads_key())
            )
        yielded = 0
        before_id = get_checkpoint_id(before) if before else None

        for thread_id in thread_ids:
            if config and "checkpoint_ns" in config["configurable"]:
                namespaces = [str(config["configurable"].get("checkpoint_ns", ""))]
            else:
                namespaces = sorted(
                    self._decode_text(value)
                    for value in self.client.smembers(self._namespace_key(thread_id))
                )
            for checkpoint_ns in namespaces:
                raw_ids = self.client.zrevrange(
                    self._index_key(thread_id, checkpoint_ns), 0, -1
                )
                for raw_id in raw_ids:
                    checkpoint_id = self._decode_text(raw_id)
                    if before_id and checkpoint_id >= before_id:
                        continue
                    item = self._tuple_for(thread_id, checkpoint_ns, checkpoint_id)
                    if item is None:
                        continue
                    if filter and any(
                        item.metadata.get(key) != value
                        for key, value in filter.items()
                    ):
                        continue
                    yield item
                    yielded += 1
                    if limit is not None and yielded >= limit:
                        return

    def delete_thread(self, thread_id: str) -> None:
        namespaces = [
            self._decode_text(value)
            for value in self.client.smembers(self._namespace_key(thread_id))
        ]
        keys: list[str] = []
        for checkpoint_ns in namespaces:
            index_key = self._index_key(thread_id, checkpoint_ns)
            checkpoint_ids = [
                self._decode_text(value)
                for value in self.client.zrange(index_key, 0, -1)
            ]
            for checkpoint_id in checkpoint_ids:
                keys.extend(
                    (
                        self._data_key(thread_id, checkpoint_ns, checkpoint_id),
                        self._writes_key(thread_id, checkpoint_ns, checkpoint_id),
                    )
                )
            keys.append(index_key)
        keys.append(self._namespace_key(thread_id))
        if keys:
            self.client.delete(*keys)
        self.client.srem(self._threads_key(), thread_id)


class RedisStateStore:
    """Persistent Web task repository plus legacy final-state compatibility."""

    TASK_PREFIX = "agentg:task"
    TASK_INDEX = "agentg:tasks:index"

    def __init__(self, redis_url: str, *, client: Any | None = None) -> None:
        self.client = client or redis.from_url(
            redis_url, decode_responses=True, protocol=2
        )

    @staticmethod
    def _task_key(thread_id: str) -> str:
        return f"{RedisStateStore.TASK_PREFIX}:{_component(thread_id)}"

    @staticmethod
    def _decode_text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def ping(self) -> bool:
        return bool(self.client.ping())

    def save_task(self, thread_id: str, record: Dict[str, Any]) -> None:
        created_at = float(record.get("created_at", 0) or 0)
        self.client.set(
            self._task_key(thread_id),
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        )
        self.client.zadd(self.TASK_INDEX, {thread_id: created_at})

    def load_task(self, thread_id: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(self._task_key(thread_id))
        if not raw:
            return None
        return json.loads(self._decode_text(raw))

    def list_tasks(self) -> list[tuple[str, Dict[str, Any]]]:
        thread_ids = self.client.zrevrange(self.TASK_INDEX, 0, -1)
        records: list[tuple[str, Dict[str, Any]]] = []
        for raw_id in thread_ids:
            thread_id = self._decode_text(raw_id)
            record = self.load_task(thread_id)
            if record is not None:
                records.append((thread_id, record))
        return records

    def delete_task(self, thread_id: str) -> bool:
        existed = bool(self.client.delete(self._task_key(thread_id)))
        self.client.zrem(self.TASK_INDEX, thread_id)
        return existed

    def save_state(self, thread_id: str, state: Dict[str, Any]) -> None:
        """Legacy final snapshot API retained for backward compatibility."""
        self.client.set(
            f"agent:state:{thread_id}", json.dumps(state, ensure_ascii=False)
        )

    def load_state(self, thread_id: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(f"agent:state:{thread_id}")
        if not raw:
            return None
        return json.loads(self._decode_text(raw))
