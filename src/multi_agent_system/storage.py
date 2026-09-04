import json
from typing import Any, Dict, Optional

import redis


class RedisStateStore:
    def __init__(self, redis_url: str) -> None:
        self.client = redis.from_url(redis_url, decode_responses=True)

    def save_state(self, thread_id: str, state: Dict[str, Any]) -> None:
        self.client.set(f"agent:state:{thread_id}", json.dumps(state, ensure_ascii=False))

    def load_state(self, thread_id: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(f"agent:state:{thread_id}")
        if not raw:
            return None
        return json.loads(raw)
